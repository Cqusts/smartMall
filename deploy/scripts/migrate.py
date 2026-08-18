#!/usr/bin/env python3
"""按顺序应用 deploy/sql/migrations/ 下的迁移，并记录已应用的版本。

**为什么需要一个真的迁移器，而不是 `for f in *.sql; do mysql < $f; done`：**
迁移里有 ALTER TABLE ... ADD COLUMN，那不是幂等的 —— 第二次跑直接
「Duplicate column name 'request_id'」。而这个脚本注定被反复执行（每次拉完
代码都该跑一遍），所以必须知道哪些已经做过。做法是最朴素的那种：一张
schema_migrations 表记文件名，跑之前先查。

**为什么用 Python 而不是 bash：**Windows 上没有 make、也不保证有 bash，
而这个项目的另一半本来就是 Python，`python` 一定在。编排放在 Python 里，
Windows / Linux / macOS 用同一份实现，不会出现两份逻辑各自漂移。

**三种连接方式，按可用性自动选：**

  1. docker exec 到 MySQL 容器
  2. 本机 mysql 客户端
  3. PyMySQL 直连（**不需要装任何外部命令**）

第 3 条是给「本机装了 MySQL 服务、但 mysql.exe 不在 PATH、也没有 Docker」
准备的 —— Windows 上这是常态。这条路要在 Python 里自己按分号拆语句，
本来是想避开的（注释和字符串里的分号容易切错），但这些 .sql 里没有存储过程、
触发器和 DELIMITER，形状足够简单，而且拆分器与 mysql 客户端两条路产出的
schema 做过逐表比对，完全一致。

**没有 Docker 时基础表也得建。**容器版靠 MySQL 镜像的 initdb 自动执行
deploy/sql/mysql/*.sql，本机 MySQL 没有这个机制。所以检测到库是空的就先把
那几个基础脚本跑掉，再走 migrations —— 否则迁移里的 ALTER TABLE 会找不到表。

另外两个坑一并处理掉了：

· **字符集**。连接不带 --default-character-set=utf8mb4 时，迁移里的中文会
  按 latin1 解释，类目名写进去就成了「Tæ¤」。手敲命令时最容易漏这个参数。

· **001 在全新安装上不该执行**。它给老库补 ods_process_log 与
  knowledge_item.knowledge_type，而 deploy/sql/mysql/*.sql 里已经含这两样，
  initdb 建完库它们就在了。检测到已存在就直接记为已应用，不去跑它。

用法：
    python deploy/scripts/migrate.py              # 应用所有待执行的迁移
    python deploy/scripts/migrate.py --status     # 只看状态，不改数据库
    python deploy/scripts/migrate.py --baseline   # 标记为已应用，但不执行

--baseline 是给「之前手工 mysql < xxx.sql 跑过」的库用的：那些库结构已经
对了，只是没有记录，直接跑会撞 Duplicate column。基线一次之后就能正常增量。

连接参数从环境变量或 deploy/.env 读：
    MYSQL_HOST（默认 127.0.0.1）  MYSQL_PORT（3306）
    MYSQL_USER（root）            MYSQL_PASSWORD
    MYSQL_DATABASE（smartmall）
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "deploy" / "sql" / "migrations"


def load_env() -> None:
    """读 deploy/.env（存在的话）。不覆盖已有的环境变量。"""
    env = ROOT / "deploy" / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

CONTAINER = os.environ.get("MYSQL_CONTAINER", "smdev-mysql")
DATABASE = os.environ.get("MYSQL_DATABASE", "smartmall")
HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
PORT = os.environ.get("MYSQL_PORT", "3306")
USER = os.environ.get("MYSQL_USER", "root")
PASSWORD = os.environ.get(
    "MYSQL_PASSWORD", os.environ.get("MYSQL_ROOT_PASSWORD", "root")
)

CHARSET = "--default-character-set=utf8mb4"


def _run(cmd: list[str], stdin=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdin=stdin, capture_output=True, text=True)


def _docker_available() -> bool:
    try:
        return _run(["docker", "exec", CONTAINER, "true"]).returncode == 0
    except FileNotFoundError:
        return False


def _mysql_available() -> bool:
    try:
        return _run(["mysql", "--version"]).returncode == 0
    except FileNotFoundError:
        return False


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


class CliBackend:
    """经 mysql 客户端执行（docker exec 或本机命令行）。"""

    def __init__(self, base: list[str], label: str) -> None:
        self.base, self.label = base, label

    @staticmethod
    def _clean(text: str) -> str:
        # mysql 客户端总会往 stderr 写密码警告，那不是错误
        return "\n".join(
            ln for ln in text.splitlines() if "Using a password" not in ln
        ).strip()

    def query(self, statement: str) -> str:
        p = _run(self.base + ["-N", "-B", "-e", statement, DATABASE])
        if p.returncode != 0:
            raise RuntimeError(self._clean(p.stderr) or self._clean(p.stdout))
        return p.stdout.strip()

    def run_file(self, path) -> None:
        with path.open("rb") as fh:
            p = _run(self.base + [DATABASE], stdin=fh)
        err = self._clean(p.stderr)
        if p.returncode != 0 or err.upper().startswith("ERROR"):
            raise RuntimeError(err or self._clean(p.stdout) or f"退出码 {p.returncode}")

    def ensure_database(self) -> None:
        p = _run(self.base + ["-e",
                 f"CREATE DATABASE IF NOT EXISTS `{DATABASE}` "
                 "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"])
        if p.returncode != 0:
            raise RuntimeError(self._clean(p.stderr))


class PyMySqlBackend:
    """PyMySQL 直连。不需要 docker，也不需要 mysql 客户端在 PATH 上。"""

    label = ""

    def __init__(self) -> None:
        import pymysql  # noqa: F401  仅在真的走这条路时才要求它装着

        self._pymysql = pymysql
        self.label = f"PyMySQL 直连 → {HOST}:{PORT}"

    def _connect(self, db: str | None):
        return self._pymysql.connect(
            host=HOST, port=int(PORT), user=USER, password=PASSWORD,
            database=db, charset="utf8mb4", autocommit=True,
        )

    def query(self, statement: str) -> str:
        with self._connect(DATABASE) as conn, conn.cursor() as cur:
            cur.execute(statement)
            rows = cur.fetchall() or ()
        return "\n".join("\t".join("" if c is None else str(c) for c in r)
                         for r in rows)

    def run_file(self, path) -> None:
        text = path.read_text(encoding="utf-8")
        with self._connect(DATABASE) as conn, conn.cursor() as cur:
            for stmt in split_statements(text):
                cur.execute(stmt)

    def ensure_database(self) -> None:
        with self._connect(None) as conn, conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{DATABASE}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )


def pick_backend():
    if _docker_available():
        return CliBackend(
            ["docker", "exec", "-i", CONTAINER, "mysql",
             f"-u{USER}", f"-p{PASSWORD}", CHARSET],
            f"容器 {CONTAINER}",
        )
    if _mysql_available():
        return CliBackend(
            ["mysql", f"-h{HOST}", f"-P{PORT}", f"-u{USER}",
             f"-p{PASSWORD}", CHARSET],
            f"本机客户端 → {HOST}:{PORT}",
        )
    try:
        return PyMySqlBackend()
    except ImportError:
        print("✗ 三条连接方式都不可用：")
        print("    · 没有可用的 MySQL 容器（%s）" % CONTAINER)
        print("    · PATH 上没有 mysql 客户端")
        print("    · 也没装 pymysql")
        print("  装一个就行：pip install pymysql")
        sys.exit(1)


BACKEND = pick_backend()
MODE = BACKEND.label


def sql(statement: str) -> str:
    return BACKEND.query(statement)


def sql_file(path) -> None:
    BACKEND.run_file(path)


def bootstrap_base_schema() -> None:
    """建库 + 跑基础建表脚本。

    **没有 Docker 时这一步不能少。**容器版靠 MySQL 镜像的 initdb 自动执行
    deploy/sql/mysql/*.sql；本机装的 MySQL 没有这个机制，库是空的，
    直接跑 migrations 会在第一条 ALTER TABLE 上找不到表。
    """
    BACKEND.ensure_database()
    have = sql(
        "SELECT COUNT(*) FROM information_schema.tables "
        f"WHERE table_schema='{DATABASE}' AND table_name='knowledge_item'"
    )
    if have and int(have) >= 1:
        return
    base = sorted((ROOT / "deploy" / "sql" / "mysql").glob("*.sql"),
                  key=lambda p: p.name)
    if not base:
        return
    print("  库是空的，先跑基础建表脚本（容器版由 initdb 自动完成）：")
    for path in base:
        sql_file(path)
        print(f"    ✓ {path.name}")


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg not in ("", "--status", "--baseline"):
        print(f"未知参数：{arg}（可用：--status / --baseline）")
        return 2
    status_only = arg == "--status"
    baseline = arg == "--baseline"

    print(f"==> 迁移（{MODE}，库 {DATABASE}）")

    try:
        if not status_only:
            bootstrap_base_schema()
        sql("SELECT 1")
    except Exception as exc:
        print(f"✗ 连不上数据库或建表失败（{MODE}，库 {DATABASE}）")
        print(f"  {exc}")
        print()
        print("  当前用的连接参数（改环境变量或写进 deploy/.env）：")
        print(f"    MYSQL_HOST={HOST}  MYSQL_PORT={PORT}")
        print(f"    MYSQL_USER={USER}  MYSQL_PASSWORD={'***' if PASSWORD else '(空)'}")
        print(f"    MYSQL_DATABASE={DATABASE}")
        print()
        print("  PowerShell 里临时设：$env:MYSQL_PASSWORD=\"你的密码\"")
        return 1

    sql(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  filename   VARCHAR(190) NOT NULL PRIMARY KEY,"
        "  applied_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='已应用的迁移'"
    )

    done = set(filter(None, sql("SELECT filename FROM schema_migrations").splitlines()))

    files = sorted(MIGRATIONS.glob("*.sql"), key=lambda p: p.name)

    # 001 只对「导过初版 30 张表的老库」有意义。全新安装里它要补的东西
    # initdb 已经建好了，跑它反而会因为重复定义报错，所以直接记为已应用。
    first = next((f for f in files if f.name.startswith("001_")), None)
    if first is not None and first.name not in done:
        have = sql(
            "SELECT COUNT(*) FROM information_schema.columns "
            f"WHERE table_schema='{DATABASE}' AND table_name='knowledge_item' "
            "AND column_name='knowledge_type'"
        )
        if have and int(have) >= 1:
            if not status_only:
                sql(
                    "INSERT IGNORE INTO schema_migrations(filename) "
                    f"VALUES('{first.name}')"
                )
            done.add(first.name)
            print(f"  ⤼ {first.name}（全新安装已内含，标记为已应用）")

    applied = skipped = 0
    for path in files:
        name = path.name
        if name in done:
            skipped += 1
            continue
        if status_only:
            print(f"  待应用  {name}")
            applied += 1
            continue
        if baseline:
            sql(f"INSERT IGNORE INTO schema_migrations(filename) VALUES('{name}')")
            print(f"  ⤼ {name}（基线：标记为已应用，未执行）")
            applied += 1
            continue

        try:
            sql_file(path)
        except Exception as exc:
            print(f"  ✗ {name}")
            for ln in str(exc).splitlines()[:4]:
                print(f"      {ln}")
            # 「列/表已存在」几乎总是同一个原因：这个库之前是手工跑迁移的，
            # 结构已经对了，只是没有记录。直接把话说到位，省得人去猜
            low = str(exc).lower()
            if any(k in low for k in ("duplicate column", "already exists",
                                      "duplicate key name")):
                print()
                print("  这个库看起来之前是手工执行迁移的（结构已存在，只是没有记录）。")
                print("  跑一次基线把现状登记下来，之后就能正常增量：")
                print("      python deploy/scripts/migrate.py --baseline")
            print()
            print("❌ 迁移中断。修好上面那个错误后重跑，已应用的不会重复执行。")
            return 1

        sql(f"INSERT INTO schema_migrations(filename) VALUES('{name}')")
        print(f"  ✓ {name}")
        applied += 1

    print()
    if status_only:
        print(f"共 {applied} 个待应用，{skipped} 个已应用。")
    elif baseline:
        print(f"✅ 已把 {applied} 个迁移标记为已应用（未执行），跳过 {skipped} 个原本就有记录的。")
    else:
        print(f"✅ 本次应用 {applied} 个，跳过 {skipped} 个已应用的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
