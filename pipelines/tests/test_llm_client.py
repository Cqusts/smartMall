"""LLM 客户端的错误分类与降级。

这一层没有单测时踩过两次坑：一次是 386 条对话全部 ``LocalProtocolError``
（Key 里带了换行），一次是 385 条全部 4xx 而错误正文被吞掉。两次都被当成
"这些数据没有可复用知识"标记为已处理，数据等于丢了。

分类规则只有一条，但必须守死：
**业务结论只能来自 200 响应。** 其余一律是故障，区别只在于重试有没有用。
"""

from __future__ import annotations

import httpx
import pytest

from smartmall_pipeline.gates import gate3_model as g3


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


def _client(monkeypatch, responses: list, calls: list | None = None):
    """把 httpx.post 换成按序返回预设响应的桩。"""
    seq = iter(responses)

    def fake_post(url, **kw):
        if calls is not None:
            calls.append(kw.get("json"))
        nxt = next(seq)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(httpx, "post", fake_post)
    return g3.LiteLlmClient(base_url="http://gw:9000", api_key="sk-test")


def _ok(content: str = '{"ok": true}') -> _FakeResponse:
    return _FakeResponse(
        200, payload={"choices": [{"message": {"content": content}}]}
    )


# ---------------------------------------------------------------- 错误分类


class TestErrorClassification:
    def test_5xx_is_retryable(self, monkeypatch):
        c = _client(monkeypatch, [_FakeResponse(503, "upstream down")])
        with pytest.raises(g3.LlmUnavailableError):
            c.complete_json(model="m", system="s", user="u")

    def test_429_is_retryable(self, monkeypatch):
        c = _client(monkeypatch, [_FakeResponse(429, "rate limited")])
        with pytest.raises(g3.LlmUnavailableError):
            c.complete_json(model="m", system="s", user="u")

    def test_401_is_config_error(self, monkeypatch):
        """认证失败重试一万次也不会成功，不能归为可重试。"""
        c = _client(monkeypatch, [_FakeResponse(401, "invalid api key")])
        with pytest.raises(g3.LlmConfigError) as exc:
            c.complete_json(model="m", system="s", user="u")
        assert "401" in str(exc.value)
        assert "invalid api key" in str(exc.value)

    def test_404_model_not_found_is_config_error(self, monkeypatch):
        c = _client(monkeypatch, [_FakeResponse(404, "model not found")])
        with pytest.raises(g3.LlmConfigError):
            c.complete_json(model="m", system="s", user="u")

    def test_connection_error_is_retryable(self, monkeypatch):
        c = _client(monkeypatch, [httpx.ConnectError("refused")])
        with pytest.raises(g3.LlmUnavailableError) as exc:
            c.complete_json(model="m", system="s", user="u")
        assert "http://gw:9000" in str(exc.value), "要带上地址，否则查不出配错了哪"

    def test_error_body_is_surfaced_not_swallowed(self, monkeypatch):
        """厂商的错误正文是唯一的诊断线索，必须原样透出。"""
        body = '{"code":"InvalidParameter","message":"response_format not supported"}'
        c = _client(monkeypatch, [_FakeResponse(400, body), _FakeResponse(400, body)])
        with pytest.raises(g3.LlmConfigError) as exc:
            c.complete_json(model="m", system="s", user="u")
        assert "InvalidParameter" in str(exc.value)
        assert "response_format not supported" in str(exc.value)


# ---------------------------------------------------------------- 降级


class TestResponseFormatFallback:
    def test_400_disables_response_format_and_retries(self, monkeypatch):
        """不支持 response_format 不该让整批失败——关掉再试一次。"""
        calls: list = []
        c = _client(monkeypatch, [_FakeResponse(400, "bad param"), _ok()], calls)
        assert c.complete_json(model="m", system="s", user="u") == {"ok": True}

        assert len(calls) == 2
        assert "response_format" in calls[0]
        assert "response_format" not in calls[1]

    def test_fallback_is_sticky(self, monkeypatch):
        """降级一次后不必每条都白试一遍。"""
        calls: list = []
        c = _client(
            monkeypatch, [_FakeResponse(400, "bad param"), _ok(), _ok()], calls
        )
        c.complete_json(model="m", system="s", user="u")
        c.complete_json(model="m", system="s", user="u")
        assert len(calls) == 3
        assert "response_format" not in calls[2]

    def test_fallback_not_retried_on_second_400(self, monkeypatch):
        """降级后仍 400 说明不是这个参数的问题，别无限重试。"""
        calls: list = []
        c = _client(
            monkeypatch, [_FakeResponse(400, "x"), _FakeResponse(400, "x")], calls
        )
        with pytest.raises(g3.LlmConfigError):
            c.complete_json(model="m", system="s", user="u")
        assert len(calls) == 2

    def test_prompts_mention_lowercase_json(self):
        """开启 response_format 的服务普遍要求 prompt 里出现小写 json。

        写成大写 JSON 会被判为"没提 json"直接 400——一个纯文本细节
        能让整批清洗失败，因此用测试钉住。
        """
        for prompt in (g3.TRIAGE_SYSTEM, g3.EXTRACT_SYSTEM, g3.STYLE_SYSTEM):
            assert "json" in prompt, f"缺少小写 json：{prompt[:30]}"


# ---------------------------------------------------------------- Key 卫生


class TestApiKeyHygiene:
    def test_whitespace_is_stripped(self):
        """从 .env 或控制台复制来的 Key 常带换行。

        带换行的 header 会让 h11 抛 LocalProtocolError，
        报错信息里完全看不出是 Key 的问题。
        """
        c = g3.LiteLlmClient(base_url="http://x", api_key="  sk-abc\n")
        assert c.api_key == "sk-abc"

    def test_non_ascii_key_fails_early_and_clearly(self):
        c_args = dict(base_url="http://x", api_key="sk-中文引号")
        with pytest.raises(g3.LlmConfigError) as exc:
            g3.LiteLlmClient(**c_args)
        assert "非 ASCII" in str(exc.value)

    def test_base_url_trailing_slash_is_normalized(self):
        c = g3.LiteLlmClient(base_url="http://x:9000/", api_key="k")
        assert c.base_url == "http://x:9000"


class TestDashScopeAliases:
    def test_gateway_aliases_map_to_real_model_names(self, monkeypatch):
        """网关别名在直连模式下必须翻译，否则 DashScope 直接 400。"""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
        calls: list = []

        def fake_post(url, **kw):
            calls.append(kw.get("json"))
            return _ok()

        monkeypatch.setattr(httpx, "post", fake_post)
        c = g3.DashScopeLlmClient()
        c.complete_json(model="chat-light", system="s", user="u")
        assert calls[0]["model"] == "qwen-turbo"

    def test_unknown_model_passes_through(self, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
        calls: list = []
        monkeypatch.setattr(
            httpx, "post", lambda url, **kw: (calls.append(kw.get("json")), _ok())[1]
        )
        c = g3.DashScopeLlmClient()
        c.complete_json(model="qwen3-max", system="s", user="u")
        assert calls[0]["model"] == "qwen3-max"

    def test_missing_key_says_how_to_proceed_without_paying(self, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(g3.LlmError) as exc:
            g3.DashScopeLlmClient()
        assert "--fake-llm" in str(exc.value), "要给出不花钱也能跑通的路径"


# ---------------------------------------------------------------- 模型名可替换


class TestModelOverride:
    """换厂商不该需要改代码。

    默认模型名是网关别名，只有 DashScope 的别名表能翻译。额度用尽、
    厂商涨价、想换本地 vLLM——这些都会发生，而它们都不该逼人去改源码。
    """

    def test_env_overrides_each_stage(self, monkeypatch):
        from smartmall_pipeline.cli import _gate3_config_from_env

        monkeypatch.setenv("SMARTMALL_TRIAGE_MODEL", "deepseek-chat")
        monkeypatch.setenv("SMARTMALL_EXTRACT_MODEL", "deepseek-reasoner")
        monkeypatch.setenv("SMARTMALL_STYLE_MODEL", "glm-4-flash")
        cfg = _gate3_config_from_env()
        assert cfg.triage_model == "deepseek-chat"
        assert cfg.extract_model == "deepseek-reasoner"
        assert cfg.style_model == "glm-4-flash"

    def test_unset_keeps_gateway_aliases(self, monkeypatch):
        from smartmall_pipeline.cli import _gate3_config_from_env

        for k in ("SMARTMALL_TRIAGE_MODEL", "SMARTMALL_EXTRACT_MODEL",
                  "SMARTMALL_STYLE_MODEL"):
            monkeypatch.delenv(k, raising=False)
        cfg = _gate3_config_from_env()
        assert cfg.triage_model == "chat-light"
        assert cfg.extract_model == "chat-default"

    def test_blank_env_is_ignored(self, monkeypatch):
        """.env 里写了空值不该把模型名清空。"""
        from smartmall_pipeline.cli import _gate3_config_from_env

        monkeypatch.setenv("SMARTMALL_TRIAGE_MODEL", "   ")
        assert _gate3_config_from_env().triage_model == "chat-light"

    def test_triage_still_uses_the_cheaper_model(self, monkeypatch):
        """两段式的成本前提：粗筛必须比抽取便宜，别被覆盖搞反。"""
        from smartmall_pipeline.cli import _gate3_config_from_env

        for k in ("SMARTMALL_TRIAGE_MODEL", "SMARTMALL_EXTRACT_MODEL"):
            monkeypatch.delenv(k, raising=False)
        cfg = _gate3_config_from_env()
        assert cfg.triage_model != cfg.extract_model


class TestBackendResolution:
    """通道选择不该在配好厂商之后还撞回默认通道。

    真实场景：.env 里 DashScope 和 DeepSeek 两套都留着（一个额度用尽，
    一个是新换的）。忘了加 --llm openai 就又撞上 403——
    这种坑不该靠人记住。
    """

    def _args(self, llm=None, fake=False):
        import argparse

        return argparse.Namespace(llm=llm, fake_llm=fake)

    def test_explicit_flag_always_wins(self, monkeypatch):
        from smartmall_pipeline.cli import _resolve_llm_backend

        monkeypatch.setenv("SMARTMALL_LLM_BASE_URL", "https://api.deepseek.com")
        assert _resolve_llm_backend(self._args(llm="dashscope")) == "dashscope"

    def test_base_url_implies_openai(self, monkeypatch):
        from smartmall_pipeline.cli import _resolve_llm_backend

        monkeypatch.setenv("SMARTMALL_LLM_BASE_URL", "https://api.deepseek.com")
        assert _resolve_llm_backend(self._args()) == "openai"

    def test_falls_back_to_dashscope(self, monkeypatch):
        from smartmall_pipeline.cli import _resolve_llm_backend

        monkeypatch.delenv("SMARTMALL_LLM_BASE_URL", raising=False)
        assert _resolve_llm_backend(self._args()) == "dashscope"

    def test_blank_base_url_does_not_imply_openai(self, monkeypatch):
        """.env 模板里留的空行不该把通道带偏。"""
        from smartmall_pipeline.cli import _resolve_llm_backend

        monkeypatch.setenv("SMARTMALL_LLM_BASE_URL", "  ")
        assert _resolve_llm_backend(self._args()) == "dashscope"

    def test_fake_beats_everything(self, monkeypatch):
        from smartmall_pipeline.cli import _resolve_llm_backend

        monkeypatch.setenv("SMARTMALL_LLM_BASE_URL", "https://api.deepseek.com")
        assert _resolve_llm_backend(self._args(llm="openai", fake=True)) == "fake"


class TestChatUrl:
    """base_url 该写到哪一层，各家文档给的答案不一样。

    DeepSeek 写 https://api.deepseek.com，OpenAI/Kimi 写到 /v1，
    智谱写到 /api/paas/v4。照抄哪一份都合理，代码得都认。
    """

    def _url(self, base: str) -> str:
        return g3.LiteLlmClient(base_url=base, api_key="k").chat_url

    def test_bare_host_gets_v1(self):
        assert self._url("https://api.deepseek.com") == (
            "https://api.deepseek.com/v1/chat/completions"
        )

    def test_v1_is_not_doubled(self):
        """粘了带 /v1 的地址不该拼成 /v1/v1 然后 404。"""
        assert self._url("https://api.moonshot.cn/v1") == (
            "https://api.moonshot.cn/v1/chat/completions"
        )

    def test_other_version_segments_respected(self):
        """智谱是 v4，硬补 /v1 会打到不存在的路径。"""
        assert self._url("https://open.bigmodel.cn/api/paas/v4") == (
            "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        )

    def test_trailing_slash_with_version(self):
        assert self._url("https://api.moonshot.cn/v1/") == (
            "https://api.moonshot.cn/v1/chat/completions"
        )

    def test_path_containing_v1_midway_still_gets_suffix(self):
        """只有结尾的版本段算数，中间出现的不算。"""
        assert self._url("http://host/v1proxy") == (
            "http://host/v1proxy/v1/chat/completions"
        )

    def test_dashscope_compatible_mode_unchanged(self):
        """已验证可用的 DashScope 路径不能被这次改动带偏。"""
        c = g3.LiteLlmClient(
            base_url="https://dashscope.aliyuncs.com/compatible-mode", api_key="k"
        )
        assert c.chat_url == (
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        )
