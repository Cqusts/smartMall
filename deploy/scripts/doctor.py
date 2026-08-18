#!/usr/bin/env python3
"""环境自检：一条命令查清「为什么起不来」。

这个脚本是被一连串真实故障逼出来的 —— 每一项检查都对应一次实际卡住的经历：

    Maven 版本         3.3.9 上编译报「不再支持源选项 5」，而 pom 里写着 release 21
    Python 依赖        少装 pipelines → 页面能开、商品是空的、日志里什么都没有
    数据库连通         没有 Docker 也没有 mysql 客户端时，连不上却看不出是哪一步
    应用账号           迁移走 root 成功、应用走 smartmall 失败，报错指向账号却像是数据没导
    环境变量陷阱       只设 MYSQL_PASSWORD 没设 MYSQL_USER → smartmall/<root密码> → Access denied
    迁移是否齐全       009 没跑 → 运营 Agent 的表缺着，用到才发现
    端口               8081/9002 被占，服务起不来或连到了别的进程

用法：
    python deploy/scripts/doctor.py
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

OK, WARN, BAD = "✓", "⚠", "✗"
_fail = 0
_warn = 0


def line(mark: str, title: str, detail: str = "") -> None:
    global _fail, _warn
    if mark == BAD:
        _fail += 1
    elif mark == WARN:
        _warn += 1
    print(f"  {mark} {title}" + (f"\n      {detail}" if detail else ""))


def load_env() -> None:
    env = ROOT / "deploy" / ".env"
    if not env.is_file():
        return
    for raw in env.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            k, v = raw.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def check_env_trap() -> None:
    """只设密码没设用户名 —— 这一个坑单独占一节，因为它的报错极具误导性。"""
    if os.environ.get("MYSQL_PASSWORD") and not os.environ.get("MYSQL_USER"):
        line(BAD, "环境变量：只设了 MYSQL_PASSWORD，没设 MYSQL_USER",
             "应用默认用 smartmall 账号，会拿 smartmall + 你设的密码去连 → Access denied。\n"
             "      清掉它（$env:MYSQL_PASSWORD=$null）用默认账号，"
             "或把 MYSQL_USER 一起设上。\n"
             "      给 db-init 的管理员密码请用 MYSQL_ADMIN_PASSWORD。")
    else:
        line(OK, "环境变量：MYSQL_USER / MYSQL_PASSWORD 没有半设状态")


def check_python_deps() -> None:
    missing = []
    for mod in ("pymysql", "sqlalchemy", "smartmall_pipeline", "fastapi", "uvicorn"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        line(BAD, f"Python 依赖缺失：{', '.join(missing)}",
             "pip install -e pipelines -e apps/python/ai-common "
             '-e "apps/python/ai-agent[server]"')
    else:
        line(OK, "Python 依赖齐全")


def check_maven() -> None:
    mvnw = ROOT / "apps" / "java" / ("mvnw.cmd" if os.name == "nt" else "mvnw")
    if mvnw.is_file():
        line(OK, "Maven Wrapper 就位（用 ./mvnw，不依赖本机 Maven 版本）")
    else:
        line(WARN, "缺 Maven Wrapper", "本机 mvn 需 ≥ 3.6.3")


def _conn(user: str, password: str, db: str | None):
    import pymysql

    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=user, password=password, database=db,
        charset="utf8mb4", connect_timeout=5,
    )


def check_database() -> None:
    db = os.environ.get("MYSQL_DATABASE", "smartmall")
    app_user = os.environ.get("SMARTMALL_APP_USER", "smartmall")
    app_pass = os.environ.get("SMARTMALL_APP_PASSWORD", "smartmall")
    # 应用真正会用的凭据：环境变量优先，否则就是内置默认
    eff_user = os.environ.get("MYSQL_USER", app_user)
    eff_pass = os.environ.get("MYSQL_PASSWORD", app_pass)

    try:
        import pymysql  # noqa: F401
    except ImportError:
        line(BAD, "没装 pymysql，无法检查数据库", "pip install pymysql")
        return

    try:
        conn = _conn(eff_user, eff_pass, db)
    except Exception as exc:
        line(BAD, f"应用账号连不上库（{eff_user}@{os.environ.get('MYSQL_HOST','127.0.0.1')}/{db}）",
             f"{exc}\n      跑 db-init 会建账号并校准密码："
             '$env:MYSQL_ADMIN_PASSWORD="root密码"; .\\smartmall.ps1 db-init')
        return

    with conn:
        line(OK, f"应用账号 {eff_user} 能连上 {db}")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=%s", (db,))
            tables = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name='schema_migrations'", (db,))
            has_rec = cur.fetchone()[0]

            applied = 0
            if has_rec:
                cur.execute("SELECT COUNT(*) FROM schema_migrations")
                applied = cur.fetchone()[0]

            total = len(list((ROOT / "deploy" / "sql" / "migrations").glob("*.sql")))
            if applied >= total:
                line(OK, f"迁移齐全（{applied}/{total}），共 {tables} 张表")
            else:
                line(BAD, f"迁移不齐（{applied}/{total}）", "跑 db-init 补上")

            for tbl, why in (("product", "商品页"), ("mall_order", "下单"),
                             ("sku", "库存")):
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name=%s", (db, tbl))
                if not cur.fetchone()[0]:
                    line(BAD, f"缺表 {tbl}（{why}用得到）", "跑 db-init")

            cur.execute("SELECT COUNT(*) FROM product WHERE deleted=0")
            n = cur.fetchone()[0]
            if n:
                line(OK, f"商品种子数据 {n} 条")
            else:
                line(BAD, "商品表是空的", "跑 db-init（会导入 005 的种子数据）")


def check_ports() -> None:
    for port, who in ((8081, "mall-product"), (9002, "店铺页")):
        s = socket.socket()
        s.settimeout(1)
        listening = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
        if listening:
            line(OK, f":{port} 已在监听（{who} 应该起着了）")
        else:
            line(WARN, f":{port} 没有服务（{who} 未启动）",
                 "run-product / serve 分别在两个终端里跑")


def main() -> int:
    load_env()
    print("==> smartMall 环境自检\n")
    print("  ---- 环境 ----")
    check_env_trap()
    check_python_deps()
    check_maven()
    print("\n  ---- 数据库 ----")
    check_database()
    print("\n  ---- 服务 ----")
    check_ports()

    print()
    if _fail:
        print(f"❌ {_fail} 项需要处理" + (f"，{_warn} 项提醒" if _warn else ""))
        return 1
    print("✅ 环境正常" + (f"（{_warn} 项提醒）" if _warn else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
