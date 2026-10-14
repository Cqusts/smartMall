"""agent_trace 归档：搬而不删、分批、可中断可重跑、不丢数据。

跑在真实 SQLite 上——要验的是 SQL 本身（INSERT..SELECT..NOT EXISTS、DELETE..IN、
派生表 LIMIT），内存桩验不出来。真 MySQL 上的行为（含线上并发写入）另有验证。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text

from smartmall_pipeline import archive as arch
from smartmall_pipeline.archive import ArchiveInconsistency, TRACE_COLUMNS, archive_traces

CUTOFF = datetime(2026, 1, 1, 0, 0, 0)

_COLS = """
    id INTEGER PRIMARY KEY, trace_id TEXT NOT NULL, session_id TEXT NOT NULL,
    user_id INTEGER, product_id INTEGER, input_text TEXT NOT NULL,
    rewritten_query TEXT DEFAULT '', intent TEXT DEFAULT '',
    retrieval_hit_count INTEGER DEFAULT 0, retrieval_max_score REAL DEFAULT 0,
    retrieval_item_ids TEXT, answer TEXT, citations TEXT, postcheck_flags TEXT,
    handover INTEGER DEFAULT 0, handover_reason TEXT DEFAULT '', model TEXT DEFAULT '',
    latency_ms TEXT, error TEXT DEFAULT '', thumb INTEGER, feedback_reason TEXT DEFAULT '',
    feedback_at TEXT, created_at TEXT NOT NULL"""


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    with eng.begin() as c:
        c.execute(text(f"CREATE TABLE agent_trace ({_COLS})"))
        c.execute(text(f"CREATE TABLE agent_trace_archive ({_COLS}, archived_at TEXT DEFAULT CURRENT_TIMESTAMP)"))
    return eng


def seed(engine, *, old=0, new=0, old_feedback=0, start_id=1):
    """old 行早于 CUTOFF，new 行晚于它；old_feedback 是早于 CUTOFF 但带点赞的行。id 连续递增。"""
    rows, i = [], start_id
    for kind, n in (("old", old), ("old_fb", old_feedback), ("new", new)):
        for _ in range(n):
            at = (CUTOFF - timedelta(days=30) if kind != "new" else CUTOFF + timedelta(days=30))
            rows.append({
                "id": i, "t": f"trace{i:06d}", "s": f"sess{i % 7}", "u": i % 50, "p": 1 + i % 9,
                "q": f"问题{i}", "a": f"答案{i}", "th": 1 if kind == "old_fb" else None,
                "at": at.strftime("%Y-%m-%d %H:%M:%S")})
            i += 1
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO agent_trace (id, trace_id, session_id, user_id, product_id, input_text, answer,"
            " thumb, created_at) VALUES (:id, :t, :s, :u, :p, :q, :a, :th, :at)"), rows)
    return rows


def ids(engine, table):
    with engine.connect() as c:
        return {r[0] for r in c.execute(text(f"SELECT id FROM {table}"))}


class TestMovesNotDeletes:
    def test_old_rows_move_and_new_rows_stay(self, engine):
        seed(engine, old=30, new=10)
        r = archive_traces(engine, cutoff=CUTOFF, sleep=0)
        live, archived = ids(engine, "agent_trace"), ids(engine, "agent_trace_archive")
        assert r.archived == 30 and r.stopped_by == "done" and r.eligible_remaining == 0
        assert archived == set(range(1, 31)) and live == set(range(31, 41))
        assert not (live & archived), "同一行不该同时在两张表里"

    def test_every_column_is_preserved(self, engine):
        """搬的时候漏一列（比如新加的列没进 TRACE_COLUMNS），数据就悄悄丢了。"""
        rows = seed(engine, old=3)
        with engine.begin() as c:
            c.execute(text("UPDATE agent_trace SET rewritten_query='改写', intent='fit', retrieval_hit_count=4,"
                           " retrieval_max_score=0.8123, retrieval_item_ids='[1,2]', citations='[1]',"
                           " postcheck_flags='[]', handover=1, handover_reason='low_conf', model='m',"
                           " latency_ms=:lat, error='e', feedback_reason='r', feedback_at='2025-01-01 00:00:00'"),
                      {"lat": '{"a": 1}'})
            before = [tuple(r) for r in c.execute(text(f"SELECT {', '.join(TRACE_COLUMNS)} FROM agent_trace ORDER BY id"))]
        archive_traces(engine, cutoff=CUTOFF, sleep=0)
        with engine.connect() as c:
            after = [tuple(r) for r in c.execute(text(f"SELECT {', '.join(TRACE_COLUMNS)} FROM agent_trace_archive ORDER BY id"))]
        assert after == before and len(after) == 3

    def test_cutoff_is_exclusive(self, engine):
        with engine.begin() as c:
            c.execute(text("INSERT INTO agent_trace (id, trace_id, session_id, input_text, created_at)"
                           " VALUES (1,'a','s','x','2026-01-01 00:00:00'), (2,'b','s','x','2025-12-31 23:59:59')"))
        archive_traces(engine, cutoff=CUTOFF, sleep=0)
        assert ids(engine, "agent_trace") == {1} and ids(engine, "agent_trace_archive") == {2}

    def test_created_at_is_checked_not_just_id(self, engine):
        """id 与时间大体同序，但不保证（手工导入、时钟回拨）。id 落在范围内、时间却是新的行不能被搬走。"""
        with engine.begin() as c:
            c.execute(text("INSERT INTO agent_trace (id, trace_id, session_id, input_text, created_at) VALUES"
                           " (1,'a','s','x','2025-06-01 00:00:00'),"
                           " (2,'b','s','x','2026-06-01 00:00:00'),"     # 新，夹在两个旧行之间
                           " (3,'c','s','x','2025-06-02 00:00:00')"))
        archive_traces(engine, cutoff=CUTOFF, sleep=0)
        assert ids(engine, "agent_trace") == {2} and ids(engine, "agent_trace_archive") == {1, 3}

    def test_nothing_to_do(self, engine):
        seed(engine, new=5)
        r = archive_traces(engine, cutoff=CUTOFF, sleep=0)
        assert r.stopped_by == "nothing" and r.archived == 0 and len(ids(engine, "agent_trace")) == 5


class TestFeedbackRowsAreTrainingData:
    def test_rows_with_feedback_stay_by_default(self, engine):
        seed(engine, old=10, old_feedback=4)
        r = archive_traces(engine, cutoff=CUTOFF, sleep=0)
        assert r.archived == 10 and r.skipped_feedback == 4
        assert ids(engine, "agent_trace") == set(range(11, 15))

    def test_include_feedback_moves_them_too(self, engine):
        seed(engine, old=10, old_feedback=4)
        r = archive_traces(engine, cutoff=CUTOFF, sleep=0, include_feedback=True)
        assert r.archived == 14 and ids(engine, "agent_trace") == set()

    def test_feedback_rows_inside_a_batch_do_not_stop_progress(self, engine):
        """一整批全是点赞行 → 这一批没有可搬的，但必须继续往后走，不能当成"搬完了"。"""
        seed(engine, old=0, old_feedback=8, new=0)
        seed(engine, old=6, start_id=9)                      # 点赞行在前，普通旧行在后
        r = archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=4)
        assert r.archived == 6 and ids(engine, "agent_trace") == set(range(1, 9))


class TestBatching:
    def test_each_batch_is_bounded_and_progress_is_reported(self, engine):
        seed(engine, old=100)
        seen = []
        r = archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=7,
                           progress=lambda res: seen.append(res.archived))
        assert r.batches == 15 and r.archived == 100            # ceil(100/7)
        deltas = [b - a for a, b in zip([0] + seen, seen)]
        assert max(deltas) <= 7, "一批超过了 batch_size：事务会越来越大"

    def test_sleep_between_batches(self, engine):
        seed(engine, old=20)
        naps = []
        archive_traces(engine, cutoff=CUTOFF, sleep=0.25, batch_size=10, sleeper=naps.append)
        assert naps == [0.25, 0.25], "每批之间要停顿，给线上写入让路"

    def test_batch_size_validated(self, engine):
        with pytest.raises(ValueError):
            archive_traces(engine, cutoff=CUTOFF, batch_size=0)

    def test_final_probe_does_not_scan_live_rows(self, engine):
        """没有这个上界的话，最后一批查不到东西时要把整张在线表扫一遍才敢收工。
        新行（id 大于 max_id）一行都不该被碰到。"""
        seed(engine, old=10, new=500)
        stmts = []

        @event.listens_for(engine, "before_cursor_execute")
        def spy(conn, cur, statement, params, context, executemany):
            stmts.append((statement, params))
        archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=4)
        for statement, params in stmts:
            if "MAX(id) FROM (SELECT id" in statement:
                assert 10 in params, f"批探测没有带上 max_id 上界：{params}"


class TestResumable:
    def test_stopping_early_and_rerunning_finishes_the_job(self, engine):
        seed(engine, old=100, new=5)
        r1 = archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=10, max_batches=3)
        assert r1.stopped_by == "max_batches" and r1.archived == 30 and r1.eligible_remaining == 70
        r2 = archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=10)
        assert r2.stopped_by == "done" and r2.archived == 70
        assert len(ids(engine, "agent_trace_archive")) == 100 and len(ids(engine, "agent_trace")) == 5

    def test_time_budget_stops_cleanly(self, engine):
        seed(engine, old=100)
        t = {"now": 0.0}

        def clock():
            return t["now"]

        def tick(_):
            t["now"] += 1.0
        r = archive_traces(engine, cutoff=CUTOFF, sleep=1, batch_size=10, max_seconds=3.5,
                           clock=clock, sleeper=tick)
        assert r.stopped_by == "time_budget" and r.archived == 40 and r.eligible_remaining == 60

    def test_a_crash_in_the_middle_loses_nothing_and_a_rerun_completes(self, engine):
        """第 3 批删除时崩了：前两批已提交，第 3 批整批回滚（既没复制、也没删）。重跑后每行恰好一份。"""
        seed(engine, old=40)
        state = {"deletes": 0}

        @event.listens_for(engine, "before_cursor_execute")
        def boom(conn, cur, statement, params, context, executemany):
            if statement.lstrip().startswith("DELETE FROM agent_trace"):
                state["deletes"] += 1
                if state["deletes"] == 3:
                    raise RuntimeError("进程被杀")

        with pytest.raises(Exception, match="进程被杀"):
            archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=10)
        assert ids(engine, "agent_trace_archive") == set(range(1, 21)), "前两批应当已提交，且第 3 批整批回滚"
        assert ids(engine, "agent_trace") == set(range(21, 41))
        event.remove(engine, "before_cursor_execute", boom)

        r = archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=10)
        assert r.archived == 20
        assert ids(engine, "agent_trace_archive") == set(range(1, 41)) and ids(engine, "agent_trace") == set()

    def test_rows_already_in_the_archive_do_not_duplicate_or_block(self, engine):
        """上一次复制了、删除没完成（比如被人手工中断过）：重跑只该补上删除，不能主键冲突，不能重复。"""
        seed(engine, old=6)
        with engine.begin() as c:
            c.execute(text("INSERT INTO agent_trace_archive SELECT *, CURRENT_TIMESTAMP FROM agent_trace WHERE id <= 3"))
        r = archive_traces(engine, cutoff=CUTOFF, sleep=0)
        assert r.archived == 6
        assert ids(engine, "agent_trace_archive") == set(range(1, 7)) and ids(engine, "agent_trace") == set()
        with engine.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM agent_trace_archive")).scalar() == 6


class TestNeverDeletesWhatItDidNotCopy:
    def _interfere_before_delete(self, engine, *, times):
        """复制之后、删除之前，有一行刚好被点了赞（不再满足"没有点赞"）——它被复制了，
        却不会被删除：归档表里一份、在线表里还有一份。"""
        fired = {"n": 0}

        @event.listens_for(engine, "before_cursor_execute")
        def interfere(conn, cur, statement, params, context, executemany):
            if statement.lstrip().startswith("DELETE FROM agent_trace") and fired["n"] < times:
                fired["n"] += 1
                cur.execute("UPDATE agent_trace SET thumb = 1 WHERE id = 4")
        return fired

    def test_a_row_changed_mid_batch_rolls_back_and_the_retry_is_consistent(self, engine):
        seed(engine, old=10)
        fired = self._interfere_before_delete(engine, times=1)
        r = archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=10, sleeper=lambda s: None)
        assert fired["n"] == 1 and r.retries == 1, "第一次尝试应当检测到重复、整批回滚并重试"
        # 注意：SQLite 是单写者，这里的"并发修改"只能在同一个事务里做，所以它跟着第一次尝试一起回滚了；
        # 重试时数据是干净的，10 行全部搬走。（真 MySQL 上点赞是别的连接提交的，见提交说明里的实测。）
        assert ids(engine, "agent_trace") == set()
        assert ids(engine, "agent_trace_archive") == set(range(1, 11))

    def test_persistent_conflict_gives_up_after_the_attempt_limit_and_leaves_everything_intact(self, engine):
        seed(engine, old=10)
        self._interfere_before_delete(engine, times=99)
        # 每次重试都被同一行"打断"：第二次起第 4 行已有点赞、不会再被复制，所以其实不会再冲突——
        # 要制造持续冲突，让每次尝试都改一行还没被点赞的
        @event.listens_for(engine, "before_cursor_execute")
        def keep_breaking(conn, cur, statement, params, context, executemany):
            if statement.lstrip().startswith("DELETE FROM agent_trace"):
                cur.execute("UPDATE agent_trace SET thumb = 1 WHERE id = (SELECT MIN(id) FROM agent_trace WHERE thumb IS NULL)")
        with pytest.raises(ArchiveInconsistency):
            archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=10, sleeper=lambda s: None)
        assert ids(engine, "agent_trace_archive") == set(), "放弃时最后一次尝试也必须回滚"
        assert ids(engine, "agent_trace") == set(range(1, 11)), "回滚后在线表一行都不能少"

    def test_deadlock_victim_is_retried_not_fatal(self, engine):
        """MySQL 的死锁（1213）把我们的事务当了牺牲品：整批已回滚，重做这一批即可。"""
        seed(engine, old=10)
        state = {"n": 0}

        class Deadlock(Exception):
            def __init__(self):
                self.orig = type("O", (), {"args": (1213, "Deadlock found when trying to get lock")})()

        @event.listens_for(engine, "before_cursor_execute")
        def victim(conn, cur, statement, params, context, executemany):
            if statement.lstrip().startswith("DELETE FROM agent_trace") and state["n"] == 0:
                state["n"] = 1
                raise Deadlock()
        r = archive_traces(engine, cutoff=CUTOFF, sleep=0, batch_size=10, sleeper=lambda s: None)
        assert r.retries == 1 and r.archived == 10
        assert ids(engine, "agent_trace") == set() and ids(engine, "agent_trace_archive") == set(range(1, 11))

    def test_a_non_retryable_error_is_not_swallowed(self, engine):
        seed(engine, old=10)

        @event.listens_for(engine, "before_cursor_execute")
        def boom(conn, cur, statement, params, context, executemany):
            if statement.lstrip().startswith("DELETE FROM agent_trace"):
                raise RuntimeError("磁盘满了")
        with pytest.raises(Exception, match="磁盘满了"):
            archive_traces(engine, cutoff=CUTOFF, sleep=0, sleeper=lambda s: None)
        assert ids(engine, "agent_trace") == set(range(1, 11)) and ids(engine, "agent_trace_archive") == set()

    def test_delete_is_restricted_to_rows_present_in_the_archive(self, engine):
        """即使复制那一步什么都没复制成，删除也不能删掉在线表里没复制的行。"""
        seed(engine, old=5)

        @event.listens_for(engine, "before_cursor_execute", retval=True)
        def neuter_copy(conn, cur, statement, params, context, executemany):
            if statement.lstrip().startswith("INSERT INTO agent_trace_archive"):
                statement = statement.replace("FROM agent_trace WHERE", "FROM agent_trace WHERE 1=0 AND")
            return statement, params

        r = archive_traces(engine, cutoff=CUTOFF, sleep=0)
        assert ids(engine, "agent_trace") == set(range(1, 6)), "没复制成功的行被删了"
        assert r.archived == 0 and r.eligible_remaining == 5, "搬不动就该如实报告还剩多少"


class TestDryRun:
    def test_changes_nothing_and_reports_what_would_happen(self, engine):
        seed(engine, old=12, old_feedback=3, new=4)
        r = archive_traces(engine, cutoff=CUTOFF, dry_run=True)
        assert r.stopped_by == "dry_run" and r.eligible_remaining == 12 and r.skipped_feedback == 3
        assert len(ids(engine, "agent_trace")) == 19 and ids(engine, "agent_trace_archive") == set()


class TestSchemaDrift:
    """给在线表加了列却忘了归档表，归档时才报 Unknown column——而这时通常已经是线上了。"""

    ROOT = Path(__file__).resolve().parents[2] / "deploy" / "sql" / "migrations"

    @staticmethod
    def _columns(sql: str, table: str) -> set[str]:
        body = re.search(rf"CREATE TABLE IF NOT EXISTS `{table}` \((.*?)\n\) ENGINE", sql, re.S).group(1)
        return set(re.findall(r"^\s*`(\w+)`\s", body, re.M))

    def test_trace_columns_match_the_live_table(self):
        live = self._columns((self.ROOT / "003_agent_trace_and_handover.sql").read_text(encoding="utf-8"), "agent_trace")
        alter = re.search(r"ALTER TABLE `agent_trace`(.*?);", (self.ROOT / "009_marketing_copy.sql").read_text(encoding="utf-8"), re.S).group(1)
        live |= set(re.findall(r"ADD COLUMN `(\w+)`", alter))
        assert set(TRACE_COLUMNS) == live, f"差异：{set(TRACE_COLUMNS) ^ live}"

    def test_archive_table_has_every_live_column(self):
        archive = self._columns((self.ROOT / "015_agent_trace_archive.sql").read_text(encoding="utf-8"), "agent_trace_archive")
        assert set(TRACE_COLUMNS) <= archive, f"归档表缺列：{set(TRACE_COLUMNS) - archive}"
        assert "archived_at" in archive


class TestCli:
    def test_end_to_end_with_dry_run_then_real(self, engine, monkeypatch, capsys):
        from smartmall_pipeline import cli

        class Repo:
            pass
        repo = Repo()
        repo.engine = engine
        monkeypatch.setattr(cli.DwsRepository, "from_env", classmethod(lambda cls: repo))
        monkeypatch.setattr(arch, "db_now", lambda eng: CUTOFF + timedelta(days=90))     # 保留期 90 天 → cutoff = CUTOFF
        seed(engine, old=8, old_feedback=2, new=3)

        assert cli.main(["archive-traces", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "预览" in out and "8" in out
        assert len(ids(engine, "agent_trace")) == 13

        assert cli.main(["archive-traces", "--batch-size", "3", "--sleep", "0"]) == 0
        out = capsys.readouterr().out
        assert "已归档 8 行" in out and "带点赞点踩" in out
        assert len(ids(engine, "agent_trace_archive")) == 8 and len(ids(engine, "agent_trace")) == 5

    def test_inconsistency_is_reported_not_traceback(self, engine, monkeypatch, capsys):
        from smartmall_pipeline import cli

        class Repo:
            pass
        repo = Repo()
        repo.engine = engine
        monkeypatch.setattr(cli.DwsRepository, "from_env", classmethod(lambda cls: repo))
        monkeypatch.setattr(arch, "db_now", lambda eng: CUTOFF + timedelta(days=90))
        monkeypatch.setattr(arch, "archive_traces",
                            lambda *a, **k: (_ for _ in ()).throw(ArchiveInconsistency("批 (id 0, 5] 删除后仍有 1 行同时在两张表里，已回滚")))
        assert cli.main(["archive-traces"]) == 1
        assert "已回滚" in capsys.readouterr().out
