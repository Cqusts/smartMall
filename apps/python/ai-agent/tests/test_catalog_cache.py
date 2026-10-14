"""商品列表缓存与 /api/products 的并发行为。

这组测试对应压测里量出来的三个缺陷：吞吐固定 5.2 req/s（每请求重建）、
并发时事件循环被占住（``/health`` 最大等了 13 秒）、每请求新建一条 MySQL 连接。
每一条都要**能在缺陷存在时变红**——这个项目里测试和缺陷"擦肩而过"不是第一次了。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
import time

import pytest

from app.catalog_cache import CatalogCache, etag_matches


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make(payloads, **kw):
    """每次重建依次吐出 payloads 里的下一个；用完了重复最后一个。"""
    calls = []

    def build():
        calls.append(1)
        return payloads[min(len(calls), len(payloads)) - 1]

    clock = FakeClock()
    return CatalogCache(build, clock=clock, **kw), clock, calls


OK1 = {"ok": True, "items": [{"id": 1, "stock": 5}]}
OK2 = {"ok": True, "items": [{"id": 1, "stock": 4}]}
BAD = {"ok": False, "items": [], "error": "OperationalError", "detail": "x: y"}


class TestTtl:
    def test_second_read_within_ttl_does_not_rebuild(self):
        c, _, calls = make([OK1], ttl=5)
        a, b = c.get("k"), c.get("k")
        assert a is b and len(calls) == 1

    def test_expires_after_ttl(self):
        c, clock, calls = make([OK1, OK2], ttl=5)
        first = c.get("k")
        clock.advance(5.1)
        second = c.get("k")
        assert len(calls) == 2
        assert first.body != second.body

    def test_ttl_zero_disables_the_cache(self):
        c, _, calls = make([OK1], ttl=0)
        c.get("k"); c.get("k"); c.get("k")
        assert len(calls) == 3

    def test_different_key_is_a_miss(self):
        """连库配置变了，旧缓存不作数——否则换了库还在返回上一个库的商品。"""
        c, _, calls = make([OK1, OK2], ttl=5)
        c.get("db-a")
        c.get("db-b")
        assert len(calls) == 2


class TestInvalidate:
    def test_invalidate_forces_rebuild_before_ttl(self):
        c, _, calls = make([OK1, OK2], ttl=5)
        c.get("k")
        c.invalidate()
        after = c.get("k")
        assert len(calls) == 2
        assert json.loads(after.body)["items"][0]["stock"] == 4

    def test_invalidation_during_a_build_is_not_lost(self):
        """重建途中发生了作废：这份结果是"作废之前"的数据，不能被当成新的留下来。

        版本号必须在开始重建**之前**取。写成建完之后才取，这条会红——
        刚下完单的用户会在 TTL 内一直看到没扣库存的页面。
        """
        started, release = threading.Event(), threading.Event()
        calls = []

        def build():
            calls.append(1)
            if len(calls) == 1:
                started.set()
                release.wait(5)               # 卡在第一次重建中途
                return OK1                    # 这是"作废之前"的数据
            return OK2

        c = CatalogCache(build, ttl=60)
        t = threading.Thread(target=lambda: c.get("k"))
        t.start()
        assert started.wait(5)
        c.invalidate()                        # 重建进行中，数据变了
        release.set()
        t.join(5)

        latest = c.get("k")
        assert len(calls) == 2, "作废发生在重建途中，下一次读取必须重建"
        assert json.loads(latest.body)["items"][0]["stock"] == 4


class TestFailureHandling:
    def test_failure_is_cached_briefly_so_waiters_do_not_each_pay_the_timeout(self):
        c, clock, calls = make([BAD, OK1], ttl=60, fail_ttl=2)
        assert c.get("k").ok is False
        assert c.get("k").ok is False
        assert len(calls) == 1, "库连不上时，每次读取都去重试会让排队请求串行各等一遍超时"

    def test_failure_expires_quickly_so_recovery_is_not_delayed(self):
        c, clock, calls = make([BAD, OK1], ttl=60, fail_ttl=2)
        c.get("k")
        clock.advance(2.1)
        assert c.get("k").ok is True, "库恢复后要在 fail_ttl 内就能看到，而不是等满 ttl"

    def test_failed_snapshot_has_no_usable_etag_semantics(self):
        c, _, _ = make([BAD])
        assert c.get("k").ok is False       # 端点据此决定不发 ETag


class TestSingleFlight:
    def test_thirty_concurrent_readers_trigger_one_rebuild(self):
        """过期的那一刻 30 个请求同时到：只该重建一次。

        没有单飞的话这里是 30 次——缓存在流量最大的时候恰恰形同虚设。
        """
        gate = threading.Event()
        count = 0
        lock = threading.Lock()

        def build():
            nonlocal count
            gate.wait(5)                      # 让所有线程都挤到重建点上
            with lock:
                count += 1
            time.sleep(0.05)
            return OK1

        c = CatalogCache(build, ttl=60)
        results = []
        threads = [threading.Thread(target=lambda: results.append(c.get("k")))
                   for _ in range(30)]
        [t.start() for t in threads]
        time.sleep(0.1)
        gate.set()
        [t.join(10) for t in threads]

        assert count == 1, f"30 个并发读取重建了 {count} 次"
        assert len(results) == 30
        assert len({id(r) for r in results}) == 1, "所有人应拿到同一份快照"


class TestSerialization:
    def test_gzip_roundtrips_to_the_same_json(self):
        snap = make([OK1])[0].get("k")
        assert json.loads(gzip.decompress(snap.gz)) == json.loads(snap.body) == OK1

    def test_chinese_is_not_escaped(self):
        """ensure_ascii=False：中文按原样出去，体积比 \\uXXXX 小得多，
        也和 FastAPI 原先的 JSONResponse 行为一致。"""
        snap = make([{"ok": True, "items": [{"name": "米白色针织衫"}]}])[0].get("k")
        assert "米白色针织衫".encode() in snap.body

    def test_etag_is_stable_when_content_is_unchanged(self):
        """TTL 到期重建出的内容没变，ETag 就不该变——浏览器才能继续 304。"""
        c, clock, _ = make([OK1, OK1], ttl=5)
        a = c.get("k").etag
        clock.advance(6)
        assert c.get("k").etag == a

    def test_etag_changes_when_content_changes(self):
        c, clock, _ = make([OK1, OK2], ttl=5)
        a = c.get("k").etag
        clock.advance(6)
        assert c.get("k").etag != a


class TestEtagMatching:
    @pytest.mark.parametrize("header,expected", [
        ('W/"abc"', True), ('"abc"', True), ('W/"x", W/"abc"', True),
        ('W/"zzz"', False), ("*", True), ("", False), (None, False),
    ])
    def test_match(self, header, expected):
        assert etag_matches(header, 'W/"abc"') is expected


# ---------------------------------------------------------------- 端点

class TestEndpoint:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        import app.routers.ws as ws
        ws.reset_catalog_cache()
        self.ws = ws
        yield
        ws.reset_catalog_cache()

    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)

    def _patch(self, monkeypatch, payload, counter=None):
        def build():
            if counter is not None:
                counter.append(1)
            return payload
        monkeypatch.setattr(self.ws, "_build_catalog_payload", build)

    def test_serves_gzip_when_accepted_and_json_otherwise(self, monkeypatch):
        self._patch(monkeypatch, OK1)
        c = self._client()
        gz = c.get("/api/products", headers={"Accept-Encoding": "gzip"})
        assert gz.headers["content-encoding"] == "gzip"
        assert gz.json() == OK1                      # TestClient 自动解压

        plain = c.get("/api/products", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers
        assert plain.json() == OK1

    def test_conditional_request_gets_304_without_a_body(self, monkeypatch):
        self._patch(monkeypatch, OK1)
        c = self._client()
        first = c.get("/api/products")
        etag = first.headers["etag"]
        again = c.get("/api/products", headers={"If-None-Match": etag})
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["etag"] == etag

    def test_changed_data_invalidates_the_etag(self, monkeypatch):
        self._patch(monkeypatch, OK1)
        c = self._client()
        etag = c.get("/api/products").headers["etag"]
        self._patch(monkeypatch, OK2)
        self.ws.get_catalog_cache().invalidate()
        r = c.get("/api/products", headers={"If-None-Match": etag})
        assert r.status_code == 200
        assert r.json()["items"][0]["stock"] == 4

    def test_failure_is_200_with_reason_and_never_cached_by_clients(self, monkeypatch):
        """既有契约：库挂了仍是 200 + ok:false + detail，页面退化为只有客服入口。"""
        self._patch(monkeypatch, BAD)
        r = self._client().get("/api/products")
        assert r.status_code == 200
        assert r.json()["ok"] is False and r.json()["detail"]
        assert r.headers["cache-control"] == "no-store"
        assert "etag" not in r.headers

    def test_repeat_requests_hit_the_cache(self, monkeypatch):
        n = []
        self._patch(monkeypatch, OK1, n)
        c = self._client()
        for _ in range(20):
            c.get("/api/products")
        assert len(n) == 1, f"20 次请求重建了 {len(n)} 次"

    def test_successful_order_invalidates_the_catalog(self, monkeypatch):
        """用户自己刚买完，页面上的库存必须马上变——这是最容易被注意到的不一致。"""
        n = []
        self._patch(monkeypatch, OK1, n)
        c = self._client()
        c.get("/api/products")
        assert len(n) == 1

        # 不 mock _forward 本身——要验证的正是它里面"写成功就作废"这一段。
        # 所以 mock 的是它底下的 HTTP 调用。
        import httpx

        class FakeResp:
            status_code = 200
            def json(self): return {"code": 0, "data": {}}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def request(self, *a, **k): return FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        r = c.post("/api/orders", json={"requestId": "r1", "skuNo": "S", "quantity": 1})
        assert r.json()["code"] == 0

        c.get("/api/products")
        assert len(n) == 2, "下单成功后下一次读取必须重建"

    def test_failed_order_does_not_invalidate(self, monkeypatch):
        n = []
        self._patch(monkeypatch, OK1, n)
        c = self._client()
        c.get("/api/products")

        import httpx

        class FakeResp:
            status_code = 200
            def json(self): return {"code": 2401, "message": "库存不足", "data": None}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def request(self, *a, **k): return FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        c.post("/api/orders", json={"requestId": "r2", "skuNo": "S", "quantity": 1})
        c.get("/api/products")
        assert len(n) == 1, "失败的下单没有改变任何东西，作废只是白白多一次重建"


class TestEventLoopStaysFree:
    """压测里最伤的一条：并发 30 时 ``/health`` 最大等了 13 秒。"""

    def test_slow_catalog_build_does_not_freeze_other_requests(self, monkeypatch):
        import httpx

        import app.routers.ws as ws
        from app.main import app

        ws.reset_catalog_cache()

        def slow_build():
            time.sleep(0.4)                   # 同步阻塞，等价于一次慢查询
            return OK1
        monkeypatch.setattr(ws, "_build_catalog_payload", slow_build)

        async def run():
            lags = []

            async def watch_loop():
                # 每 10ms 睡一觉，醒来比预定晚了多少 = 事件循环被占住了多久
                for _ in range(60):
                    want = time.perf_counter() + 0.01
                    await asyncio.sleep(0.01)
                    lags.append(time.perf_counter() - want)

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await asyncio.gather(
                    watch_loop(),
                    *[c.get("/api/products") for _ in range(8)],
                )
            return max(lags)

        worst = asyncio.run(run())
        ws.reset_catalog_cache()
        # 重建要 0.4s。事件循环若被占住，worst 会接近 0.4；放进线程池后只剩调度抖动
        assert worst < 0.15, f"事件循环被占住了 {worst*1000:.0f}ms"
