"""客服 Agent 命令行调试台。

    smartmall-agent chat                     # 交互式对话
    smartmall-agent chat --product-id 1024   # 带商品上下文
    smartmall-agent ask "这件是什么面料"      # 单轮
    smartmall-agent trace "会起球吗"          # 单轮 + 打印完整 Trace

调试台的价值在于**看得见中间过程**：意图分到哪一类、检索命中了什么、
分数多少、为什么转人工。这些在聊天界面里全是隐形的，而它们恰恰是
调参时唯一有用的信息。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .llm import FakeLlmClient, LlmError, OpenAiCompatClient
from .nodes import AgentConfig, Deps
from .retriever import RetrievalError
from .state import AgentState, SessionContext


# ---------------------------------------------------------------- .env


def _load_env_file(path: Path) -> int:
    """与 smartmall-pipeline 用同一份 deploy/.env，避免两处配置漂移。"""
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _default_env_files() -> list[Path]:
    candidates = [Path.cwd() / ".env"]
    for parent in Path(__file__).resolve().parents:
        if (parent / "deploy").is_dir():
            candidates.append(parent / "deploy" / ".env")
            break
    return candidates


# ---------------------------------------------------------------- 装配


def _build_deps(args: argparse.Namespace) -> Deps:
    cfg = AgentConfig()
    for env_key, attr in (
        ("SMARTMALL_INTENT_MODEL", "intent_model"),
        ("SMARTMALL_ANSWER_MODEL", "answer_model"),
        ("SMARTMALL_EXTRACT_MODEL", "answer_model"),
    ):
        value = os.environ.get(env_key, "").strip()
        if value:
            setattr(cfg, attr, value)
    # 三个轻量节点（意图/澄清/寒暄/改写/摘要）共用便宜模型
    light = os.environ.get("SMARTMALL_TRIAGE_MODEL", "").strip()
    if light:
        for attr in ("intent_model", "clarify_model", "chitchat_model",
                     "rewrite_model", "summary_model"):
            setattr(cfg, attr, light)
    cfg.top_k = args.top_k
    cfg.handover_below = args.handover_below
    cfg.clarify_below = args.clarify_below

    llm = FakeLlmClient() if args.fake_llm else OpenAiCompatClient()

    if args.rag_url:
        from .retriever import HttpRetriever

        retriever = HttpRetriever(args.rag_url)
        print(f"  检索：ai-rag {args.rag_url}")
    else:
        from .retriever import LocalRetriever

        retriever = LocalRetriever()
        print(f"  检索：本地 MySQL，已加载 {retriever.size} 条切片"
              f"（{retriever.provider_name}）")
        if retriever.size == 0:
            print("  ⚠ 索引是空的。先跑 smartmall-pipeline clean 和 index。")

    print(f"  模型：意图 {cfg.intent_model} / 回答 {cfg.answer_model}")

    store = None
    if not args.no_trace:
        from .store import MySqlTraceStore

        try:
            store = MySqlTraceStore.from_env()
            print("  埋点：写入 agent_trace / handover_ticket")
        except Exception as exc:  # noqa: BLE001
            # 埋点起不来不该挡住对话——它是数据管道，不是功能依赖
            print(f"  ⚠ 埋点不可用（{type(exc).__name__}），本次不落库")
            print("    建表：deploy/sql/migrations/003_agent_trace_and_handover.sql")

    return Deps(llm=llm, retriever=retriever, config=cfg, store=store)


def _new_state(args: argparse.Namespace) -> AgentState:
    return AgentState(session=SessionContext(
        user_id=args.user_id,
        current_product_id=args.product_id,
        category_id=args.category_id,
    ))


def _render(state: AgentState, *, verbose: bool) -> None:
    if verbose:
        print(f"\n  ── 意图 {state.trace.intent}"
              f" · 命中 {state.trace.retrieval_hit_count} 条"
              f" · 最高相似度 {state.trace.retrieval_max_score:.3f}")
        if state.trace.rewritten_query:
            print(f"     改写查询：{state.trace.rewritten_query}")
        for h in state.hits:
            print(f"     [#{h.item_id}] {h.dense_score:.3f} {h.title[:40]}")
        if state.postcheck_flags:
            print(f"     合规标记：{'、'.join(state.postcheck_flags)}")
        if state.handover:
            reason = state.handover_reason.value if state.handover_reason else ""
            print(f"     ⚠ 转人工：{reason}")

    print(f"\n客服：{state.answer}")
    if state.citations:
        print("\n  引用：")
        for c in state.citations:
            print(f"    [#{c.item_id}] {c.title[:50]}")
    if state.handover and state.handover_summary:
        print("\n  交接摘要：")
        print(f"    {state.handover_summary.get('summary', '')}")
        action = state.handover_summary.get("suggested_action")
        if action:
            print(f"    建议：{action}")
    if state.handover_ticket_id:
        print(f"\n  已建工单 #{state.handover_ticket_id} —— 这是一个知识盲点。")
        print(f"    补上答案：smartmall-pipeline handover answer "
              f"{state.handover_ticket_id} \"...\"")
    if state.trace.trace_id:
        print(f"\n  trace {state.trace.trace_id[:12]}"
              f"  （点踩：smartmall-agent feedback {state.trace.trace_id} down）")


# ---------------------------------------------------------------- 命令


def cmd_chat(args: argparse.Namespace) -> int:
    from .graph import safe_run_turn

    deps = _build_deps(args)
    state = _new_state(args)
    print("\n输入问题开始对话，Ctrl+C 或输入 exit 退出。\n")

    while True:
        try:
            message = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if message.lower() in ("exit", "quit", "q"):
            break
        if not message:
            continue
        # 每轮新建 trace，但沿用同一个 session——多轮上下文靠它维持
        state.trace = type(state.trace)()
        state = safe_run_turn(message, state, deps)
        _render(state, verbose=args.verbose)
        print()
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from .graph import safe_run_turn

    deps = _build_deps(args)
    state = safe_run_turn(args.question, _new_state(args), deps)
    _render(state, verbose=args.verbose)
    return 1 if state.handover else 0


def cmd_trace(args: argparse.Namespace) -> int:
    from .graph import safe_run_turn

    deps = _build_deps(args)
    state = safe_run_turn(args.question, _new_state(args), deps)
    _render(state, verbose=True)
    print("\n  ── Trace ──")
    print(json.dumps(state.trace.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    """点赞点踩回写。

    点踩的回复直接作为 DPO 负例；原因里的「态度生硬」「太啰嗦」
    正对应微调要解决的风格问题——那两个选项的信息量最大。
    """
    from .store import MySqlTraceStore

    store = MySqlTraceStore.from_env()
    ok = store.record_feedback(
        args.trace_id, 1 if args.thumb == "up" else -1, args.reason
    )
    if not ok:
        print(f"✗ 没有找到 trace {args.trace_id}")
        if store.errors:
            print(f"  {store.errors[-1]}")
        return 1
    print(f"  ✓ 已记录{'点赞' if args.thumb == 'up' else '点踩'}")
    return 0


def cmd_traces(args: argparse.Namespace) -> int:
    """最近的对话埋点。

    调阈值时最有用的一张表：把命中分数和"这次答得好不好"放在一起看，
    才知道 handover_below 该定在哪。
    """
    from .store import MySqlTraceStore

    rows = MySqlTraceStore.from_env().recent(
        args.limit, handover_only=args.handover_only
    )
    if not rows:
        print("  还没有埋点。先跑 `smartmall-agent chat` 聊几句。")
        return 0

    print(f"{'时间':<17} {'意图':<20} {'命中':>4} {'最高分':>7} {'反馈':<4} 问题")
    print("─" * 96)
    for r in rows:
        thumb = {1: "👍", -1: "👎"}.get(r["thumb"], "")
        mark = "转人工 " if r["handover"] else ""
        print(f"{r['created_at']:%m-%d %H:%M:%S}   {r['intent']:<20}"
              f" {r['retrieval_hit_count']:>4} {float(r['retrieval_max_score']):>7.3f}"
              f" {thumb:<4} {mark}{r['input_text'][:28]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="smartmall-agent", description="smartMall 客服 Agent 调试台"
    )
    p.add_argument("--env-file", help="默认依次尝试 ./.env 与 <仓库根>/deploy/.env")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--product-id", type=int, help="当前商品，决定检索过滤范围")
    common.add_argument("--category-id", type=int)
    common.add_argument("--user-id", type=int, default=10086)
    common.add_argument("--top-k", type=int, default=5)
    common.add_argument("--handover-below", type=float, default=0.30,
                        help="低于此相似度直接转人工")
    common.add_argument("--clarify-below", type=float, default=0.50,
                        help="低于此相似度先追问澄清")
    common.add_argument("--rag-url", help="改用 ai-rag 服务检索，默认直连 MySQL")
    common.add_argument("--fake-llm", action="store_true",
                        help="用假模型，不调真实 API、不产生费用")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="打印意图、命中与分数")
    common.add_argument("--no-trace", action="store_true",
                        help="不写埋点。默认写——Trace 是训练数据的原料")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chat", parents=[common], help="交互式多轮对话").set_defaults(
        func=cmd_chat
    )
    s = sub.add_parser("ask", parents=[common], help="单轮提问")
    s.add_argument("question")
    s.set_defaults(func=cmd_ask)
    s = sub.add_parser("trace", parents=[common], help="单轮提问并打印完整 Trace")
    s.add_argument("question")
    s.set_defaults(func=cmd_trace)

    s = sub.add_parser("feedback", help="给某次回复点赞或点踩")
    s.add_argument("trace_id")
    s.add_argument("thumb", choices=["up", "down"])
    s.add_argument("--reason", default="",
                   help="答非所问 / 信息错误 / 态度生硬 / 太啰嗦")
    s.set_defaults(func=cmd_feedback)

    s = sub.add_parser("traces", help="看最近的对话埋点")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--handover-only", action="store_true")
    s.set_defaults(func=cmd_traces)

    args = p.parse_args(argv)

    files = [Path(args.env_file)] if args.env_file else _default_env_files()
    for f in files:
        n = _load_env_file(f)
        if n:
            print(f"  已加载 {f}（{n} 项）")
            break

    try:
        return args.func(args)
    except RetrievalError as exc:
        print(f"\n✗ 检索不可用：{exc}")
        return 1
    except LlmError as exc:
        print(f"\n✗ 模型调用失败：{exc}")
        print("  想先跑通链路不花钱：加 --fake-llm")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
