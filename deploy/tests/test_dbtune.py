"""MySQL 配置体检的评估规则（纯函数，不连库）。

每条规则的两边都要验：该提醒时提醒，不该提醒时不能乱喊——体检如果天天报警，
大家很快就学会了无视它，真有问题时也没人看。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MB, GB = 1024 * 1024, 1024 * 1024 * 1024


@pytest.fixture(scope="module")
def dbtune():
    spec = importlib.util.spec_from_file_location("dbtune", ROOT / "deploy" / "scripts" / "dbtune.py")
    m = importlib.util.module_from_spec(spec)
    # dataclass 要在 sys.modules 里按模块名找到自己，不先登记会报 'NoneType' has no attribute '__dict__'
    sys.modules["dbtune"] = m
    spec.loader.exec_module(m)
    return m


def run(dbtune, **kw):
    base = dict(variables={"innodb_buffer_pool_size": str(128 * MB), "max_connections": "300",
                           "innodb_redo_log_capacity": str(100 * MB),
                           "slow_query_log": "ON", "long_query_time": "0.5"},
                status={"Max_used_connections": "20"}, dataset_bytes=50 * MB,
                java_pool_total=40, python_pool_per_process=20)
    variables = {**base.pop("variables"), **kw.pop("variables", {})}
    status = {**base.pop("status"), **kw.pop("status", {})}
    return dbtune.assess(variables=variables, status=status, **{**base, **kw})


def by_title(findings, needle):
    hits = [f for f in findings if needle in f.title]
    assert len(hits) == 1, [f.title for f in findings]
    return hits[0]


class TestBufferPool:
    def test_small_dataset_fits_and_is_not_nagged(self, dbtune):
        f = by_title(run(dbtune), "buffer pool")
        assert f.level == "ok"

    def test_dataset_beyond_80_percent_of_the_pool_warns_with_a_command(self, dbtune):
        f = by_title(run(dbtune, dataset_bytes=200 * MB), "buffer pool")
        assert f.level == "warn"
        assert "SET PERSIST innodb_buffer_pool_size = 1073741824" in f.detail, "建议值至少 1GB"
        assert "103ms" in f.detail and "6.2ms" in f.detail, "要带上实测依据，而不是只说'调大'"

    def test_boundary_is_strict(self, dbtune):
        """正好 80% 不报：规则是"超过"。"""
        f = by_title(run(dbtune, dataset_bytes=128 * MB * 0.8), "buffer pool")
        assert f.level == "ok"

    def test_big_dataset_gets_a_proportional_recommendation(self, dbtune):
        f = by_title(run(dbtune, variables={"innodb_buffer_pool_size": str(1 * GB)},
                         dataset_bytes=2 * GB), "buffer pool")
        assert f.level == "warn" and f"= {3 * GB}" in f.detail          # 2GB × 1.5

    @pytest.mark.parametrize("dataset_mb", [10, 100, 700, 1200, 5000])
    def test_recommendation_is_a_multiple_of_the_chunk_and_at_least_1_5x(self, dbtune, dataset_mb):
        rec = dbtune.recommended_pool_bytes(dataset_mb * MB)
        assert rec % (128 * MB) == 0, "不是 chunk 的整数倍会被 InnoDB 向上取整，设的和实际得到的对不上"
        assert rec >= max(1 * GB, dataset_mb * MB * 1.5)
        assert rec - max(1 * GB, dataset_mb * MB * 1.5) < 128 * MB, "取整多给了不止一个 chunk"

    def test_missing_variable_does_not_crash_or_invent_a_finding(self, dbtune):
        out = dbtune.assess(variables={"max_connections": "300", "slow_query_log": "ON", "long_query_time": "0.5"},
                            status={}, dataset_bytes=10 * GB, java_pool_total=40, python_pool_per_process=20)
        assert not [f for f in out if "buffer pool" in f.title]


class TestConnectionBudget:
    def test_comfortable_budget_is_ok_and_states_its_assumption(self, dbtune):
        f = by_title(run(dbtune), "连接预算")
        assert f.level == "ok" and "最坏 80" in f.title and "1 个副本" in f.detail

    def test_tight_worst_case_warns(self, dbtune):
        f = by_title(run(dbtune, variables={"max_connections": "100"}), "连接预算")
        assert f.level == "warn" and "最坏需要 80 条" in f.title      # 80 > 100 × 0.7

    def test_observed_peak_warns_even_when_the_estimate_looks_fine(self, dbtune):
        """估算是按配置算的；历史峰值是真实发生过的。后者超了，说明有地方用了预算之外的连接。"""
        f = by_title(run(dbtune, status={"Max_used_connections": "250"}), "连接数历史峰值")
        assert f.level == "warn" and "每个请求新建了连接池" in f.detail

    def test_scaling_replicas_is_reflected(self, dbtune):
        f = by_title(run(dbtune, python_processes=6), "连接预算")           # 40 + 6×20 = 160 > 210? no
        assert "最坏 160" in f.title
        f = by_title(run(dbtune, python_processes=10, variables={"max_connections": "300"}), "连接预算")
        assert f.level == "warn"                                           # 40 + 200 = 240 > 210


class TestSlowQueryLog:
    def test_off_warns_with_the_command(self, dbtune):
        f = by_title(run(dbtune, variables={"slow_query_log": "OFF"}), "慢查询日志")
        assert f.level == "warn" and "SET PERSIST slow_query_log = ON" in f.detail

    def test_default_threshold_is_too_high_to_catch_anything_here(self, dbtune):
        f = by_title(run(dbtune, variables={"long_query_time": "10.000000"}), "慢查询日志")
        assert f.level == "warn" and "long_query_time = 0.5" in f.detail

    def test_reasonable_setup_is_ok(self, dbtune):
        assert by_title(run(dbtune), "慢查询日志").level == "ok"

    def test_numeric_on_value_is_understood(self, dbtune):
        assert by_title(run(dbtune, variables={"slow_query_log": "1"}), "慢查询日志").level == "ok"


class TestRedoCapacity:
    """这条规则改过一次。第一版断言"不建议改 redo"，依据是无并发写入时归档速度没变、
    Innodb_log_waits 是 0——错的：它没看并发写入的尾延迟（p99 6ms → 415~616ms），
    而 Innodb_log_waits 统计的是 log buffer 等待，不反映 checkpoint 压力。"""

    def test_small_redo_with_a_big_dataset_warns_and_cites_the_tail_latency(self, dbtune):
        f = by_title(run(dbtune, dataset_bytes=1 * GB), "redo")
        assert f.level == "warn"
        assert "SET PERSIST innodb_redo_log_capacity = 1073741824" in f.detail
        assert "p99" in f.detail and "616ms" in f.detail, "要带上实测的尾延迟，而不是只说'调大'"
        assert "Innodb_log_waits" in f.detail, "要点明那个看起来相关、其实看不出来的计数器"
        assert "--batch-size 500 --sleep 0.1" in f.detail, "要给出不改配置的替代办法"

    def test_small_dataset_is_not_nagged(self, dbtune):
        """小库不做批量维护，天天报警只会让人学会无视体检。"""
        assert by_title(run(dbtune, dataset_bytes=50 * MB), "redo").level == "ok"

    def test_large_redo_is_ok_even_with_a_big_dataset(self, dbtune):
        f = by_title(run(dbtune, dataset_bytes=2 * GB, variables={"innodb_redo_log_capacity": str(1 * GB)}), "redo")
        assert f.level == "ok"

    def test_threshold_is_512mb(self, dbtune):
        assert by_title(run(dbtune, dataset_bytes=1 * GB,
                            variables={"innodb_redo_log_capacity": str(511 * MB)}), "redo").level == "warn"
        assert by_title(run(dbtune, dataset_bytes=1 * GB,
                            variables={"innodb_redo_log_capacity": str(512 * MB)}), "redo").level == "ok"

    def test_old_servers_without_the_variable_are_skipped(self, dbtune):
        """innodb_redo_log_capacity 是 8.0.30 才有的。没有这个变量就别编一条建议出来。"""
        out = dbtune.assess(variables={"innodb_buffer_pool_size": str(1 * GB), "max_connections": "300",
                                       "slow_query_log": "ON", "long_query_time": "0.5"},
                            status={}, dataset_bytes=5 * GB, java_pool_total=40, python_pool_per_process=20)
        assert not [f for f in out if "redo" in f.title]


class TestDoctorHelpers:
    @pytest.fixture
    def doctor(self):
        sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
        spec = importlib.util.spec_from_file_location("doctor_mod", ROOT / "deploy" / "scripts" / "doctor.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_java_pool_total_is_read_from_the_yaml_files(self, doctor, tmp_path, monkeypatch):
        def svc(name, body):
            d = tmp_path / "apps" / "java" / name / "src" / "main" / "resources"
            d.mkdir(parents=True)
            (d / "application.yml").write_text(body, encoding="utf-8")
        svc("a", "spring:\n  datasource:\n    hikari:\n      maximum-pool-size: 7\n")
        svc("b", "spring:\n  datasource:\n    url: x\n")           # 没配 hikari：按默认的 10
        svc("gateway", "server:\n  port: 8080\n")                  # 没有数据源：不占连接
        monkeypatch.setattr(doctor, "ROOT", tmp_path)
        assert doctor._java_pool_total() == 17

    def test_real_repo_total_matches_the_four_services(self, doctor):
        assert doctor._java_pool_total() == 40, "改了某个服务的 Hikari 上限，这里的预算估算会跟着变"
