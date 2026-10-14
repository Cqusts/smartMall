"""ai-rag 的并发行为。

对应一次真实压测：``/search`` 是 ``async def``，函数体里却调同步的向量化 API 与
同步的 Milvus 查询。向量化耗时 150ms 时，20 个并发请求总耗时 3.0 秒——正好
20×150ms，完全串行，而且事件循环被冻结了整整 3 秒，``/health`` 也跟着等。
这类缺陷在单请求测试里永远看不出来：每个请求单独跑都是对的。
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app import main as rag
from app.search import SearchService


class SlowStub:
    """向量化一次要 ``delay`` 秒的检索服务桩——同步阻塞，等价于同步 httpx 调 API。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def search(self, query, **kw):
        time.sleep(self.delay)
        return []


@pytest.fixture(autouse=True)
def _reset_service():
    saved = rag._service
    yield
    rag._service = saved


class TestSearchDoesNotBlockTheEventLoop:
    def test_twenty_concurrent_searches_overlap_instead_of_queueing(self):
        """20 个并发 × 150ms：串行是 3.0s，并行应接近 0.15s。

        上界给 1.0s 而不是 0.3s——线程池调度在慢 CI 上会抖，要抓的是 3.0 与 0.15 之间
        那个数量级的差别，不是毫秒级的优劣。
        """
        rag._service = SlowStub(0.15)

        async def run():
            transport = httpx.ASGITransport(app=rag.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                t = time.perf_counter()
                rs = await asyncio.gather(*[
                    c.post("/search", json={"query": f"退货{i}", "top_k": 5})
                    for i in range(20)])
                return time.perf_counter() - t, rs

        wall, rs = asyncio.run(run())
        assert all(r.status_code == 200 for r in rs)
        assert wall < 1.0, f"20 个并发搜索用了 {wall:.2f}s，接近串行的 3.0s"

    def test_event_loop_stays_responsive_while_searching(self):
        """搜索进行时，其它请求（比如 /health）不该被拖住。

        直接量事件循环的延迟：每 10ms 睡一觉，醒来比预定晚了多少，
        就是循环被占住的时长。别用"探 /health 的耗时"去量——那个计时从请求发出
        才开始，漏掉了排队等循环的时间（第一次写的就是这么量的，量出 7ms，
        而真实的冻结是 3 秒）。
        """
        rag._service = SlowStub(0.3)

        async def run():
            lags = []

            async def watch_loop():
                for _ in range(80):
                    want = time.perf_counter() + 0.01
                    await asyncio.sleep(0.01)
                    lags.append(time.perf_counter() - want)

            transport = httpx.ASGITransport(app=rag.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await asyncio.gather(
                    watch_loop(),
                    *[c.post("/search", json={"query": f"q{i}", "top_k": 5}) for i in range(6)])
            return max(lags)

        worst = asyncio.run(run())
        assert worst < 0.15, f"事件循环被占住了 {worst*1000:.0f}ms"

    def test_reload_also_runs_off_the_event_loop(self):
        """/search/reload 要全量扫 Milvus 语料，几秒级的阻塞。"""

        class SlowReload:
            def refresh_stats(self):
                time.sleep(0.4)
                return 7

        rag._service = SlowReload()

        async def run():
            lags = []

            async def watch_loop():
                for _ in range(60):
                    want = time.perf_counter() + 0.01
                    await asyncio.sleep(0.01)
                    lags.append(time.perf_counter() - want)

            transport = httpx.ASGITransport(app=rag.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                _, r = await asyncio.gather(watch_loop(), c.post("/search/reload"))
            return max(lags), r

        worst, r = asyncio.run(run())
        assert r.json()["data"]["documents"] == 7
        assert worst < 0.15, f"重建 IDF 表期间事件循环被占住了 {worst*1000:.0f}ms"


class TestLazyInitRaces:
    def test_service_is_built_once_under_concurrent_first_requests(self, monkeypatch):
        """冷启动的那一刻会有一批请求同时发现 _service is None。

        不加锁就是并发建多个 SearchService：对 Milvus Lite 来说是多个客户端抢
        同一份本地数据文件，对服务端是白白多建一批连接。
        """
        rag._service = None
        built = []

        def slow_build(**kw):
            built.append(1)
            time.sleep(0.2)                 # 拉大窗口，让所有线程都撞进来
            return SlowStub(0)
        monkeypatch.setattr(rag, "build_service", slow_build)

        threads = [threading.Thread(target=rag.get_service) for _ in range(12)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        assert len(built) == 1, f"12 个并发首次请求装配了 {len(built)} 个服务"

    def test_idf_table_is_scanned_once_under_concurrent_first_searches(self):
        """首次检索要全量扫一遍 Milvus 建 IDF 表（几秒）。并发首次检索不能各扫一遍。"""
        scans = []

        class FakeClient:
            def query(self, **kw):
                scans.append(1)
                time.sleep(0.1)
                return [{"text": "七天无理由退换"}]

        svc = SearchService(
            store=SimpleNamespace(client=FakeClient(), cfg=SimpleNamespace(collection="c")),
            provider=None)

        threads = [threading.Thread(target=svc._ensure_stats) for _ in range(12)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        assert len(scans) == 1, f"12 个并发首次检索扫描语料 {len(scans)} 次"
        assert svc.stats is not None
