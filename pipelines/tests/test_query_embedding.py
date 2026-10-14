"""查询向量化的缓存与连接复用。

对应在线检索的成本结构：每个用户问题都要向量化一次——一次网络往返，而且原先每次
还要新建一条 TCP + TLS 连接。客服场景里问题高度重复，向量化又是确定性的，所以缓存它
没有一致性代价（缓存检索*结果*才有）。
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from smartmall_pipeline.rag import store as rag_store
from smartmall_pipeline.rag.embedding import DIM, DashScopeEmbedding


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv("EMBED_QUERY_CACHE_SIZE", raising=False)
    rag_store.reset_query_cache()
    yield
    rag_store.reset_query_cache()


class CountingProvider:
    name = "fake/m1"
    dim = 8
    max_batch = 10

    def __init__(self):
        self.calls = 0

    def embed_query(self, text):
        self.calls += 1
        # 故意用不能被 float32 精确表示的值，才测得出"未命中与命中向量一致"
        return [0.1 * (i + 1) + len(text) * 1e-9 for i in range(self.dim)]


class TestQueryCache:
    def test_repeat_query_is_served_from_cache(self):
        p = CountingProvider()
        a = rag_store.embed_query(p, "能退货吗")
        b = rag_store.embed_query(p, "能退货吗")
        assert p.calls == 1
        assert a == b

    def test_miss_and_hit_return_identical_vectors(self):
        """同一句话第一次与第二次拿到的向量必须逐位相同——
        否则相似度会在第 8 位小数上不同，是最难复现的一类问题。"""
        p = CountingProvider()
        first = rag_store.embed_query(p, "发货多久")
        second = rag_store.embed_query(p, "发货多久")
        assert first == second

    def test_different_query_is_a_miss(self):
        p = CountingProvider()
        rag_store.embed_query(p, "a")
        rag_store.embed_query(p, "b")
        assert p.calls == 2

    def test_key_includes_provider_name(self):
        """换模型必须换键：不同模型的向量不可比，拿旧模型缓存的向量去查新模型建的
        索引，相似度就失去意义，而且不报任何错。"""
        p1, p2 = CountingProvider(), CountingProvider()
        p2.name = "fake/m2"
        rag_store.embed_query(p1, "同一句话")
        rag_store.embed_query(p2, "同一句话")
        assert p1.calls == 1 and p2.calls == 1

    def test_failure_is_not_cached(self):
        class Flaky(CountingProvider):
            def embed_query(self, text):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("API 抖了一下")
                return [0.1] * self.dim          # 不走 super()：它会再给 calls 加一次

        p = Flaky()
        with pytest.raises(RuntimeError):
            rag_store.embed_query(p, "q")
        assert len(rag_store.embed_query(p, "q")) == p.dim      # 恢复后能正常取到
        assert p.calls == 2

    def test_wrong_dimension_is_rejected_and_not_cached(self):
        class Bad(CountingProvider):
            def embed_query(self, text):
                self.calls += 1
                return [0.0] * 3

        p = Bad()
        for _ in range(2):
            with pytest.raises(RuntimeError, match="声明 dim"):
                rag_store.embed_query(p, "q")
        assert p.calls == 2, "维度不对的结果被缓存了"

    def test_lru_evicts_oldest(self, monkeypatch):
        monkeypatch.setenv("EMBED_QUERY_CACHE_SIZE", "2")
        rag_store.reset_query_cache()
        p = CountingProvider()
        for q in ("a", "b", "a", "c"):       # a 被刷新过，所以淘汰的是 b
            rag_store.embed_query(p, q)
        calls_before = p.calls
        rag_store.embed_query(p, "a")
        assert p.calls == calls_before, "最近用过的 a 不该被淘汰"
        rag_store.embed_query(p, "b")
        assert p.calls == calls_before + 1, "最久没用的 b 应该已被淘汰"

    def test_size_zero_disables(self, monkeypatch):
        monkeypatch.setenv("EMBED_QUERY_CACHE_SIZE", "0")
        rag_store.reset_query_cache()
        p = CountingProvider()
        rag_store.embed_query(p, "q"); rag_store.embed_query(p, "q")
        assert p.calls == 2

    def test_stored_compactly_as_float32(self):
        """每条 4KB 而不是 Python list[float] 的 ~33KB。"""
        p = CountingProvider(); p.dim = DIM
        p.embed_query = lambda t: [0.5] * DIM
        rag_store.embed_query(p, "q")
        blobs = list(rag_store.query_cache()._data.values())
        assert len(blobs) == 1 and len(blobs[0]) == DIM * 4

    def test_concurrent_access_is_safe(self):
        p = CountingProvider()
        errors = []

        def work():
            try:
                for i in range(200):
                    rag_store.embed_query(p, f"q{i % 20}")
            except Exception as e:      # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=work) for _ in range(16)]
        [t.start() for t in ts]; [t.join(10) for t in ts]
        assert not errors


# ---------------------------------------------------------------- 连接复用

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"             # 默认的 1.0 不保持连接，测不出复用
    posts = 0

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        type(self).posts += 1
        texts = body["input"]["texts"] if isinstance(body["input"], dict) else body["input"]
        data = {"output": {"embeddings": [
            {"text_index": i, "embedding": [1.0] + [0.0] * (DIM - 1)} for i in range(len(texts))]}}
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class _CountingServer(HTTPServer):
    """数一数一共 accept 了多少条 TCP 连接。"""
    connections = 0

    def get_request(self):
        conn, addr = super().get_request()
        type(self).connections += 1
        return conn, addr


@pytest.fixture
def server():
    _Handler.posts = 0
    _CountingServer.connections = 0
    srv = _CountingServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv
    srv.shutdown(); srv.server_close()


class TestConnectionReuse:
    def _provider(self, server, monkeypatch):
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        port = server.server_address[1]
        # 指到网关形态的 base_url 走 OpenAI 兼容协议；桩服务器同时认两种请求体
        p = DashScopeEmbedding(api_key="k", base_url=f"http://127.0.0.1:{port}/v1", native=True)
        p.NATIVE_URL = f"http://127.0.0.1:{port}/native"      # 实例上覆盖，不影响类
        return p

    def test_sequential_queries_share_one_connection(self, server, monkeypatch):
        """原先每次调用都新建 httpx.Client：N 个问题 = N 次 TCP（对 HTTPS 还要 N 次 TLS）握手。"""
        p = self._provider(server, monkeypatch)
        for i in range(8):
            p.embed_query(f"问题{i}")
        assert _Handler.posts == 8
        assert _CountingServer.connections == 1, \
            f"8 个请求建了 {_CountingServer.connections} 条连接，没有复用"
        p.close()

    def test_next_request_works_after_the_pooled_socket_died(self, server, monkeypatch):
        """复用的长连接死掉之后，下一次请求仍然成功。

        **这条测试验证的是 httpx 自己的行为，不是 ``_post`` 的重试。**复用前 httpcore
        会先检查连接是否已过期（对端关闭会让 socket 变成可读的 EOF），常见情况它自己就
        换了新连接——所以这里用真实 socket 搞死连接，根本走不到 ``_post`` 的重试分支。
        第一版写成"验证重试"，去掉重试后照样全绿：一条抓不到目标缺陷的测试。
        重试要覆盖的是更窄的竞态，见下面用桩客户端写的 TestStaleConnectionRetry。
        """
        p = self._provider(server, monkeypatch)
        p.embed_query("第一次")
        for conn in list(p._http()._transport._pool._connections):
            conn._connection._network_stream._sock.shutdown(socket.SHUT_RDWR)
        assert len(p.embed_query("第二次")) == DIM
        p.close()

    def test_client_is_created_lazily_and_closed_on_close(self, monkeypatch):
        p = DashScopeEmbedding(api_key="k")
        assert p._client is None
        c = p._http()
        assert p._http() is c
        p.close()
        assert p._client is None and c.is_closed

    def test_concurrent_first_use_creates_one_client(self):
        p = DashScopeEmbedding(api_key="k")
        seen = []
        ts = [threading.Thread(target=lambda: seen.append(p._http())) for _ in range(12)]
        [t.start() for t in ts]; [t.join(10) for t in ts]
        assert len({id(c) for c in seen}) == 1
        p.close()


class TestStaleConnectionRetry:
    """``_post`` 的重试：覆盖"httpcore 检查连接可用之后、请求发出之前，对端恰好把它关了"。

    这个窗口很窄，真实 socket 没法确定性地撞进去，所以用桩客户端直接让第一次 post 抛异常。
    """

    class Stub:
        def __init__(self, *errors):
            self.errors = list(errors)
            self.calls = 0

        def post(self, url, headers=None, json=None):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            return "ok"

    def _p(self):
        return DashScopeEmbedding(api_key="k")

    @pytest.mark.parametrize("exc_name", ["RemoteProtocolError", "ReadError"])
    def test_retries_once_on_stale_connection_errors(self, exc_name):
        import httpx

        stub = self.Stub(getattr(httpx, exc_name)("conn closed"))
        assert self._p()._post(stub, "http://x", {}) == "ok"
        assert stub.calls == 2

    def test_does_not_retry_on_timeout(self):
        """超时不重试：重试会把用户的等待翻倍（60 秒读超时 ×2），而且超时多半是对端真的慢。"""
        import httpx

        stub = self.Stub(httpx.ReadTimeout("slow"))
        with pytest.raises(httpx.ReadTimeout):
            self._p()._post(stub, "http://x", {})
        assert stub.calls == 1

    def test_gives_up_after_the_second_failure(self):
        import httpx

        stub = self.Stub(httpx.ReadError("a"), httpx.ReadError("b"))
        with pytest.raises(httpx.ReadError):
            self._p()._post(stub, "http://x", {})
        assert stub.calls == 2, "只重试一次，不无限重试"
