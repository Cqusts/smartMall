"""``from_env()`` 共享连接池。不需要真的连 MySQL：Engine 是惰性的，建出来不连库。"""

from __future__ import annotations

import pytest

from smartmall_pipeline import repository as repo


@pytest.fixture(autouse=True)
def _clean_registry():
    repo.dispose_shared_engines()
    yield
    repo.dispose_shared_engines()


class TestEngineSharing:
    def test_from_env_calls_share_one_engine(self):
        """调用方有多少个，池子就有多少个——这是被压测量出来的缺陷：
        /api/products 每个请求 from_env() 一次，MySQL 的 Connections 每请求 +1。"""
        a = repo.DwsRepository.from_env()
        b = repo.DwsRepository.from_env()
        c = repo.OdsRepository.from_env()
        assert a.engine is b.engine is c.engine

    def test_different_dsn_gets_different_engine(self, monkeypatch):
        """配置变了必须拿到新池子，而不是继续连着旧库。
        （测试里 monkeypatch MYSQL_PORT 去造一个必然失败的连接，靠的就是这点。）"""
        a = repo.DwsRepository.from_env()
        monkeypatch.setenv("MYSQL_PORT", "1")
        b = repo.DwsRepository.from_env()
        assert a.engine is not b.engine

    def test_direct_construction_stays_isolated(self):
        """直接构造不共享。测试里的 sqlite 内存库靠"一个实例一个库"互相隔离，
        共享了会让一个测试往另一个测试的库里写。"""
        a = repo.DwsRepository(dsn="sqlite://")
        b = repo.DwsRepository(dsn="sqlite://")
        assert a.engine is not b.engine

    def test_pool_size_is_configurable(self, monkeypatch):
        monkeypatch.setenv("MYSQL_POOL_SIZE", "3")
        monkeypatch.setenv("MYSQL_MAX_OVERFLOW", "2")
        monkeypatch.setenv("MYSQL_POOL_RECYCLE", "600")
        pool = repo.DwsRepository.from_env().engine.pool
        assert pool.size() == 3
        assert pool._max_overflow == 2
        assert pool._recycle == 600

    def test_bad_env_value_falls_back_to_default(self, monkeypatch):
        """环境变量写成 "abc" 不该让服务起不来。"""
        monkeypatch.setenv("MYSQL_POOL_SIZE", "abc")
        assert repo.DwsRepository.from_env().engine.pool.size() == 10

    def test_dispose_clears_the_registry(self):
        a = repo.DwsRepository.from_env()
        repo.dispose_shared_engines()
        b = repo.DwsRepository.from_env()
        assert a.engine is not b.engine

    def test_dsn_changes_with_env(self, monkeypatch):
        before = repo.dsn_from_env()
        monkeypatch.setenv("MYSQL_HOST", "db.example")
        assert repo.dsn_from_env() != before
