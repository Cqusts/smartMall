#!/usr/bin/env python3
"""agent_trace 的容量基准与归档验证（真 MySQL）。

**只动一个叫 smartmall_bench 的临时库**，不碰 smartmall。需要管理员账号
（MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD，约定同 deploy/scripts/migrate.py）。

    python db_trace_bench.py fill 1000000     建临时库（真实 DDL：003 + 009 + 015）并灌 100 万行
    python db_trace_bench.py probe            单行自动提交插入压 5 秒 + 几条真实读查询的耗时
    python db_trace_bench.py verify           归档验证：线上写入和点赞同时进行，校验不丢不重

**为什么 buffer pool 的差别在默认配置下看不出来：**OS 的 page cache 会替它兜底。要让 buffer pool
成为唯一的缓存，得让 MySQL 用 ``innodb_flush_method=O_DIRECT``（专用数据库服务器的常见配置）。
对比 128MB 与 1GB 时请带上它，并用 ``SET GLOBAL innodb_buffer_pool_size = ...`` 在线切换。

``verify`` 的环境变量：``INS_RATE``（每个写入线程每秒几条，默认 12，8 个线程合计约 100 条/秒，
接近这个项目真实的对话峰值；调大看极限）。
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import smartmall_env as env  # noqa: E402

ROOT = env.ROOT
DB = "smartmall_bench"
MIG = ROOT / "deploy" / "sql" / "migrations"


def conn(db: str | None = DB, autocommit: bool = True):
    import pymysql

    user, password = env.admin_credentials()
    return pymysql.connect(host=env.host(), port=env.port(), user=user, password=password,
                           database=db, autocommit=autocommit, charset="utf8mb4")


def scalar(sql: str, *args):
    c = conn(); cur = c.cursor(); cur.execute(sql, args or None); r = cur.fetchone(); c.close()
    return r[0] if r and len(r) == 1 else r


def setup() -> None:
    c = conn(None); cur = c.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {DB}")
    cur.execute(f"CREATE DATABASE {DB} CHARACTER SET utf8mb4")
    c.select_db(DB)
    for f in ("003_agent_trace_and_handover.sql", "009_marketing_copy.sql", "015_agent_trace_archive.sql"):
        sql = re.sub(r"--[^\n]*", "", (MIG / f).read_text(encoding="utf-8"))
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            head = stmt.split("(")[0]
            if "agent_trace" in head or stmt.upper().startswith("ALTER TABLE `AGENT_TRACE`"):
                cur.execute(stmt)
    c.close()


INSERT = ("INSERT INTO agent_trace (trace_id, session_id, user_id, product_id, input_text, rewritten_query, intent,"
          " retrieval_hit_count, retrieval_max_score, retrieval_item_ids, answer, citations, postcheck_flags, handover,"
          " handover_reason, model, latency_ms, error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
QS = ["这件衣服会起球吗", "洗几次之后会不会变形", "什么时候能发货", "支持七天无理由退货吗", "有没有大码",
      "尺码偏大还是偏小", "可以开发票吗", "运费怎么算"]
ANS = ("您好，这款面料经过预缩处理，水洗后变形率低于国标；支持七天无理由退货，"
       "发货时间一般为付款后 48 小时内。[#12][#31] ") * 2


def row(rnd: random.Random):
    """和 app 写入的形状一致：trace_id/session_id 是随机十六进制（二级索引的随机插入点）。"""
    return (uuid.uuid4().hex, uuid.uuid4().hex, rnd.randint(1, 5000), rnd.randint(1, 580),
            rnd.choice(QS) + str(rnd.randint(0, 50)), "", "product_knowledge", rnd.randint(0, 5),
            round(rnd.random(), 4), json.dumps([rnd.randint(1, 900) for _ in range(5)]), ANS,
            json.dumps([12, 31]), "[]", 1 if rnd.random() < 0.05 else 0, "", "chat-default",
            json.dumps({"retrieve": 40, "generate": 2100}), "")


def cmd_fill(n: int, threads: int = 4) -> None:
    setup()
    per = n // threads
    t0 = time.perf_counter()

    def work(seed):
        rnd = random.Random(seed); c = conn(autocommit=False); cur = c.cursor(); done = 0
        while done < per:
            batch = [row(rnd) for _ in range(min(1000, per - done))]
            cur.executemany(INSERT, batch); c.commit(); done += len(batch)
        c.close()
    ths = [threading.Thread(target=work, args=(random.random(),)) for _ in range(threads)]
    [t.start() for t in ths]; [t.join() for t in ths]
    print(f"灌了 {per * threads} 行，{per * threads / (time.perf_counter() - t0):,.0f} 行/秒")


def insert_probe(seconds: float = 5, threads: int = 16):
    lat, stop, lock = [], time.perf_counter() + seconds, threading.Lock()

    def work(seed):
        rnd = random.Random(seed); c = conn(); cur = c.cursor(); mine = []
        while time.perf_counter() < stop:
            r = row(rnd); t = time.perf_counter(); cur.execute(INSERT, r); mine.append(time.perf_counter() - t)
        with lock:
            lat.extend(mine)
        c.close()
    ths = [threading.Thread(target=work, args=(random.random(),)) for _ in range(threads)]
    [t.start() for t in ths]; [t.join() for t in ths]
    lat.sort()
    return len(lat) / seconds, lat[len(lat) // 2] * 1000, lat[int(len(lat) * .99)] * 1000


def demand_ms(window_days: int | None = None, n: int = 20) -> float:
    """n 个不同商品的 product_demand 的 p50。"""
    c = conn(); cur = c.cursor(); ts = []
    for pid in random.Random(3).sample(range(1, 580), n):
        sql = ("SELECT input_text, COUNT(*) c FROM agent_trace WHERE product_id=%s AND input_text<>''"
               + (" AND created_at >= NOW() - INTERVAL %s DAY" if window_days else "")
               + " GROUP BY input_text ORDER BY c DESC LIMIT 12")
        t = time.perf_counter(); cur.execute(sql, (pid, window_days) if window_days else (pid,)); cur.fetchall()
        ts.append((time.perf_counter() - t) * 1000)
    c.close(); ts.sort(); return round(ts[len(ts) // 2], 1)


def cmd_probe() -> None:
    cur = conn().cursor(); cur.execute("ANALYZE TABLE agent_trace")
    tps, p50, p99 = insert_probe()
    print(json.dumps({"rows": scalar("SELECT COUNT(*) FROM agent_trace"),
                      "single_insert_tps": round(tps), "insert_p50_ms": round(p50, 2), "insert_p99_ms": round(p99, 1),
                      "product_demand_p50_ms": demand_ms()}, ensure_ascii=False))


def _dirty() -> int:
    return int(scalar("SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_pages_dirty'")[1])


def cmd_verify() -> None:
    n0 = scalar("SELECT MAX(id) FROM agent_trace"); total0 = scalar("SELECT COUNT(*) FROM agent_trace")
    print(f"数据：{total0} 行，max_id={n0}")
    # 让数据像真实的：时间随 id 线性分布在最近 365 天（id 与时间同序），约 3% 带点赞
    c = conn(); cur = c.cursor()
    for lo in range(0, n0, 100_000):
        cur.execute("UPDATE agent_trace SET created_at = NOW() - INTERVAL ROUND((%s - id) / %s * 365 * 86400) SECOND,"
                    " thumb = IF(RAND() < 0.03, IF(RAND() < 0.5, 1, -1), NULL) WHERE id > %s AND id <= %s",
                    (n0, n0, lo, lo + 100_000))
    cur.execute("ANALYZE TABLE agent_trace"); c.close()

    chk = ("SUM(CRC32(CONCAT_WS('|', id, trace_id, session_id, IFNULL(user_id,''), IFNULL(product_id,''), input_text,"
           " IFNULL(answer,''), IFNULL(retrieval_item_ids,''), IFNULL(latency_ms,''), created_at)))")
    before = scalar(f"SELECT {chk} FROM agent_trace WHERE id <= %s", n0)
    old90 = scalar("SELECT COUNT(*) FROM agent_trace WHERE created_at < NOW() - INTERVAL 90 DAY")
    d_before = demand_ms()
    print(f"归档前：超过 90 天 {old90} 行；product_demand p50 {d_before}ms")

    rate = float(os.environ.get("INS_RATE", "12")); n_ins = 8
    # 基线：同等限速流量、不归档，10 秒
    def inserter(sink, stop, seed):
        rnd = random.Random(seed); c = conn(); cur = c.cursor(); gap = 1.0 / rate; nxt = time.perf_counter()
        while not stop.is_set():
            r = row(rnd); t = time.perf_counter(); cur.execute(INSERT, r); sink.append(time.perf_counter() - t)
            nxt += gap; time.sleep(max(0.0, nxt - time.perf_counter()))
        c.close()
    base, stop_b = [], threading.Event()
    bths = [threading.Thread(target=inserter, args=(base, stop_b, 100 + i)) for i in range(n_ins)]
    [t.start() for t in bths]; time.sleep(10); stop_b.set(); [t.join() for t in bths]
    base.sort()
    print(f"基线（同等限速流量、不归档）：{len(base) / 10:.0f}/s，p50 {base[len(base) // 2] * 1000:.2f}ms "
          f"p99 {base[int(len(base) * .99)] * 1000:.1f}ms max {base[-1] * 1000:.0f}ms")

    # 归档 + 同时线上写入 + 同时点赞
    sys.path.insert(0, str(ROOT / "apps" / "python" / "ai-agent"))
    from sqlalchemy import create_engine
    from app.agent.store import MySqlTraceStore

    user, password = env.admin_credentials()
    store = MySqlTraceStore(engine=create_engine(
        f"mysql+pymysql://{user}:{password}@{env.host()}:{env.port()}/{DB}?charset=utf8mb4"))
    stop, ins_lat, fb = threading.Event(), [], []

    def feedbacker():
        rnd = random.Random(9); c = conn(); cur = c.cursor()
        while not stop.is_set():
            pick = rnd.randint(1, n0)                                    # 老的新的都会点到
            cur.execute("SELECT trace_id FROM agent_trace WHERE id=%s UNION ALL "
                        "SELECT trace_id FROM agent_trace_archive WHERE id=%s", (pick, pick))
            r = cur.fetchone()
            if r:
                thumb = rnd.choice([1, -1]); fb.append((r[0], thumb, store.record_feedback(r[0], thumb, "测试")))
            time.sleep(0.04)
    ths = [threading.Thread(target=inserter, args=(ins_lat, stop, i)) for i in range(n_ins)] + [threading.Thread(target=feedbacker)]
    [t.start() for t in ths]
    e = {**os.environ, "MYSQL_HOST": env.host(), "MYSQL_PORT": str(env.port()), "MYSQL_USER": user,
         "MYSQL_PASSWORD": password, "MYSQL_DATABASE": DB, "PYTHONPATH": str(ROOT / "pipelines")}
    t0 = time.perf_counter()
    out = subprocess.run([sys.executable, "-m", "smartmall_pipeline.cli", "archive-traces", "--older-than-days", "90"],
                         env=e, capture_output=True, text=True, cwd=ROOT / "pipelines")
    dur = time.perf_counter() - t0
    stop.set(); [t.join() for t in ths]
    print("归档命令输出：")
    for ln in out.stdout.replace("\r", "\n").splitlines():
        if ln.strip() and ("已搬" not in ln or "已归档" in ln):
            print("   " + ln)
    if out.returncode:
        print("归档命令失败：", out.stderr[-800:])
    ins_lat.sort()
    print(f"归档用时 {dur:.1f}s；期间线上单行插入 {len(ins_lat) / dur:.0f}/s，p50 {ins_lat[len(ins_lat) // 2] * 1000:.2f}ms "
          f"p99 {ins_lat[int(len(ins_lat) * .99)] * 1000:.1f}ms max {ins_lat[-1] * 1000:.0f}ms")

    # 校验：不丢、不重、内容不变
    live = scalar("SELECT COUNT(*) FROM agent_trace WHERE id <= %s", n0); arc = scalar("SELECT COUNT(*) FROM agent_trace_archive")
    dup = scalar("SELECT COUNT(*) FROM agent_trace t JOIN agent_trace_archive a ON a.id = t.id")
    cols = ("id, trace_id, session_id, user_id, product_id, input_text, rewritten_query, intent, retrieval_hit_count,"
            " retrieval_max_score, retrieval_item_ids, answer, citations, postcheck_flags, handover, handover_reason,"
            " model, latency_ms, error, thumb, feedback_reason, feedback_at, created_at")
    after = scalar(f"SELECT {chk} FROM (SELECT {cols} FROM agent_trace WHERE id <= {n0} UNION ALL "
                   f"SELECT {cols} FROM agent_trace_archive) u")
    print(f"校验：在线 {live} + 归档 {arc} = {live + arc}（原 {total0}）；两表重复 {dup}；内容校验和 "
          f"{'一致' if before == after else '不一致!!'}")
    last, bad, on_archive = {}, 0, 0
    for tid, thumb, ok in fb:
        last[tid] = (thumb, ok)
    c = conn(); cur = c.cursor()
    for tid, (thumb, ok) in last.items():
        cur.execute("SELECT thumb FROM agent_trace WHERE trace_id=%s UNION ALL SELECT thumb FROM agent_trace_archive WHERE trace_id=%s", (tid, tid))
        if not ok or thumb not in [r[0] for r in cur.fetchall()]:
            bad += 1
        cur.execute("SELECT 1 FROM agent_trace_archive WHERE trace_id=%s", (tid,))
        on_archive += 1 if cur.fetchone() else 0
    print(f"点赞：期间 {len(fb)} 次（涉及 {len(last)} 条），其中落在归档表上的 {on_archive} 条；未落库/返回 False 的 {bad}")

    # 归档刚结束时实例还在刷脏页，延迟会很难看；等刷完再量才是稳态。两个数都给出来
    d_now = demand_ms(); dirty0 = _dirty(); t = time.perf_counter()
    while time.perf_counter() - t < 240 and _dirty() > 200:
        time.sleep(2)
    settle = time.perf_counter() - t
    print(f"归档刚结束：product_demand p50 {d_now}ms（此时还有 {dirty0} 个脏页在刷）")
    print(f"刷盘结束后（等了 {settle:.0f}s）：全部历史 p50 {demand_ms()}ms，90 天窗口 p50 {demand_ms(90)}ms；归档前 {d_before}ms")
    out2 = subprocess.run([sys.executable, "-m", "smartmall_pipeline.cli", "archive-traces", "--older-than-days", "90", "--dry-run"],
                          env=e, capture_output=True, text=True, cwd=ROOT / "pipelines")
    print("重跑 dry-run：", [ln.strip() for ln in out2.stdout.splitlines() if "预览" in ln or "没有" in ln])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fill":
        cmd_fill(int(sys.argv[2]) if len(sys.argv) > 2 else 1_000_000)
    elif cmd == "probe":
        cmd_probe()
    elif cmd == "verify":
        cmd_verify()
    else:
        print(__doc__)
        sys.exit(2)
