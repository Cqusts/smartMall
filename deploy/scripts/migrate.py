#!/usr/bin/env python3
"""建库、建表、按顺序应用 deploy/sql/migrations/ 下的迁移。

    python deploy/scripts/migrate.py              建库 + 建表 + 应用待执行的迁移
    python deploy/scripts/migrate.py --status     只看状态，不改数据库

连的是**本机 MySQL**，走 PyMySQL 直连，不需要 docker，也不需要 mysql.exe 在
PATH 上 —— Windows 上装了 MySQL 服务但命令行客户端不在 PATH 是常态。

**为什么需要一个真的迁移器，而不是 `for f in *.sql; do mysql < $f; done`：**
迁移里有 ALTER TABLE ... ADD COLUMN，那不是幂等的 —— 第二次跑直接
「Duplicate column name 'request_id'」。而这个脚本注定被反复执行（每次拉完
代码都该跑一遍），所以必须知道哪些做过：一张 schema_migrations 表记文件名。

**基础表也归它建。**deploy/sql/mysql/*.sql 那几个建表脚本，本机 MySQL 没有
任何机制会自动执行，库是空的直接跑 migrations 会在第一条 ALTER TABLE 上
找不到表。所以检测到空库就先把它们跑掉。

**应用账号也归它建。**迁移用管理员账号（root），而 mall-* 与 ai-agent 连库
用的是 smartmall —— 不建这个账号，迁移会成功而应用连不上，页面表现成
「商品数据读取失败」，很难联想到是缺账号。见 ensure_app_user()。

连接参数看 smartmall_env.py：管理员是 MYSQL_ADMIN_USER/MYSQL_ADMIN_PASSWORD，
应用账号是 MYSQL_USER/MYSQL_PASSWORD，两套不能混用。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smartmall_env as env  # noqa: E402

env.load_env()

ROOT = env.ROOT
MIGRATIONS = ROOT / "deploy" / "sql" / "migrations"
BASE_SCHEMA = ROOT / "deploy" / "sql" / "mysql"

DATABASE = env.database()
ADMIN_USER, ADMIN_PASSWORD = env.admin_credentials()
APP_USER, APP_PASSWORD = env.app_credentials()


# ---------------------------------------------------------------- SQL 拆分

def split_statements(sql_text: str) -> list[str]:
    """把一个 .sql 文件切成一条条语句。

    只处理这些 .sql 实际用到的语法：`--` 行注释、单/双引号字符串、反引号标识符。
    没有 DELIMITER、存储过程和触发器（有的话按分号切必然出错），所以够用。
    引号内的分号不切、注释里的分号不切 —— 这两点是最容易写错的地方。
    """
    out, buf = [], []
    quote = None          # 当前所在的引号类型，None 表示在引号外
    i, n = 0, len(sql_text)
    while i < n:
        ch = sql_text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:      # 转义：反斜杠后面那个字符原样吃掉
                buf.append(sql_text[i + 1]); i += 2; continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch; buf.append(ch); i += 1; continue
        if ch == "-" and sql_text.startswith("--", i):
            j = sql_text.find("\n", i)          # 行注释：整行丢掉
            i = n if j == -1 else j + 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []; i += 1; continue
        buf.append(ch); i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


#: 「这条语句的效果已经存在」对应的 MySQL 错误码。
#:
#: 迁移里的 ALTER TABLE ADD COLUMN 不幂等，而现实中总有库是手工执行过一部分的
#: （本项目就是：早期让人手敲过 007/008）。逐条语句执行、把这些错误当作
#: 「这部分已经做过」跳过，整个文件就变成幂等的 —— 缺的补上、有的略过，
#: 最终收敛到同一个 schema。
#:
#: 只容忍「已存在」这一类。语法错误、外键失败等一律照常报错中断。
ALREADY_APPLIED_ERRNOS = {
    1050,  # Table already exists
    1060,  # Duplicate column name
    1061,  # Duplicate key name
    1091,  # Can't DROP; check that column/key exists
    1826,  # Duplicate foreign key constraint name
}


def errno_of(exc: Exception) -> int | None:
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


# ---------------------------------------------------------------- 数据库访问

def connect(db: str | None = DATABASE):
    return env.connect(ADMIN_USER, ADMIN_PASSWORD, db)


def sql(statement: str, db: str | None = DATABASE) -> list[tuple]:
    with connect(db) as conn, conn.cursor() as cur:
        cur.execute(statement)
        return list(cur.fetchall() or ())


def scalar(statement: str) -> int:
    rows = sql(statement)
    return int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0


def run_file(path: Path) -> None:
    """逐条执行一个 .sql 文件，跳过「效果已存在」的语句。

    整份文件一把梭做不到跳过单条 —— 撞上第一个 Duplicate column 就整体失败，
    后面真正缺的东西也补不上了。
    """
    skipped = 0
    with connect() as conn, conn.cursor() as cur:
        for stmt in split_statements(path.read_text(encoding="utf-8")):
            try:
                cur.execute(stmt)
            except Exception as exc:
                if errno_of(exc) in ALREADY_APPLIED_ERRNOS:
                    skipped += 1
                    continue
                raise
    if skipped:
        print(f"      （其中 {skipped} 条的效果已存在，跳过）")


# ---------------------------------------------------------------- 建库建账号

def ensure_database() -> None:
    with env.connect(ADMIN_USER, ADMIN_PASSWORD, None) as conn, conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DATABASE}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")


def ensure_app_user() -> None:
    """建应用账号、把密码校准到期望值，并**真的连一次**确认可用。

    两个坑都实测踩过：

    · **CREATE USER IF NOT EXISTS 不会改已存在账号的密码。**机器上早就有一个
      同名账号（别的项目留下的、或手工建过）时，它静默跳过，密码还是旧的，
      应用照样 Access denied。所以后面必须跟一句 ALTER USER 把密码校准。

    · **'u'@'%' 与 'u'@'localhost' 是两个独立账号。**只修好其中一个时，
      走 TCP 可能匹配 %（通）、走 socket 匹配 localhost（不通），
      于是「我明明连得上」和「应用连不上」同时成立。两个都要处理。

    最后那次试连是关键：SQL 没报错不等于账号能用 —— 早先的版本就是只看
    「CREATE 没抛异常」就打了 ✓，而实际密码是错的。
    """
    if APP_USER == ADMIN_USER:
        return                      # 应用就用管理员账号，不用另建
    try:
        with connect(None) as conn, conn.cursor() as cur:
            for h in ("%", "localhost"):
                cur.execute(f"CREATE USER IF NOT EXISTS '{APP_USER}'@'{h}' "
                            f"IDENTIFIED BY '{APP_PASSWORD}'")
                # 账号已存在时上一句什么都不做，靠这句把密码校准
                cur.execute(f"ALTER USER '{APP_USER}'@'{h}' "
                            f"IDENTIFIED BY '{APP_PASSWORD}'")
                cur.execute(f"GRANT ALL PRIVILEGES ON `{DATABASE}`.* "
                            f"TO '{APP_USER}'@'{h}'")
            cur.execute("FLUSH PRIVILEGES")
    except Exception as exc:
        # 管理员账号没有建用户的权限时不该中断迁移 —— 表建好了仍然有价值
        print(f"  ⚠ 没能创建/校准应用账号 {APP_USER}：{exc}")
        print(f"    应用默认用 {APP_USER}/{APP_PASSWORD} 连库。要么手动建它，")
        print(f'    要么让应用改用现在这个账号：$env:MYSQL_USER="{ADMIN_USER}"')
        return

    try:
        env.connect(APP_USER, APP_PASSWORD, DATABASE).close()
        print(f"  ✓ 应用账号 {APP_USER} 可用（已试连确认；应用用它，不是 {ADMIN_USER}）")
    except Exception as exc:
        print(f"  ⚠ 应用账号 {APP_USER} 建好了，但试连失败：{exc}")
        print("    应用会连不上库，页面表现为「商品数据读取失败」。")


def bootstrap_base_schema() -> None:
    ensure_database()
    ensure_app_user()
    have = scalar("SELECT COUNT(*) FROM information_schema.tables "
                  f"WHERE table_schema='{DATABASE}' AND table_name='knowledge_item'")
    if have:
        return
    base = sorted(BASE_SCHEMA.glob("*.sql"), key=lambda p: p.name)
    if not base:
        return
    print("  库是空的，先跑基础建表脚本：")
    for path in base:
        run_file(path)
        print(f"    ✓ {path.name}")


# ---------------------------------------------------------------- 主流程

def connection_help(exc: Exception) -> None:
    print(f"✗ 连不上本机 MySQL（{env.host()}:{env.port()}，库 {DATABASE}）")
    print(f"  {exc}")
    print()
    print("  当前用的管理员凭据：")
    print(f"    MYSQL_ADMIN_USER={ADMIN_USER}")
    print(f"    MYSQL_ADMIN_PASSWORD={'已设置' if ADMIN_PASSWORD else '(空 —— 多半就是这个问题)'}")
    print()
    print("  PowerShell 里设一下再跑：")
    print('      $env:MYSQL_ADMIN_PASSWORD="你的 root 密码"')
    print("      python deploy/scripts/migrate.py")


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg not in ("", "--status"):
        print(f"未知参数：{arg}（可用：--status）")
        return 2
    status_only = arg == "--status"

    print(f"==> 迁移（本机 MySQL {env.host()}:{env.port()}，库 {DATABASE}）")

    try:
        if not status_only:
            bootstrap_base_schema()
        sql("SELECT 1")
    except Exception as exc:
        connection_help(exc)
        return 1

    sql("CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  filename   VARCHAR(190) NOT NULL PRIMARY KEY,"
        "  applied_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='已应用的迁移'")

    done = {r[0] for r in sql("SELECT filename FROM schema_migrations")}
    files = sorted(MIGRATIONS.glob("*.sql"), key=lambda p: p.name)

    # 001 只对「导过初版 30 张表的老库」有意义。全新安装里它要补的东西
    # 基础建表脚本已经建好了，跑它反而会重复定义，所以直接记为已应用。
    first = next((f for f in files if f.name.startswith("001_")), None)
    if first is not None and first.name not in done:
        if scalar("SELECT COUNT(*) FROM information_schema.columns "
                  f"WHERE table_schema='{DATABASE}' AND table_name='knowledge_item' "
                  "AND column_name='knowledge_type'"):
            if not status_only:
                sql("INSERT IGNORE INTO schema_migrations(filename) "
                    f"VALUES('{first.name}')")
            done.add(first.name)
            print(f"  ⤼ {first.name}（全新安装已内含，标记为已应用）")

    applied = skipped = 0
    for path in files:
        if path.name in done:
            skipped += 1
            continue
        if status_only:
            print(f"  待应用  {path.name}")
            applied += 1
            continue
        try:
            run_file(path)
        except Exception as exc:
            print(f"  ✗ {path.name}")
            for ln in str(exc).splitlines()[:4]:
                print(f"      {ln}")
            print()
            print("❌ 迁移中断。修好上面那个错误后重跑，已应用的不会重复执行。")
            return 1
        sql(f"INSERT INTO schema_migrations(filename) VALUES('{path.name}')")
        print(f"  ✓ {path.name}")
        applied += 1

    print()
    if status_only:
        print(f"共 {applied} 个待应用，{skipped} 个已应用。")
    else:
        print(f"✅ 本次应用 {applied} 个，跳过 {skipped} 个已应用的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
