"""依赖装配。**只此一份。**

踩过的坑：CLI 和 HTTP 服务各自装配了一遍 Deps，然后漂移了——CLI 会把
``SMARTMALL_*_MODEL`` 覆盖进配置，服务端不会，于是服务端拿着网关别名
``chat-default`` 去调 DeepSeek，直接 400，最后表现成"这个问题我不太确定，
帮您转接人工"。

**症状离病因隔了三层**：用户看到的是"答不上来"，实际是模型名没翻译。
装配逻辑重复一次就会漂移一次，所以这里收成唯一入口，CLI 与服务共用。
"""

from __future__ import annotations

import os

from .llm import FakeLlmClient, OpenAiCompatClient
from .nodes import AgentConfig, Deps


def config_from_env(**overrides) -> AgentConfig:
    """从环境变量构建配置。

    默认模型名是网关别名（``chat-light`` / ``chat-default``），直连厂商时
    必须覆盖成真实模型名——DeepSeek 不认识"chat-default"。
    与数据中台用同一套环境变量，避免两处配置各说各话。
    """
    cfg = AgentConfig()

    answer = (
        os.environ.get("SMARTMALL_ANSWER_MODEL")
        or os.environ.get("SMARTMALL_EXTRACT_MODEL")
        or ""
    ).strip()
    if answer:
        cfg.answer_model = answer

    # 意图/澄清/寒暄/改写/摘要都是轻量调用，共用便宜模型
    light = (
        os.environ.get("SMARTMALL_INTENT_MODEL")
        or os.environ.get("SMARTMALL_TRIAGE_MODEL")
        or ""
    ).strip()
    if light:
        for attr in ("intent_model", "clarify_model", "chitchat_model",
                     "rewrite_model", "summary_model"):
            setattr(cfg, attr, light)

    for key, value in overrides.items():
        if value is not None:
            setattr(cfg, key, value)
    return cfg


def build_deps(
    *,
    fake_llm: bool = False,
    rag_url: str | None = None,
    with_store: bool = True,
    with_tools: bool = True,
    config: AgentConfig | None = None,
    log=print,
) -> Deps:
    """装好一套依赖。

    可选组件（埋点、工具）起不来时**只警告不中断**：它们是数据管道与
    增强能力，不是对话本身的前提。必需组件（模型、检索）起不来才该报错。
    """
    cfg = config or config_from_env()

    llm = FakeLlmClient() if fake_llm else OpenAiCompatClient()

    if rag_url:
        from .retriever import HttpRetriever

        retriever = HttpRetriever(rag_url)
        log(f"  检索：ai-rag {rag_url}")
    else:
        from .retriever import LocalRetriever

        retriever = LocalRetriever()
        log(f"  检索：本地 MySQL，已加载 {retriever.size} 条切片"
            f"（{retriever.provider_name}）")
        if retriever.size == 0:
            log("  ⚠ 索引是空的。先跑 smartmall-pipeline clean 和 index。")

    log(f"  模型：意图 {cfg.intent_model} / 回答 {cfg.answer_model}")

    store = None
    if with_store:
        from .store import MySqlTraceStore

        try:
            store = MySqlTraceStore.from_env()
            log("  埋点：写入 agent_trace / handover_ticket")
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ 埋点不可用（{type(exc).__name__}），本次不落库")
            log("    建表：deploy/sql/migrations/003_agent_trace_and_handover.sql")

    tools = None
    if with_tools:
        from .tools import MySqlToolBox

        try:
            tools = MySqlToolBox.from_env()
            log("  工具：商品/SKU/尺码表/订单（只读，含越权校验）")
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ 工具集不可用（{type(exc).__name__}），实时类问题会转人工")
            log("    建表与种子：deploy/sql/migrations/004_order_and_tool_seed.sql")

    vlm = None
    if fake_llm:
        from .vision import FakeVisionClient

        vlm = FakeVisionClient()
    else:
        from .vision import DashScopeVisionClient

        try:
            vlm = DashScopeVisionClient()
            log("  看图：qwen-vl-max（转述后走同一套脱敏与检索）")
        except Exception as exc:  # noqa: BLE001
            # 失败关闭：不配就不收图。装作没看见比"看了但没脱敏"安全得多
            log(f"  ⚠ 看图不可用（{type(exc).__name__}），发图会被婉拒")

    return Deps(llm=llm, retriever=retriever, config=cfg, store=store,
                tools=tools, vision=vlm)
