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
from pathlib import Path

from .llm import FakeLlmClient, OpenAiCompatClient
from .nodes import AgentConfig, Deps


def asset_dir() -> Path:
    """生成素材的落地目录。

    默认放在 ``web/generated``，因为它要被页面直接引用（见
    ``routers/media.py``）。**不放 web/img**：那里是商家自己传的商品图，
    与机器生成的素材混在一起之后，"这张图是不是 AI 画的"就只能靠查库——
    而《人工智能生成合成内容标识办法》要求的恰恰是它可被识别。
    """
    override = os.environ.get("SMARTMALL_ASSET_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "web" / "generated"


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
    with_retriever: bool = True,
    with_media: bool = False,
    with_tasks: bool = True,
    config: AgentConfig | None = None,
    log=print,
) -> Deps:
    """装好一套依赖。

    可选组件（埋点、工具）起不来时**只警告不中断**：它们是数据管道与
    增强能力，不是对话本身的前提。必需组件（模型、检索）起不来才该报错。

    ``with_retriever=False`` 给导购这类不走 RAG 的链路用。**这不是省事，
    是别立假门槛**：导购一条检索都不发，却要为了装配去申请 embedding
    的 key、等它把几千条切片灌进内存，起不来还直接报错退出——
    用户会以为是导购坏了。

    ``with_media`` 默认关，与上面相反的理由：**它是唯一一个装上就可能
    花钱的组件**，而 chat / ask / eval 一次也用不到。谁要用谁显式打开。
    """
    cfg = config or config_from_env()

    llm = FakeLlmClient() if fake_llm else OpenAiCompatClient()

    if not with_retriever:
        from .retriever import NullRetriever

        retriever = NullRetriever()
        log("  检索：不装（这条链路只查结构化商品数据）")
    elif rag_url:
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

    mediac, assets = None, None
    if with_media:
        if fake_llm:
            from .marketing.media import FakeMediaClient

            mediac = FakeMediaClient()
            log("  生成：假模型（不调 API、不花额度）")
        else:
            from .marketing.media import DashScopeMediaClient

            try:
                mediac = DashScopeMediaClient()
                log(f"  生成：{mediac.image_model} / {mediac.video_model}")
            except Exception as exc:  # noqa: BLE001
                # 不配就只出提示词。提示词是这条链路上唯一能被规则检查的
                # 东西，看得见它比生成不了更要紧
                log(f"  ⚠ 生成模型不可用（{type(exc).__name__}），只出提示词")

        from .marketing.store import MySqlAssetStore

        try:
            assets = MySqlAssetStore.from_env()
            log("  素材：写入 marketing_asset（一律待审）")
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠ 素材库不可用（{type(exc).__name__}），生成的图不会落库")
            log("    建表：deploy/sql/migrations/011_marketing_asset.sql")

    queue = None
    if with_tasks:
        from .tasks.store import MySqlTaskStore

        try:
            queue = MySqlTaskStore.from_env()
            log("  任务：客服答不上来 → 自动派给知识运维 → 再派给运营")
        except Exception as exc:  # noqa: BLE001
            # 起不来只是回到"四个 Agent 各跑各的"，对话本身一点不受影响
            log(f"  ⚠ 任务队列不可用（{type(exc).__name__}），Agent 之间不派活")
            log("    建表：deploy/sql/migrations/012_agent_task.sql")

    return Deps(llm=llm, retriever=retriever, config=cfg, store=store,
                tools=tools, vision=vlm, media=mediac, asset_store=assets,
                asset_dir=asset_dir() if with_media else None, tasks=queue)
