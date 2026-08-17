"""客服 Agent 命令行调试台。

    smartmall-agent chat                     # 交互式对话
    smartmall-agent chat --product-id 1024   # 带商品上下文
    smartmall-agent ask "这件是什么面料"      # 单轮
    smartmall-agent trace "会起球吗"          # 单轮 + 打印完整 Trace
    smartmall-agent shop -v                  # 导购 Agent：多轮收敛出商品
    smartmall-agent kb                       # 知识运维：给盲点起草（默认试跑）

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
from .textwidth import pad, truncate


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


def _build_deps(args: argparse.Namespace, *, with_retriever: bool = True) -> Deps:
    """CLI 侧装配。只负责把命令行参数翻成配置，装配本身走 assembly。"""
    from .assembly import build_deps, config_from_env

    cfg = config_from_env(
        top_k=args.top_k,
        handover_below=args.handover_below,
        clarify_below=args.clarify_below,
    )
    return build_deps(
        fake_llm=args.fake_llm,
        rag_url=args.rag_url,
        with_store=not args.no_trace,
        with_tools=not args.no_tools,
        with_retriever=with_retriever,
        config=cfg,
    )


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
            # 看覆盖率而不是 bm25>0：后者近乎恒真（共享一个常见 bigram
            # 就有分），诊断面板上会把"根本没匹配上"显示成"词汇✓"
            lex = f"词汇{h.lexical_overlap:.2f}"
            print(f"     [#{h.item_id}] {h.dense_score:.3f} {pad(lex, 8)}"
                  f" {truncate(h.title, 36)}")
        for t in state.trace.tools_called:
            mark = "命中" if t.get("hit") else "无数据"
            print(f"     🔧 {t['name']} {t['latency_ms']}ms {mark}")
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


def cmd_shop(args: argparse.Namespace) -> int:
    """导购 Agent 的交互式调试。

    这里 verbose 显示的东西和客服那边不一样，因为**要看的东西不一样**。
    客服看的是"这条回答有没有依据"（命中、分数、覆盖率）；导购看的是
    **收敛过程**——攒到了哪些条件、按这些条件搜出几件、放宽了什么。
    多轮下来那几行叠在一起，就是这个 Agent 到底在不在收敛的全部证据。
    """
    from .shopping.graph import safe_run_turn
    from .shopping.nodes import MAX_ASKS
    from .shopping.state import ShoppingState

    deps = _build_deps(args, with_retriever=False)
    state = ShoppingState(session=SessionContext(
        user_id=args.user_id, current_product_id=args.product_id,
        category_id=args.category_id,
    ))
    print("\n说说你想买什么，Ctrl+C 或输入 exit 退出。\n")

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

        state = safe_run_turn(message, state, deps)

        if args.verbose:
            print(f"\n  ── 已知需求：{state.need.describe()}")
            print(f"     候选 {len(state.candidates)} 件"
                  f" · 已追问 {state.asked}/{MAX_ASKS}"
                  f" · 结果 {state.outcome}")
            if state.relaxed:
                print(f"     ⚠ 放宽了：{'、'.join(state.relaxed)}")
            for t in state.trace.tools_called:
                mark = "有结果" if t.get("hit") else "零条"
                print(f"     🔧 {t['name']} {t['latency_ms']}ms {mark}")
            if state.trace.error:
                print(f"     ✗ {state.trace.error}")

        print(f"\n导购：{state.answer}")
        if state.recommended:
            by_id = {int(c["id"]): c for c in state.candidates}
            print("\n  推荐商品：")
            for pid in state.recommended:
                item = by_id.get(pid, {})
                print(f"    #{pid} {truncate(str(item.get('name', '')), 40)}"
                      f"  {item.get('price_from', '')} 元")
        print()
    return 0


def cmd_kb(args: argparse.Namespace) -> int:
    """知识运维：把「答不上来的问题」变成「待审的知识」。

    默认 ``--dry-run``，只起草不落库。**这个默认值是刻意的**——
    这条链路会往知识库里写东西，而写进去的每一条都会被之后的检索命中。
    第一次跑先看看机器写成什么样，比先写进去再删干净容易得多。
    """
    from .knowledge import graph as kb_graph
    from .knowledge.store import MySqlKnowledgeStore

    try:
        store = MySqlKnowledgeStore.from_env()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 连不上知识库（{type(exc).__name__}）：{exc}")
        print("  建表：deploy/sql/migrations/003_agent_trace_and_handover.sql")
        return 1

    spots = store.blind_spots(limit=args.scan)
    if not spots:
        print("\n  没有待处理的盲点——要么知识库覆盖得好，要么还没人用过。")
        return 0

    deps = _build_deps(args)
    deps.kb = None if args.dry_run else store

    print(f"\n==> 扫到 {len(spots)} 个题面"
          f"{'（试跑，不落库）' if args.dry_run else ''}\n")

    report = kb_graph.run_batch(spots, deps, limit=args.limit)

    mark = {"drafted": "✎", "draft_only": "✎", "already_covered": "=",
            "needs_human": "!", "skipped": "×"}
    for s in report.spots:
        print(f"  {mark.get(s.outcome, '?')} [{s.spot.priority}]"
              f" ×{s.spot.times}  {truncate(s.question, 40)}")
        if s.spot.variants:
            print(f"      其他问法：{truncate('、'.join(s.spot.variants), 46)}")
        if s.outcome == "already_covered":
            print(f"      库里已有 #{s.item_id}"
                  f"（相似度 {s.trace.retrieval_max_score:.3f}）")
            print("      当时没召回是检索的问题，再补一条知识没用")
        elif s.outcome == "duplicate":
            print(f"      内容与 #{s.item_id} 相同，没有重复落库")
            # 工单不关：内容有了，但**这个问法**能不能检索到它是另一回事
            # （踩到过：「怎么洗」和「洗涤方式」题面零重合）
            print("      工单仍是 open——这个问法能否召回到它，"
                  "要等索引重建后才知道")
        elif s.draft:
            print(f"      草稿：{truncate(s.draft, 58)}")
            print(f"      依据：{'、'.join(e.ref for e in s.evidence)}")
        if s.flags:
            print(f"      ⚠ {'、'.join(s.flags)}")
        if s.outcome == "drafted":
            print(f"      → knowledge_item #{s.item_id}（待审核）")
        print()

    d = report.as_dict()
    written = d["drafted"]
    ready = d["draft_only"]
    print(f"  ── 落库 {written} · 起草待落库 {ready}"
          f" · 库里已有 {d['already_covered']} · 本轮重复 {d['duplicate']}"
          f" · 需要人写 {d['needs_human']} · 跳过 {d['skipped']}")
    if args.dry_run:
        print(f"\n  试跑不写库。去掉 --dry-run 会写入 {ready} 条**待审核**的条目"
              "（机器起草的永远不自动通过）")
    print("  审核草稿：smartmall-pipeline annotate export --status pending")
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

    # 全部按显示列对齐：表头是中文、👍 是宽字符，按字符补齐会让表头和
    # 数据行各错开两三列，而竖着扫这张表正是它存在的意义
    print(f"{pad('时间', 17)} {pad('意图', 20)} {pad('命中', 4, right=True)}"
          f" {pad('最高分', 7, right=True)} {pad('反馈', 4)} 问题")
    print("─" * 96)
    for r in rows:
        thumb = {1: "👍", -1: "👎"}.get(r["thumb"], "")
        mark = "转人工 " if r["handover"] else ""
        print(f"{r['created_at']:%m-%d %H:%M:%S}   {pad(r['intent'], 20)}"
              f" {r['retrieval_hit_count']:>4} {float(r['retrieval_max_score']):>7.3f}"
              f" {pad(thumb, 4)} {mark}{truncate(r['input_text'], 28)}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """跑评测集。

    这是整个项目里唯一**测量**而非断言的部分：单测证明代码按我写的
    那样跑，评测证明这套系统在真实输入上管用。
    """
    import sys as _sys

    from ..eval import runner as R

    deps = _build_deps(args)
    names = [args.suite] if args.suite != "all" else list(R.RUNNERS)
    baseline = R.load_baseline()
    outcomes, failed = {}, False

    for name in names:
        full = R.load(name)
        samples = R.subsample(full, args.limit or 0, R.LABEL_KEYS.get(name))
        note = f"，从 {len(full)} 条分层抽样" if len(samples) < len(full) else ""
        print(f"\n==> {name}（{len(samples)} 条{note}）")

        def tick(i, total, _n=name):
            print(f"\r  {i}/{total}", end="", flush=True)

        out = R.RUNNERS[name](deps, samples, progress=tick)
        print("\r" + " " * 20 + "\r", end="")
        print(out.render(_TITLES.get(name, name)))

        note = R.compare_baseline(name, out.report, baseline)
        if note:
            print(f"\n{note}")
            if note.startswith("✗"):
                failed = True

        errs = list(R.iter_errors(out, args.show_errors))
        if errs:
            print("\n  错例（改哪里看这里，不是看总分）：")
            for e in errs:
                extra = {k: v for k, v in e.items() if k != "text"}
                print(f"    「{e['text']}」 {extra}")

        outcomes[name] = {
            "accuracy": round(out.report.accuracy, 4),
            "macro_f1": round(out.report.macro_f1, 4),
            "samples": len(samples),
        }
        failed = failed or not out.passed

    if args.save_baseline:
        # 只在全绿时写基线，否则会把一次退步固化成新标准
        if args.limit:
            print("\n✗ --limit 是抽样跑，不能写基线——"
                  "拿 20 条的分数当全量基线，往后每次比较都是错的")
        elif failed:
            print("\n✗ 有门禁未通过，不写基线——那会把退步固化成新标准")
        else:
            print(f"\n✅ 基线已更新：{R.save_baseline(outcomes)}")

    print()
    return 1 if failed else 0


_TITLES = {
    "intent": "意图分类（七类）",
    "negative": "拒答（知识库没有的问题必须转人工）",
    "safety": "安全（注入/违禁拦截，同时不误伤正常提问）",
}


def cmd_serve(args: argparse.Namespace) -> int:
    """起 Web 调试台。

    终端能聊，但演示时一个真的聊天界面差别很大——尤其是右侧那块
    诊断面板：意图、命中、相似度、有没有词汇支撑、为什么转人工，
    这些平时全是隐形的。
    """
    try:
        import uvicorn
    except ImportError:
        print("✗ 需要 uvicorn：pip install -e 'apps/python/ai-agent[server]'")
        return 1

    print(f"\n  调试台 → http://127.0.0.1:{args.port}/\n")
    uvicorn.run("app.main:app", host=args.host, port=args.port,
                reload=args.reload, log_level="warning")
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
    common.add_argument("--clarify-below", type=float, default=0.55,
                        help="低于此相似度先追问澄清（且需有词汇支撑，否则转人工）")
    common.add_argument("--rag-url", help="改用 ai-rag 服务检索，默认直连 MySQL")
    common.add_argument("--fake-llm", action="store_true",
                        help="用假模型，不调真实 API、不产生费用")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="打印意图、命中与分数")
    common.add_argument("--no-trace", action="store_true",
                        help="不写埋点。默认写——Trace 是训练数据的原料")
    common.add_argument("--no-tools", action="store_true",
                        help="不接业务工具。实时类问题会转人工")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chat", parents=[common], help="交互式多轮对话").set_defaults(
        func=cmd_chat
    )
    sub.add_parser("shop", parents=[common],
                   help="导购 Agent：多轮收敛出具体商品").set_defaults(
        func=cmd_shop
    )

    s = sub.add_parser("kb", parents=[common],
                       help="知识运维 Agent：给知识盲点起草待审条目")
    s.add_argument("--scan", type=int, default=50, help="扫多少个题面")
    s.add_argument("--limit", type=int, default=5,
                   help="聚类后处理前 N 个盲点")
    # 默认试跑：这条链路会往知识库写东西，而写进去的每一条都会被之后的
    # 检索命中。先看看机器写成什么样，比先写进去再删干净容易得多
    s.add_argument("--write", dest="dry_run", action="store_false",
                   default=True, help="真的写库（默认只试跑）")
    s.set_defaults(func=cmd_kb)
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

    s = sub.add_parser("eval", parents=[common], help="跑评测集（意图/拒答/安全）")
    s.add_argument("--suite", default="all",
                   choices=["all", "intent", "negative", "safety"])
    s.add_argument("--limit", type=int, default=0, help="只跑前 N 条，试水用")
    s.add_argument("--show-errors", type=int, default=8)
    s.add_argument("--save-baseline", action="store_true",
                   help="把本次结果写成基线（仅在门禁全过时生效）")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("serve", help="起 Web 调试台（流式对话 + 诊断面板）")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=9002)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

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
