"""生产检索路径：ai-agent 的 HttpRetriever ↔ ai-rag 的 /search。

**这条路径原先一个测试都没有，而且在服务端根本没接上**：``get_deps()`` 不传
``rag_url``，对话永远走进程内暴力检索，compose 里的 ai-rag 与 Milvus 虽然起着，
却从来不在查询链路上。"生产路径"只存在于文档与一个无人调用的类里。

契约测试是跨进程的：真的起一个 ai-rag（uvicorn 子进程，检索服务换成吐真实
``ScoredHit`` 的桩），HttpRetriever 通过真实 HTTP 去调。两边各自的单元测试都能全绿
而字段名对不上——这类缺陷只有把两头接起来才暴露。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.agent.retriever import HttpRetriever, RetrievalError

RAG_DIR = Path(__file__).resolve().parents[2] / "ai-rag"

LAUNCHER = textwrap.dedent('''
    import sys
    sys.path.insert(0, {rag_dir!r})
    import uvicorn
    import app.main as m
    from app.search import ScoredHit, SearchError

    class Svc:
        def search(self, query, *, top_k=5, product_ids=None, category_id=None, biz_types=None):
            if query == "BOOM":
                raise SearchError("Milvus 连不上")
            if query == "EMPTY":
                return []
            return [ScoredHit(
                item_id=7, chunk_id=7000, title="退货规则",
                content="退货规则\\n七天内可退|pid=" + str(list(product_ids or [])) + "|cat=" + str(category_id),
                score=0.0328, dense_score=0.71, bm25_score=3.2, lexical_overlap=0.8,
                biz_type="qa", modality="text", asset_ids=(11, 12),
                product_ids=tuple(product_ids or ()),
            )]

    m._service = Svc()
    uvicorn.run(m.app, host="127.0.0.1", port={port}, log_level="error")
''')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def rag_url():
    if not RAG_DIR.is_dir():
        pytest.skip("缺 apps/python/ai-rag")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", LAUNCHER.format(rag_dir=str(RAG_DIR), port=port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail("ai-rag 子进程退出了：" + proc.stderr.read().decode()[-800:])
        try:
            if httpx.get(url + "/health", timeout=1).status_code == 200:
                break
        except Exception:               # noqa: BLE001
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("ai-rag 子进程 30 秒内没起来")
    yield url
    proc.terminate()
    proc.wait(10)


class TestContract:
    def test_hit_fields_survive_the_round_trip(self, rag_url):
        """ai-rag 吐的 ScoredHit.to_json() 与 HttpRetriever 读的键必须一一对上。
        对不上时不报错，只是判据悄悄变成默认值——闸门失效。"""
        r = HttpRetriever(rag_url)
        [c] = r.search("能退货吗")
        assert c.item_id == 7
        assert c.title == "退货规则"
        assert "七天内可退" in c.content
        assert c.dense_score == pytest.approx(0.71)       # 拒答阈值用的是它，不是 RRF 分
        assert c.score == pytest.approx(0.0328)
        assert c.bm25_score == pytest.approx(3.2)
        assert c.lexical_overlap == pytest.approx(0.8)    # 缺了会落到 -1.0 的弱判据
        assert c.asset_ids == (11, 12)
        r.close()

    def test_filters_are_forwarded(self, rag_url):
        """product_ids 决定检索过滤范围；丢了就会拿 A 商品的知识回答 B 商品。

        **必须通过 HttpRetriever.search 去观察。**第一版直接 post 到 ai-rag，测的是桩的
        回显——把 HttpRetriever 里转发过滤条件的那几行删掉，测试照样全绿。
        现在桩把"收到了什么"写进返回内容里，经由 search() 的返回值读出来。
        """
        r = HttpRetriever(rag_url)
        [c] = r.search("q", product_ids=[9001, 9002], category_id=1024)
        assert "pid=[9001, 9002]" in c.content, c.content
        assert "cat=1024" in c.content, c.content

        [c] = r.search("q")                      # 没给过滤条件就不该凭空带上
        assert "pid=[]" in c.content and "cat=None" in c.content, c.content
        r.close()

    def test_zero_hits_is_an_answer_not_an_error(self, rag_url):
        """检索到 0 条是确定的结论（知识库里确实没有）；失败是"不知道"。
        两者混在一起，Agent 就会拿"没找到相关信息"回复用户——把故障说成事实。"""
        r = HttpRetriever(rag_url)
        assert r.search("EMPTY") == []
        r.close()

    def test_backend_failure_becomes_retrieval_error(self, rag_url):
        """ai-rag 报 503（Milvus 连不上）必须变成 RetrievalError，让 Agent 走转人工。"""
        r = HttpRetriever(rag_url)
        with pytest.raises(RetrievalError, match="503"):
            r.search("BOOM")
        r.close()


class TestFailureModes:
    def test_unreachable_ai_rag_fails_fast(self):
        """ai-rag 没起来时应该很快失败并转人工，而不是让用户干等读超时。"""
        r = HttpRetriever(f"http://127.0.0.1:{_free_port()}", timeout=10.0)
        t = time.perf_counter()
        with pytest.raises(RetrievalError, match="无法连接"):
            r.search("q")
        assert time.perf_counter() - t < 3.5
        r.close()

    def test_connect_timeout_is_shorter_than_read_timeout(self):
        r = HttpRetriever("http://x", timeout=10.0)
        t = r._http().timeout
        assert t.connect == 3.0 and t.read == 10.0
        r.close()


class TestConnectionReuse:
    def test_sequential_searches_share_one_connection(self, rag_url):
        """原先每次 httpx.post——每个用户问题都新建一条到 ai-rag 的 TCP 连接。"""
        r = HttpRetriever(rag_url)
        for i in range(10):
            r.search(f"问题{i}")
        pool = r._http()._transport._pool
        assert len(pool._connections) == 1, f"10 次检索留下了 {len(pool._connections)} 条连接"
        r.close()

    def test_concurrent_first_use_creates_one_client(self):
        r = HttpRetriever("http://x")
        seen = []
        ts = [threading.Thread(target=lambda: seen.append(r._http())) for _ in range(12)]
        [t.start() for t in ts]; [t.join(10) for t in ts]
        assert len({id(c) for c in seen}) == 1
        r.close()


class TestStaleConnectionRetry:
    class Stub:
        def __init__(self, *errors):
            self.errors, self.calls = list(errors), 0

        def post(self, url, json=None):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            return SimpleNamespace(status_code=200, json=lambda: {"data": {"hits": []}}, text="")

    def _retriever(self, stub):
        r = HttpRetriever("http://x")
        r._client = stub
        return r

    @pytest.mark.parametrize("name", ["RemoteProtocolError", "ReadError"])
    def test_retries_once_on_stale_connection(self, name):
        stub = self.Stub(getattr(httpx, name)("closed"))
        assert self._retriever(stub).search("q") == []
        assert stub.calls == 2

    def test_timeout_is_not_retried(self):
        """重试会把用户的等待翻倍。"""
        stub = self.Stub(httpx.ReadTimeout("slow"))
        with pytest.raises(RetrievalError):
            self._retriever(stub).search("q")
        assert stub.calls == 1


# ---------------------------------------------------------------- 后端开关

class TestBackendSwitch:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        import app.routers.chat as chat
        monkeypatch.setattr(chat, "_deps", None)
        self.chat = chat

    def _capture(self, monkeypatch, backend):
        import app.agent.assembly as assembly
        from app.config import settings

        seen = {}
        monkeypatch.setattr(assembly, "build_deps", lambda **kw: seen.update(kw) or object())
        monkeypatch.setattr(settings, "retriever_backend", backend)
        monkeypatch.setattr(settings, "rag_base_url", "http://ai-rag:9001")
        self.chat.get_deps()
        return seen

    def test_http_backend_passes_rag_url(self, monkeypatch):
        """这是原先缺的那一行：不传 rag_url，服务端就永远走本地暴力检索。"""
        assert self._capture(monkeypatch, "http")["rag_url"] == "http://ai-rag:9001"

    def test_local_backend_passes_none(self, monkeypatch):
        assert self._capture(monkeypatch, "local")["rag_url"] is None

    def test_typo_in_backend_name_fails_at_startup(self):
        """RETRIEVER_BACKEND=htp 不能悄悄退回 local——那会让人以为生产在走 Milvus。"""
        from pydantic import ValidationError

        from app.config import Settings

        with pytest.raises(ValidationError):
            Settings(retriever_backend="htp")

    def test_default_is_local_for_dev_experience(self):
        from app.config import Settings
        assert Settings().retriever_backend == "local"


class TestHealthRequirement:
    class Reg:
        def __init__(self):
            self.required = {}

        def register(self, name, check, *, required=True, timeout=3.0):
            self.required[name] = required

    def _reg(self, backend):
        from app.main import register_dependencies

        reg = self.Reg()
        register_dependencies(reg, SimpleNamespace(
            litellm_base_url="http://l", rag_base_url="http://r", retriever_backend=backend,
            redis_host="h", redis_port=1, kafka_bootstrap_servers="k:9092"))
        return reg.required

    def test_ai_rag_required_only_when_it_is_actually_used(self):
        """local 后端根本不调 ai-rag，它挂了不该让 /ready 变红、被编排系统摘掉流量。"""
        assert self._reg("http")["ai-rag"] is True
        assert self._reg("local")["ai-rag"] is False


class TestInfoExposesBackend:
    def test_info_reports_which_retriever_is_live(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.config import settings
        from app.main import app

        monkeypatch.setattr(settings, "retriever_backend", "http")
        assert TestClient(app).get("/info").json()["retriever"] == "http"
