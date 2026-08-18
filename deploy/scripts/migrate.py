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
真正执行 .sql 文件仍然交给 mysql 客户端 —— 在 Python 里自己拆语句
（按分号切）在遇到注释、字符串里的分号时很容易切错，不值得冒这个险。

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


if _docker_available():
    MODE = f"容器 {CONTAINER}"
    BASE = ["docker", "exec", "-i", CONTAINER, "mysql",
            f"-u{USER}", f"-p{PASSWORD}", CHARSET]
elif _mysql_available():
    MODE = f"本机客户端 → {HOST}:{PORT}"
    BASE = ["mysql", f"-h{HOST}", f"-P{PORT}", f"-u{USER}", f"-p{PASSWORD}", CHARSET]
else:
    print("✗ 既没有可用的 MySQL 容器（%s），本机也没有 mysql 客户端" % CONTAINER)
    print("  起容器：docker compose -f deploy/docker-compose.dev.yml up -d mysql")
    sys.exit(1)


def _clean(text: str) -> str:
    """mysql 客户端总会往 stderr 写密码警告，那不是错误。"""
    return "\n".join(
        ln for ln in text.splitlines() if "Using a password" not in ln
    ).strip()


def sql(statement: str) -> str:
    """执行一条语句，返回裸结果（-N -B：无表头、制表符分隔）。"""
    p = _run(BASE + ["-N", "-B", "-e", statement, DATABASE])
    if p.returncode != 0:
        raise RuntimeError(_clean(p.stderr) or _clean(p.stdout))
    return p.stdout.strip()


def sql_file(path: Path) -> None:
    """执行一个 .sql 文件。失败时抛出，错误信息里已去掉密码警告。"""
    with path.open("rb") as fh:
        p = _run(BASE + [DATABASE], stdin=fh)
    err = _clean(p.stderr)
    if p.returncode != 0 or err.upper().startswith("ERROR"):
        raise RuntimeError(err or _clean(p.stdout) or f"退出码 {p.returncode}")


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg not in ("", "--status", "--baseline"):
        print(f"未知参数：{arg}（可用：--status / --baseline）")
        return 2
    status_only = arg == "--status"
    baseline = arg == "--baseline"

    try:
        sql("SELECT 1")
    except Exception as exc:
        print(f"✗ 连不上数据库（{MODE}，库 {DATABASE}）")
        print(f"  {exc}")
        return 1

    print(f"==> 迁移（{MODE}，库 {DATABASE}）")

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
