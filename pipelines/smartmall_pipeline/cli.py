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
import sys
from datetime import datetime
from pathlib import Path

from . import coverage as coverage_mod
from . import publish as publish_mod
from .gates import gate3_model
from .ingest import synthetic
from .models import KnowledgeType
from .orchestrator import run_pipeline
from .repository import DwsRepository, OdsRepository

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


def cmd_clean(args: argparse.Namespace) -> int:
    """从 ODS 拉取未处理对话，跑四道关卡，产出写回 DWS。"""
    ods = OdsRepository.from_env()
    dws = DwsRepository.from_env()

    print(f"==> 从 ODS 拉取最多 {args.limit} 条未处理对话")
    dialogues = ods.fetch_unprocessed(limit=args.limit)
    if not dialogues:
        print("  没有待处理的对话。先跑 `cli ingest` 造一批。")
        return 0
    print(f"  取到 {len(dialogues)} 条")

    if args.fake_llm:
        print("  使用 FakeLlmClient（不调用真实模型，不产生费用）")
        llm = gate3_model.FakeLlmClient(items_per_dialogue=args.items_per_dialogue)
    else:
        base = os.environ.get("LITELLM_BASE_URL", "http://localhost:9000")
        print(f"  经 ai-gateway 调用模型：{base}")
        llm = gate3_model.LiteLlmClient(
            base_url=base, api_key=os.environ.get("LITELLM_API_KEY", "")
        )

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
    out = run_pipeline(
        dialogues, llm, batch_id=batch_id, product_category=product_category
    )

    print()
    print(out.report.render())
    print()

    n_items = dws.save_knowledge_items(out.knowledge_items)
    n_samples = dws.save_sft_samples(out.sft_samples)
    dws.record_job(batch_id, out.report.to_stats_json())
    # 必须紧跟着标记，否则重跑会把同一批对话再处理一遍
    n_marked = ods.mark_processed(out.ods_outcomes, batch_id)

    print(f"  ✓ 写入 knowledge_item {n_items} 条、sft_sample {n_samples} 条")
    print(f"  ✓ 待人工处理 {out.pending_human} 条（Label Studio 队列）")
    print(f"  ✓ 已标记 {n_marked} 条 ODS 记录为已处理")
    return 0


# ---------------------------------------------------------------- stats


def cmd_stats(args: argparse.Namespace) -> int:
    """各层数据量概览。"""
    from sqlalchemy import text

    repo = DwsRepository.from_env()
    queries = [
        ("ODS 原始对话", "SELECT COUNT(*) FROM ods_raw_dialogue"),
        ("DWD 标准化会话", "SELECT COUNT(*) FROM dwd_dialogue_session"),
        ("知识条目（全部）", "SELECT COUNT(*) FROM knowledge_item WHERE deleted=0"),
        ("  已审核", "SELECT COUNT(*) FROM knowledge_item WHERE deleted=0 AND review_status IN ('approved','revised')"),
        ("  待审核", "SELECT COUNT(*) FROM knowledge_item WHERE deleted=0 AND review_status='pending'"),
        ("  待向量化", "SELECT COUNT(*) FROM knowledge_item WHERE deleted=0 AND embedding_status IN ('pending','stale')"),
        ("微调样本", "SELECT COUNT(*) FROM sft_sample WHERE deleted=0"),
        ("数据集版本", "SELECT COUNT(*) FROM dataset_version"),
        ("清洗任务记录", "SELECT COUNT(*) FROM ds_job"),
    ]

    print(f"==> {_env_summary()}\n")
    with repo.engine.connect() as conn:
        for label, sql in queries:
            n = conn.execute(text(sql)).scalar()
            print(f"  {label:<20} {n:>8}")

        rows = conn.execute(text(
            "SELECT source, COUNT(*) FROM knowledge_item WHERE deleted=0 "
            "GROUP BY source ORDER BY COUNT(*) DESC"
        )).fetchall()
        if rows:
            print("\n  按来源分布：")
            for source, n in rows:
                print(f"    {source:<18} {n:>8}")
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
        print(f"    {t:<24} {n:>8} 行")

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
    s.add_argument("--items-per-dialogue", type=int, default=2,
                   help="仅 --fake-llm 时有效")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("stats", help="各层数据量概览")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("coverage", help="知识覆盖度矩阵")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_coverage)

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
