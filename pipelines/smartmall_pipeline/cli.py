"""数据中台命令行入口。

给人用的口子——Airflow 是给调度用的，但开发、联调、演示都需要能手动跑一遍。

::

    python -m smartmall_pipeline.cli check                  # 检查数据库连通与表结构
    python -m smartmall_pipeline.cli ingest --count 300     # 生成合成对话写入 ODS
    python -m smartmall_pipeline.cli clean                  # 跑四道关卡
    python -m smartmall_pipeline.cli stats                  # 看各层数据量
    python -m smartmall_pipeline.cli coverage               # 知识覆盖度矩阵
    python -m smartmall_pipeline.cli publish --version kb-v1

数据库连接从环境变量读（与 deploy/.env 一致）：
``MYSQL_HOST`` / ``MYSQL_PORT`` / ``MYSQL_USER`` / ``MYSQL_PASSWORD`` / ``MYSQL_DATABASE``
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from . import coverage as coverage_mod
from . import publish as publish_mod
from .gates import gate3_model
from .ingest import synthetic
from .models import KnowledgeType
from .orchestrator import PipelineConfig, run_pipeline
from .repository import DwsRepository, OdsRepository
from .textwidth import pad, truncate

EXPECTED_TABLES = 31


# ---------------------------------------------------------------- .env

def _load_env_file(path: Path) -> int:
    """加载 KEY=VALUE 形式的 .env 文件。

    **不覆盖已存在的环境变量**——显式 export 的值优先于文件，
    这样临时切库（比如指到测试库跑一次）不需要改文件。

    自己实现而不引入 python-dotenv：只有十几行，不值得多一个依赖。
    """
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
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _default_env_files() -> list[Path]:
    """按优先级列出候选 .env 位置。

    先看当前目录，再往上找仓库根的 deploy/.env——
    从 pipelines/ 或仓库根目录跑，都能找到同一份配置。
    """
    candidates = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "deploy").is_dir():
            candidates.append(parent / "deploy" / ".env")
            break
    return candidates


def _env_summary() -> str:
    return (
        f"{os.environ.get('MYSQL_USER', 'smartmall')}@"
        f"{os.environ.get('MYSQL_HOST', 'localhost')}:"
        f"{os.environ.get('MYSQL_PORT', '3306')}/"
        f"{os.environ.get('MYSQL_DATABASE', 'smartmall')}"
    )


# ---------------------------------------------------------------- check


def cmd_check(args: argparse.Namespace) -> int:
    """检查数据库连通性与表结构。第一步先跑这个，能省掉后面一堆莫名其妙的报错。"""
    from sqlalchemy import text

    print(f"==> 连接 {_env_summary()}")
    repo = OdsRepository.from_env()

    try:
        with repo.engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar()
            db = conn.execute(text("SELECT DATABASE()")).scalar()
            tables = [
                r[0] for r in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                ))
            ]
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 连接失败: {type(exc).__name__}: {exc}")
        print("\n  排查方向：")
        print("    · MySQL 服务是否启动")
        print("    · 环境变量 MYSQL_USER / MYSQL_PASSWORD 是否与实际一致")
        print("    · 用户是否有该库的权限")
        return 1

    print(f"  ✓ 已连接 {version}，当前库 {db}")

    if len(tables) < EXPECTED_TABLES:
        missing = EXPECTED_TABLES - len(tables)
        print(f"  ✗ 只有 {len(tables)} 张表，少了 {missing} 张")
        print("    请按 01→02→03→04→99 的顺序导入 deploy/sql/mysql/*.sql")
        return 1
    print(f"  ✓ 表结构完整（{len(tables)} 张）")

    # 中文注释是否正常——编码错了这里能立刻发现
    with repo.engine.connect() as conn:
        comment = conn.execute(text(
            "SELECT table_comment FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'knowledge_item'"
        )).scalar()
    if comment and "知识条目" in comment:
        print("  ✓ 中文注释正常（UTF-8 编码正确）")
    else:
        print(f"  ⚠ 表注释异常：{comment!r}")
        print("    导入时编码可能选错了，建议用 UTF-8 重新导入")

    print("\n✅ 数据库就绪")
    return 0


# ---------------------------------------------------------------- ingest


def cmd_ingest(args: argparse.Namespace) -> int:
    """生成合成对话并写入 ODS。

    合成数据是冷启动的第二条腿，也是 JDDC 授权下来之前唯一能跑通全链路的数据源。
    它故意包含 PII、系统话术、近重复——正是四道关卡该清掉的东西。
    """
    print(f"==> 生成 {args.count} 条合成对话（seed={args.seed}）")
    dialogues = synthetic.generate_batch(
        args.count, seed=args.seed, duplicate_rate=args.duplicate_rate
    )
    print(f"  含近重复 {len(dialogues) - args.count} 条（用于验证去重）")

    batch_id = args.batch_id or f"syn-{datetime.now():%Y%m%d%H%M%S}"
    repo = OdsRepository.from_env()
    inserted = repo.insert_dialogues(dialogues, batch_id=batch_id)

    print(f"  ✓ 批次 {batch_id}：提交 {len(dialogues)} 条，实际写入 {inserted} 条")
    if inserted < len(dialogues):
        print(f"    （{len(dialogues) - inserted} 条因内容哈希重复被幂等跳过）")
    return 0


# ---------------------------------------------------------------- clean


def _gate3_config_from_env() -> gate3_model.Gate3Config:
    """允许用环境变量覆盖关卡③的模型名。

    默认值是网关里的别名（chat-light / chat-default），直连时由各 client
    的别名表翻译成厂商真名。但别名表只覆盖了 DashScope——换成 DeepSeek、
    Kimi、智谱或本地 vLLM 时它翻译不出来，硬编码就成了拦路虎。
    留出覆盖口，换厂商不必改代码。
    """
    cfg = gate3_model.Gate3Config()
    for env_key, attr in (
        ("SMARTMALL_TRIAGE_MODEL", "triage_model"),
        ("SMARTMALL_EXTRACT_MODEL", "extract_model"),
        ("SMARTMALL_STYLE_MODEL", "style_model"),
    ):
        value = os.environ.get(env_key, "").strip()
        if value:
            setattr(cfg, attr, value)
    return cfg


def _resolve_llm_backend(args: argparse.Namespace) -> str:
    """选定模型通道。

    显式 ``--llm`` 优先；没写时按环境变量推断——配了
    ``SMARTMALL_LLM_BASE_URL`` 就说明厂商已经选好了，此时还默认走
    dashscope 只会撞上一个他明明不打算用的通道（比如免费额度已用尽）。
    """
    if args.fake_llm:
        return "fake"
    if args.llm:
        return args.llm
    if os.environ.get("SMARTMALL_LLM_BASE_URL", "").strip():
        return "openai"
    return "dashscope"


def cmd_clean(args: argparse.Namespace) -> int:
    """从 ODS 拉取未处理对话，跑四道关卡，产出写回 DWS。"""
    ods = OdsRepository.from_env()
    dws = DwsRepository.from_env()

    print(f"==> 从 ODS 拉取最多 {args.limit} 条未处理对话")
    dialogues = ods.fetch_unprocessed(limit=args.limit)
    if not dialogues:
        # 「没有待处理」有两种完全不同的原因，处置也完全不同：
        # ODS 是空的要去 ingest，都清完了则该往下走 index。
        # 只说一句"先跑 ingest"会把已经清完的人指向重复造数据。
        from sqlalchemy import text as _sql

        with ods.engine.connect() as conn:
            total = conn.execute(
                _sql("SELECT COUNT(*) FROM ods_raw_dialogue")
            ).scalar() or 0
            done = conn.execute(
                _sql("SELECT COUNT(*) FROM ods_process_log")
            ).scalar() or 0

        if total == 0:
            print("  ODS 里一条原始对话都没有。先跑 `ingest` 造一批：")
            print("    smartmall-pipeline ingest --count 400")
        else:
            print(f"  ODS 共 {total} 条，已全部处理完（process_log {done} 条）。")
            print("  下一步：`index` 向量化，或 `ingest` 再造一批。")
            print("  想重新清洗（比如改了清洗规则）：`reset --yes` 清掉派生数据，"
                  "ODS 原始数据会保留。")
        return 0
    print(f"  取到 {len(dialogues)} 条")

    backend = _resolve_llm_backend(args)
    try:
        if backend == "fake":
            print("  使用 FakeLlmClient（不调用真实模型，不产生费用）")
            llm = gate3_model.FakeLlmClient(items_per_dialogue=args.items_per_dialogue)
        elif backend == "dashscope":
            print("  直连 DashScope（未经网关，无统一成本记账）")
            llm = gate3_model.DashScopeLlmClient()
        elif backend == "openai":
            base = os.environ.get("SMARTMALL_LLM_BASE_URL", "").strip()
            if not base:
                print("\n✗ --llm openai 需要指定服务地址。在 deploy/.env 里加：")
                print("    SMARTMALL_LLM_BASE_URL=https://api.deepseek.com")
                print("    SMARTMALL_LLM_API_KEY=sk-xxx")
                print("    SMARTMALL_TRIAGE_MODEL=deepseek-chat")
                print("    SMARTMALL_EXTRACT_MODEL=deepseek-chat")
                print("    SMARTMALL_STYLE_MODEL=deepseek-chat")
                return 1
            print(f"  直连 OpenAI 兼容服务：{base}")
            llm = gate3_model.LiteLlmClient(
                base_url=base,
                api_key=os.environ.get("SMARTMALL_LLM_API_KEY", ""),
            )
        else:
            base = os.environ.get("LITELLM_BASE_URL", "http://localhost:9000")
            print(f"  经 ai-gateway 调用模型：{base}")
            llm = gate3_model.LiteLlmClient(
                base_url=base, api_key=os.environ.get("LITELLM_API_KEY", "")
            )
    except gate3_model.LlmError as exc:
        print(f"\n✗ {exc}")
        return 1

    cfg3 = _gate3_config_from_env()
    if backend != "fake":
        print(f"  模型：粗筛 {cfg3.triage_model} / 抽取 {cfg3.extract_model}"
              f" / 风格 {cfg3.style_model}")

    # 商品→类目映射：知识条目的 category_id 靠它填，
    # 没有它覆盖度矩阵永远是 0%，检索也无法按类目收窄
    from sqlalchemy import text

    with dws.engine.connect() as conn:
        product_category = {
            r[0]: r[1]
            for r in conn.execute(text(
                "SELECT id, category_id FROM product WHERE deleted = 0"
            ))
        }
    print(f"  加载 {len(product_category)} 个商品的类目映射")

    batch_id = f"clean-{datetime.now():%Y%m%d%H%M%S}"
    try:
        out = run_pipeline(
            dialogues,
            llm,
            batch_id=batch_id,
            config=PipelineConfig(gate3=cfg3),
            product_category=product_category,
        )
    except gate3_model.LlmConfigError as exc:
        # 4xx：请求本身有问题，对每条输入都会同样失败，重试无用
        print(f"\n✗ {exc}")
        print("\n  这是「请求被拒绝」而不是「连不上」，重试不会成功。常见原因：")
        detail = str(exc)
        if "Quota" in detail or "quota" in detail or "arrears" in detail:
            print("    · 额度用尽或账户欠费 —— 这次就是它")
            print("      百炼控制台 → 费用中心：充值，并关掉「仅使用免费额度」")
            print("      两件事都要做，只充值不关开关照样 403")
        elif backend == "dashscope":
            print("    · API Key 无效或没有开通对应模型 → 到百炼控制台确认")
            print("    · 模型名不对 → 用 SMARTMALL_TRIAGE_MODEL 等环境变量覆盖")
        elif backend == "openai":
            print("    · 模型名与该厂商对不上 → SMARTMALL_*_MODEL 三个变量都要设")
            print("    · base_url 要写到域名层，不要带 /v1（代码会自己补）")
        else:
            print("    · ai-gateway 的 config.yaml 里没有配这个模型别名")
            print("    · LITELLM_API_KEY 与网关的 master key 不一致")
            print("  先用 --llm dashscope 直连排除网关自身的问题。")
        print("\n  换个厂商：--llm openai + SMARTMALL_LLM_BASE_URL（见 README）")
        print("  想先跑通链路不花钱：加 --fake-llm。")
        return 1
    except gate3_model.LlmUnavailableError as exc:
        # 快速失败：连不上模型时不该让几百条一条条超时跑完
        print(f"\n✗ {exc}")
        if backend == "gateway":
            print("  没起 ai-gateway 的话，用 --llm dashscope 直连模型。")
        return 1

    print()
    print(out.report.render())
    print()

    n_items = dws.save_knowledge_items(out.knowledge_items)
    # 待人工审核的也要落库。它们不会进索引（索引只取 approved/revised），
    # 但 ODS 那侧已标记为已处理——不写就等于凭空消失，再也捞不回来
    n_pending = dws.save_knowledge_items(out.pending_items)
    n_samples = dws.save_sft_samples(out.sft_samples)
    dws.record_job(batch_id, out.report.to_stats_json())
    # 必须紧跟着标记，否则重跑会把同一批对话再处理一遍
    n_marked = ods.mark_processed(out.ods_outcomes, batch_id)

    print(f"  ✓ 写入 knowledge_item {n_items + n_pending} 条"
          f"（{n_items} 条已通过、{n_pending} 条待人工审核）、"
          f"sft_sample {n_samples} 条")
    print(f"  ✓ 已标记 {n_marked} 条 ODS 记录为已处理")
    if out.unavailable_ods_ids:
        print(f"  ⚠ {len(out.unavailable_ods_ids)} 条因模型服务不可用未处理，"
              "未标记，重跑 clean 会继续处理")
    if n_items:
        print("\n  抽查产出：smartmall-pipeline peek")
    return 0


# ---------------------------------------------------------------- peek


def cmd_peek(args: argparse.Namespace) -> int:
    """抽查知识条目的实际内容。

    漏斗报表只说"产出 45 条"，说不了这 45 条是不是人话。用假模型跑出来的
    "合成答案0" 和真模型抽出来的真实话术，在报表上长得一模一样——
    所以放大批量之前得先肉眼看一眼。
    """
    from sqlalchemy import text

    repo = DwsRepository.from_env()
    where = ["deleted = 0"]
    params: dict[str, object] = {"n": args.limit}
    if args.status:
        where.append("review_status = :st")
        params["st"] = args.status
    if args.type:
        where.append("knowledge_type = :kt")
        params["kt"] = args.type

    sql = (
        "SELECT id, knowledge_type, review_status, confidence, quality_score, "
        "       category_id, product_ids, title, content, source, source_ref "
        f"FROM knowledge_item WHERE {' AND '.join(where)} "
        "ORDER BY id DESC LIMIT :n"
    )
    with repo.engine.connect() as conn:
        rows = list(conn.execute(text(sql), params))

    if not rows:
        print("没有符合条件的知识条目。先跑 `clean`。")
        return 0

    print(f"==> 最近 {len(rows)} 条知识条目（按 id 倒序）\n")
    for r in rows:
        m = r._mapping
        print(f"── #{m['id']}  {m['knowledge_type'] or '-'}"
              f"  {m['review_status']}"
              f"  置信 {float(m['confidence'] or 0):.2f}"
              f"  质量 {float(m['quality_score'] or 0):.2f}"
              f"  类目 {m['category_id'] or '-'}"
              f"  商品 {m['product_ids'] or '-'}")
        print(f"   问：{m['title']}")
        print(f"   答：{m['content']}")
        print(f"   来源：{m['source']} / {m['source_ref']}\n")

    # 占位符没还原、或答案里还留着手机号，都是脱敏链路出了问题
    leaked = [
        m["id"] for m in (r._mapping for r in rows)
        if re.search(r"1[3-9]\d{9}", f"{m['title']}{m['content']}")
    ]
    if leaked:
        print(f"⚠ 这些条目里疑似残留手机号，PII 脱敏没兜住：{leaked}")
    return 0


# ---------------------------------------------------------------- dedup


def cmd_dedup(args: argparse.Namespace) -> int:
    """清掉库里已有的同题重复知识。

    去重现在是清洗流水线的一环，但**已经入库的数据不会自动受益**。
    这条命令让人不必为了去重把几百段对话重新清洗一遍（还要再付一次
    模型调用的钱）。

    判据与流水线里完全一致：同 biz_type + 同标题 + 同关联商品即为同题。
    """
    from sqlalchemy import text

    repo = DwsRepository.from_env()

    # 只看已审核的：待审核的还没定稿，去重意义不大且会干扰人工判断
    with repo.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, biz_type, title, product_ids, confidence, quality_score,"
            " CHAR_LENGTH(content) AS clen "
            "FROM knowledge_item WHERE deleted = 0 "
            "AND review_status IN ('approved','revised') ORDER BY id"
        )).mappings().fetchall()

    groups: dict[tuple, list] = {}
    for r in rows:
        key = (r["biz_type"], (r["title"] or "").strip(), r["product_ids"] or "[]")
        groups.setdefault(key, []).append(r)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupes:
        print(f"  {len(rows)} 条已审核知识，没有同题重复。")
        return 0

    # 每组留最优的一条，判据与 dedup.merge_duplicates 一致
    to_delete: list[int] = []
    for members in dupes.values():
        members.sort(
            key=lambda r: (r["confidence"] or 0, r["quality_score"] or 0, r["clen"]),
            reverse=True,
        )
        to_delete.extend(r["id"] for r in members[1:])

    print(f"==> {len(rows)} 条已审核知识")
    print(f"  同题分组 {len(dupes)} 组，将删除 {len(to_delete)} 条重复")
    print(f"  重复率 {len(to_delete) / len(rows):.1%}")
    print("\n  样例：")
    for key, members in list(dupes.items())[:5]:
        ids = "、".join(f"#{m['id']}" for m in members[:6])
        print(f"    ×{len(members):<3} {pad(truncate(key[1], 36), 36)} {ids}")

    if not args.yes:
        print("\n  这是不可逆操作。确认无误后加 --yes 重新执行。")
        return 1

    from .rag import LocalVectorStore

    store = LocalVectorStore(engine=repo.engine)
    ids = ", ".join(str(i) for i in to_delete)
    with repo.engine.begin() as conn:
        conn.execute(text(
            f"UPDATE knowledge_item SET deleted = 1 WHERE id IN ({ids})"
        ))
    # 向量必须同时删掉，否则检索照样命中已删除的条目——
    # 软删只挡住了 SQL 查询，挡不住已经加载进内存的索引
    store.delete_by_item(to_delete)

    print(f"\n✅ 已删除 {len(to_delete)} 条重复知识及其向量")
    print("  重新加载索引：smartmall-agent chat（会自动读最新的）")
    return 0


# ---------------------------------------------------------------- handover


def cmd_handover(args: argparse.Namespace) -> int:
    """转人工工单：看盲点、补答案、回灌知识库。

    这是数据飞轮的后半圈。前半圈把历史对话变成知识，后半圈把
    「答不上来的问题」变成知识——后者更值钱，因为它由真实用户的
    真实提问驱动，而不是从存量数据里挖。
    """
    from .handover import HandoverImportError, render_blind_spots, to_knowledge_item

    repo = DwsRepository.from_env()

    if args.action == "list":
        spots = repo.blind_spots(limit=args.limit)
        print(f"==> 知识盲点（按被问次数排序）\n")
        print(render_blind_spots(spots))
        if spots:
            print(f"\n  补一条：smartmall-pipeline handover answer "
                  f"{spots[0].ticket_id} \"你的答案\"")
        return 0

    ticket = repo.fetch_ticket(args.ticket_id)
    if ticket is None:
        print(f"✗ 找不到工单 #{args.ticket_id}")
        return 1
    if ticket.status == "imported" and not args.force:
        print(f"✗ 工单 #{ticket.id} 已经回流过了（knowledge_item 已生成）。")
        print("  想重新回流加 --force，但会产生重复知识。")
        return 1

    print(f"==> 工单 #{ticket.id}")
    print(f"  问题：{ticket.question}")
    print(f"  原因：{ticket.reason}")

    # 类目要填上，否则覆盖度矩阵漏算这条——而"补上了哪块空白"
    # 正是回流最想回答的问题
    from sqlalchemy import text as _sql

    with repo.engine.connect() as conn:
        product_category = {
            r[0]: r[1] for r in conn.execute(
                _sql("SELECT id, category_id FROM product WHERE deleted = 0")
            )
        }

    try:
        item = to_knowledge_item(
            ticket, args.answer, product_category=product_category
        )
    except HandoverImportError as exc:
        print(f"✗ {exc}")
        return 1

    item_id = repo.save_knowledge_item_returning_id(item)
    repo.answer_ticket(ticket.id, args.answer, item_id)

    print(f"  ✓ 已生成知识条目 #{item_id}（{item.knowledge_type.value}，"
          f"类目 {item.category_id or '未知'}）")
    print("  ✓ 状态置为 imported")
    print("\n  它是 pending 状态，**不会**自动进索引——未经审核的知识")
    print("  绝不上线。人工确认后跑：")
    print(f"    smartmall-pipeline approve {item_id}")
    print("    smartmall-pipeline index")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """人工确认知识条目，使其可以进索引。

    这一步不能省。回流进来的答案是人工客服为**眼前这一个用户**写的，
    可能带着这单特有的让步（"这次给您补个运费"），直接当通用知识
    上线会把一次性的特例变成对所有人的承诺。
    """
    from sqlalchemy import text

    repo = DwsRepository.from_env()
    ids = ", ".join(str(int(i)) for i in args.item_ids)
    with repo.engine.begin() as conn:
        n = conn.execute(text(
            f"UPDATE knowledge_item SET review_status = 'approved',"
            f" embedding_status = 'pending' WHERE id IN ({ids})"
            f" AND review_status = 'pending'"
        )).rowcount
    print(f"  ✓ 已通过 {n} 条（原本就不是 pending 的不会被改）")
    if n:
        print("  下一步：smartmall-pipeline index")
    return 0


# ---------------------------------------------------------------- stats


def cmd_stats(args: argparse.Namespace) -> int:
    """各层数据量概览。"""
    from sqlalchemy import text

    repo = DwsRepository.from_env()
    queries = [
        ("ODS 原始对话", "SELECT COUNT(*) FROM ods_raw_dialogue"),
        # 待处理量是流水线最重要的一个数：clean 说"没有待处理的对话"时，
        # 得能立刻分清是"都清完了"还是"ODS 本身就是空的"
        ("  已处理", "SELECT COUNT(*) FROM ods_process_log"),
        ("    其中产出知识", "SELECT COUNT(*) FROM ods_process_log WHERE outcome='passed'"),
        ("    其中被淘汰", "SELECT COUNT(*) FROM ods_process_log WHERE outcome='dropped'"),
        ("  待处理", "SELECT COUNT(*) FROM ods_raw_dialogue o "
                     "LEFT JOIN ods_process_log p ON p.ods_id = o.id "
                     "WHERE p.id IS NULL"),
        ("DWD 标准化会话", "SELECT COUNT(*) FROM dwd_dialogue_session"),
        ("知识条目（全部）", "SELECT COUNT(*) FROM knowledge_item WHERE deleted=0"),
        ("  已审核", "SELECT COUNT(*) FROM knowledge_item WHERE deleted=0 AND review_status IN ('approved','revised')"),
        ("  待审核", "SELECT COUNT(*) FROM knowledge_item WHERE deleted=0 AND review_status='pending'"),
        ("  待向量化", "SELECT COUNT(*) FROM knowledge_item WHERE deleted=0 AND embedding_status IN ('pending','stale')"),
        ("微调样本", "SELECT COUNT(*) FROM sft_sample WHERE deleted=0"),
        ("数据集版本", "SELECT COUNT(*) FROM dataset_version"),
        ("清洗任务记录", "SELECT COUNT(*) FROM ds_job"),
        ("Agent 对话埋点", "SELECT COUNT(*) FROM agent_trace"),
        ("  点赞 / 点踩", "SELECT CONCAT(SUM(thumb=1), ' / ', SUM(thumb=-1)) "
                        "FROM agent_trace WHERE thumb IS NOT NULL"),
        ("转人工工单", "SELECT COUNT(*) FROM handover_ticket"),
        ("  待补答案", "SELECT COUNT(*) FROM handover_ticket WHERE status='open'"),
        ("  已回灌知识库", "SELECT COUNT(*) FROM handover_ticket "
                        "WHERE status='imported'"),
    ]

    print(f"==> {_env_summary()}\n")
    with repo.engine.connect() as conn:
        for label, sql in queries:
            n = conn.execute(text(sql)).scalar()
            print(f"  {pad(label, 20)} {n:>8}")

        rows = conn.execute(text(
            "SELECT source, COUNT(*) FROM knowledge_item WHERE deleted=0 "
            "GROUP BY source ORDER BY COUNT(*) DESC"
        )).fetchall()
        if rows:
            print("\n  按来源分布：")
            for source, n in rows:
                print(f"    {pad(source, 18)} {n:>8}")
    return 0


# ---------------------------------------------------------------- coverage


def cmd_coverage(args: argparse.Namespace) -> int:
    """知识覆盖度矩阵与补写任务。"""
    from sqlalchemy import text

    from .models import BizType, KnowledgeItem

    repo = DwsRepository.from_env()
    with repo.engine.connect() as conn:
        cats = {
            r[0]: r[1] for r in conn.execute(text(
                "SELECT id, name FROM category WHERE level=3 AND deleted=0"
            ))
        }
        rows = conn.execute(text(
            "SELECT category_id, knowledge_type, content, title, source, source_ref "
            "FROM knowledge_item WHERE deleted=0 "
            "AND review_status IN ('approved','revised')"
        )).mappings().fetchall()

    if not cats:
        print("  category 表里没有三级类目，先补充商品类目数据")
        return 1

    items = [
        KnowledgeItem(
            biz_type=BizType.QA, content=r["content"], title=r["title"],
            source=r["source"], source_ref=r["source_ref"],
            category_id=r["category_id"],
            knowledge_type=KnowledgeType(r["knowledge_type"] or "other"),
        )
        for r in rows
    ]

    matrix = coverage_mod.build_matrix(items, cats)
    print(matrix.render())

    tasks = coverage_mod.generate_write_tasks(matrix, limit=args.limit)
    if not tasks:
        print("\n  所有格子都达标，无需补写")
        return 0

    print(f"\n补写任务（前 {len(tasks)} 条）：")
    for t in tasks:
        print(f"  [P{t.priority}] {t.category_name} × {t.knowledge_type}"
              f"  缺 {t.gap} 条 —— {t.reason}")
    return 0


# ---------------------------------------------------------------- index / search


def cmd_index(args: argparse.Namespace) -> int:
    """把已审核的知识条目向量化写入索引。

    对应 dag_kb_incremental_index 的手动版本：只处理
    ``embedding_status IN ('pending','stale')`` 的条目。
    ``stale`` 是内容改过但向量没跟上的状态——不重新算的话，
    检索命中的是旧向量、返回的是新内容，两者对不上。
    """
    from sqlalchemy import text

    from .rag import LocalVectorStore, build_provider
    from .rag.embedding import EmbeddingError

    repo = DwsRepository.from_env()

    try:
        provider = build_provider(args.embedding)
    except EmbeddingError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"==> 向量化后端：{provider.name}")

    store = LocalVectorStore(engine=repo.engine)

    # 换后端必须在**写入前**拦住。混用检测在加载索引时才报错，
    # 那时候已经晚了：被拦住的是下游（Agent 起不来），
    # 而制造混用的是这一步。
    existing = store.providers_in_index()
    others = {p: n for p, n in existing.items() if p and p != provider.name}
    if others and not args.rebuild:
        print(f"\n✗ 索引里已有其他后端的向量：{others}")
        print(f"  当前后端是 {provider.name}，混进去会让相似度失去意义。")
        print("  用 `index --rebuild` 全量重建（会删掉旧后端的向量）。")
        return 1

    where = (
        "k.deleted = 0 AND k.review_status IN ('approved','revised')"
        if args.rebuild
        else "k.deleted = 0 AND k.review_status IN ('approved','revised') "
             "AND k.embedding_status IN ('pending','stale')"
    )
    with repo.engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT k.id, k.biz_type, k.title, k.content FROM knowledge_item k "
            f"WHERE {where} ORDER BY k.id LIMIT :limit"
        ), {"limit": args.limit}).mappings().fetchall()

    if not rows:
        print("  没有待向量化的条目。加 --rebuild 可全量重建。")
        return 0
    print(f"  待处理 {len(rows)} 条")

    if args.rebuild and len(rows) == args.limit:
        # 重建被 limit 截断的话，取不到的条目会连向量都没有——
        # 检索时静默漏掉，比"混用"更隐蔽：不报错，只是永远查不到
        print(f"\n✗ 重建时正好命中 --limit {args.limit}，可能还有条目没取到。")
        print("  调大 --limit 重试，否则部分知识会没有向量、永远检索不到。")
        return 1

    # 检索文本要带上标题：用户的提问更接近「问题」而非「答案」，
    # 只索引答案会显著降低召回
    payload = [
        (r["id"], 0, f"{r['title']}\n{r['content']}" if r["title"] else r["content"])
        for r in rows
    ]

    if args.rebuild:
        # REPLACE INTO 只覆盖主键相同的行。切分数量变了、条目被下架、
        # 或者换了后端，都会留下没人覆盖到的旧行——它们正是"混用"的来源。
        # 重建就要重建干净，先删再写。
        n_other = store.delete_other_providers(provider.name)
        store.delete_by_item([r["id"] for r in rows])
        if n_other:
            print(f"  已清理其他后端的旧向量 {n_other} 行")

    # 批次上限由 provider 声明——调用方猜错的后果是跑到一半被服务端 400
    batch = args.batch_size or getattr(provider, "max_batch", 10)
    print(f"  批次大小 {batch}")
    done = 0
    for i in range(0, len(payload), batch):
        chunk = payload[i : i + batch]
        try:
            vectors = provider.embed([t for _, _, t in chunk])
        except EmbeddingError as exc:
            print(f"\n✗ 第 {i} 批向量化失败：{exc}")
            print(f"  已完成 {done} 条，重跑本命令会从断点继续")
            return 1
        store.upsert(
            [(iid, seq, txt, vec) for (iid, seq, txt), vec in zip(chunk, vectors)],
            provider.name,
        )
        repo.mark_indexed([iid for iid, _, _ in chunk])
        done += len(chunk)
        print(f"\r  已向量化 {done}/{len(payload)}", end="", flush=True)

    print(f"\n  ✓ 完成 {done} 条")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """检索调试台：输入问题，看混合检索命中什么。"""
    from .rag import LocalVectorStore, build_provider, embed_query
    from .rag.embedding import EmbeddingError

    repo = DwsRepository.from_env()
    store = LocalVectorStore(engine=repo.engine)

    n = store.load()
    if n == 0:
        print("  索引为空。先跑 `index` 建索引。")
        return 1
    print(f"==> 索引已加载 {n} 条（后端 {store.provider_name}）")

    try:
        provider = build_provider(args.embedding)
        qvec = embed_query(provider, args.query)
    except EmbeddingError as exc:
        print(f"✗ {exc}")
        return 1

    hits = store.search(
        args.query, qvec,
        top_k=args.top_k,
        product_ids=[args.product_id] if args.product_id else None,
        category_id=args.category_id,
    )

    print(f"\n问题：{args.query}")
    print("=" * 68)

    # 拒答判据用余弦相似度，不能用 RRF 分——后者只反映排名，
    # 最大值恒为 2/(k+1)，与查询是否真的匹配无关。
    # 注意本地实现**没有 rerank**，所以这个阈值比 docs/04 里
    # rerank 之后的 0.5 宽松；接上 bge-reranker 后应改用那个阈值。
    usable = [
        h for h in hits
        if h.dense_score >= args.min_similarity or h.bm25_score > 0
    ]

    if not hits or not usable:
        print("无命中。")
        print(f"  最高相似度 {hits[0].dense_score:.3f} < 阈值 {args.min_similarity}"
              if hits else "  召回为空")
        print("\n  按设计此时应拒答并转人工，而不是让模型硬编——")
        print("  电商客服说错话的代价是真实的退货与投诉。")
        return 0

    for i, h in enumerate(usable, 1):
        kw = "  关键词命中" if h.bm25_score > 0 else ""
        print(f"\n[{i}] 相似度={h.dense_score:.3f}  RRF={h.score:.4f}{kw}")
        print(f"    item={h.item_id}  {h.biz_type}/{h.knowledge_type}")
        text_ = h.text.replace("\n", " / ")
        print(f"    {text_[:120]}{'…' if len(text_) > 120 else ''}")
        if h.asset_ids:
            print(f"    素材: {list(h.asset_ids)}")

    if len(usable) < len(hits):
        print(f"\n  （另有 {len(hits) - len(usable)} 条因相似度过低被滤除）")
    print()
    return 0


# ---------------------------------------------------------------- reset


# 派生数据：全部可以从 ODS 重新算出来，因此清空是安全的。
# 顺序有讲究——先清引用方再清被引用方。
DERIVED_TABLES = [
    "knowledge_item",
    "sft_sample",
    "ods_process_log",
    "dwd_dialogue_turn",
    "dwd_dialogue_session",
    "dwd_clip_segment",
    "ds_job",
    "dataset_version",
]


def cmd_reset(args: argparse.Namespace) -> int:
    """清空派生数据，让 ODS 可以重新清洗。

    这条命令是「ODS 只增不改」这条原则的兑现方式：原始数据永远留着，
    所以清洗规则改了、类目映射修了、模型换了，都可以把派生结果丢掉重算，
    而不需要重新采集数据。

    默认只清派生层；``--include-ods`` 才会连原始数据一起删。
    """
    from sqlalchemy import text

    repo = DwsRepository.from_env()
    tables = list(DERIVED_TABLES)
    if args.include_ods:
        tables += ["ods_raw_dialogue", "ods_raw_asset", "ods_raw_clip", "ods_raw_doc"]

    print(f"==> 目标库 {_env_summary()}")
    with repo.engine.connect() as conn:
        counts = {
            t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in tables
        }

    nonempty = {t: n for t, n in counts.items() if n}
    if not nonempty:
        print("  所有目标表已是空的，无需操作")
        return 0

    print("  将清空：")
    for t, n in nonempty.items():
        print(f"    {pad(t, 24)} {n:>8} 行")

    if args.include_ods:
        print("\n  ⚠ --include-ods 会删掉原始数据，删了就只能重新采集")

    if not args.yes:
        print("\n  这是不可逆操作。确认无误后加 --yes 重新执行。")
        return 1

    with repo.engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for t in tables:
            conn.execute(text(f"TRUNCATE TABLE {t}"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    print(f"\n✅ 已清空 {len(tables)} 张表")
    if not args.include_ods:
        with repo.engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM ods_raw_dialogue")).scalar()
        print(f"   ODS 保留 {n} 条原始对话，直接跑 `clean` 即可重新清洗")
    return 0


# ---------------------------------------------------------------- publish


def cmd_publish(args: argparse.Namespace) -> int:
    """发布数据资产版本（含质量门禁）。"""
    from sqlalchemy import text

    from .models import BizType, KnowledgeItem, ReviewStatus

    repo = DwsRepository.from_env()
    with repo.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, biz_type, modality, title, content, source, source_ref, "
            "       quality_score, review_status, category_id, valid_to "
            "FROM knowledge_item WHERE deleted=0 "
            "AND review_status IN ('approved','revised')"
        )).mappings().fetchall()

    items = [
        KnowledgeItem(
            id=r["id"], biz_type=BizType(r["biz_type"]), modality=r["modality"],
            title=r["title"], content=r["content"], source=r["source"],
            source_ref=r["source_ref"], quality_score=r["quality_score"],
            review_status=ReviewStatus(r["review_status"]),
            category_id=r["category_id"], valid_to=r["valid_to"],
        )
        for r in rows
    ]

    publishable = publish_mod.select_publishable(items)
    print(f"==> 候选 {len(items)} 条，过期过滤后 {len(publishable)} 条")

    result = publish_mod.publish(
        publishable, args.version,
        snapshot_dir=args.snapshot_dir, force=args.force,
    )
    print()
    print(result.render())

    if result.passed and result.snapshot_path:
        print(f"\n  快照：{result.snapshot_path}")
    return 0 if result.passed else 1


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="smartmall-pipeline",
        description="smartMall 数据中台流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--env-file",
        help="指定 .env 文件；默认依次尝试 ./.env 与 <仓库根>/deploy/.env",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("check", help="检查数据库连通与表结构")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("ingest", help="生成合成对话写入 ODS")
    s.add_argument("--count", type=int, default=300)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--duplicate-rate", type=float, default=0.1,
                   help="近重复比例，用于验证去重是否生效")
    s.add_argument("--batch-id")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("clean", help="跑四道清洗关卡")
    s.add_argument("--limit", type=int, default=5000)
    s.add_argument("--fake-llm", action="store_true",
                   help="用假 LLM，不调真实模型、不产生费用")
    s.add_argument("--llm", default=None,
                   choices=["dashscope", "openai", "gateway"],
                   help="模型通道。不指定时：配了 SMARTMALL_LLM_BASE_URL 走 openai，"
                        "否则走 dashscope。"
                        "openai 接任何 OpenAI 兼容服务（DeepSeek、Kimi、本地 vLLM）；"
                        "gateway 经 ai-gateway（有统一记账与降级）")
    s.add_argument("--items-per-dialogue", type=int, default=2,
                   help="仅 --fake-llm 时有效")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("peek", help="抽查知识条目的实际内容")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--status", help="只看某个审核状态，如 approved / pending")
    s.add_argument("--type", help="只看某个知识类型，如 spec / logistics / sizing")
    s.set_defaults(func=cmd_peek)

    s = sub.add_parser("dedup", help="清掉库里已有的同题重复知识")
    s.add_argument("--yes", action="store_true", help="确认执行")
    s.set_defaults(func=cmd_dedup)

    s = sub.add_parser("handover", help="转人工工单：看盲点、补答案、回灌")
    hsub = s.add_subparsers(dest="action", required=True)
    h = hsub.add_parser("list", help="按被问次数排序的知识盲点")
    h.add_argument("--limit", type=int, default=30)
    h = hsub.add_parser("answer", help="补上人工答案并生成待审核知识")
    h.add_argument("ticket_id", type=int)
    h.add_argument("answer")
    h.add_argument("--force", action="store_true", help="允许对已回流的工单重做")
    s.set_defaults(func=cmd_handover)

    s = sub.add_parser("approve", help="人工确认知识条目，使其可进索引")
    s.add_argument("item_ids", type=int, nargs="+")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("stats", help="各层数据量概览")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("coverage", help="知识覆盖度矩阵")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_coverage)

    s = sub.add_parser("index", help="把已审核知识向量化写入索引")
    s.add_argument("--embedding", default="dashscope",
                   help="向量化后端：dashscope（走 API，默认）或 local（本地 bge-m3）")
    s.add_argument("--limit", type=int, default=5000)
    s.add_argument("--batch-size", type=int, default=None,
                   help="单批条数；默认取所选后端声明的上限")
    s.add_argument("--rebuild", action="store_true",
                   help="全量重建，而非只处理 pending/stale")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("search", help="检索调试台")
    s.add_argument("query", help="要检索的问题")
    s.add_argument("--embedding", default="dashscope")
    s.add_argument("--top-k", type=int, default=5)
    s.add_argument("--product-id", type=int, help="按商品收窄")
    s.add_argument("--category-id", type=int, help="按类目收窄")
    s.add_argument("--min-similarity", type=float, default=0.35,
                   help="余弦相似度下限，低于此值视为无命中。"
                        "本地实现无 rerank，故比 docs/04 的 0.5 宽松")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("reset", help="清空派生数据，保留 ODS 以便重新清洗")
    s.add_argument("--yes", action="store_true", help="确认执行（不加只做预览）")
    s.add_argument("--include-ods", action="store_true",
                   help="连原始数据一起删（谨慎：删了只能重新采集）")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("publish", help="发布数据资产版本")
    s.add_argument("--version", required=True, help="如 kb-v1")
    s.add_argument("--snapshot-dir", default="./snapshots")
    s.add_argument("--force", action="store_true",
                   help="跳过阻断性门禁（会在 stats 留审计标记）")
    s.set_defaults(func=cmd_publish)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 先加载 .env，再执行命令——省掉每开一个终端都要重设环境变量
    if args.env_file:
        path = Path(args.env_file)
        if not path.is_file():
            print(f"✗ 找不到 {path}")
            return 1
        n = _load_env_file(path)
        print(f"  已加载 {path}（{n} 项）")
    else:
        for path in _default_env_files():
            n = _load_env_file(path)
            if n:
                print(f"  已加载 {path}（{n} 项）")
                break

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except BrokenPipeError:
        # 输出被 head / more 之类截断。这是正常使用方式，不该甩 traceback。
        # 需要重定向 stdout，否则解释器退出时 flush 会再抛一次。
        try:
            sys.stdout.close()
        finally:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except Exception as exc:  # noqa: BLE001
        # 数据库连不上是最常见的失败，甩一屏 traceback 对使用者毫无帮助。
        # 只有真正意外的异常才保留堆栈。
        if _is_connection_error(exc):
            print(f"\n✗ 连接数据库失败：{_root_cause(exc)}")
            print(f"  当前配置：{_env_summary()}")
            print("\n  排查方向：")
            print("    · MySQL 服务是否启动")
            print("    · MYSQL_USER / MYSQL_PASSWORD 是否与实际一致")
            print("    · 该用户是否有这个库的权限")
            print("\n  先跑 `check` 可以一次性验证连通性与表结构。")
            return 1
        raise


def _root_cause(exc: BaseException) -> str:
    cause = exc
    while cause.__cause__ is not None:
        cause = cause.__cause__
    return str(cause).strip() or type(cause).__name__


def _is_connection_error(exc: BaseException) -> bool:
    names = set()
    cur: BaseException | None = exc
    while cur is not None:
        names.add(type(cur).__name__)
        cur = cur.__cause__
    return bool(
        names & {"OperationalError", "ConnectionRefusedError", "InterfaceError"}
    )


if __name__ == "__main__":
    sys.exit(main())
