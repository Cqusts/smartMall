#!/usr/bin/env python3
"""环境自检：一条命令查清「为什么起不来」。

    python deploy/scripts/doctor.py

每一项检查都对应一次真实卡住过的经历，不是凑数的：

    JDK 版本         21 以下报 UnsupportedClassVersionError，讲的是 class file
                     version 65.0 —— 得知道 65 对应 21 才看得懂
    Maven Wrapper    本机 Maven 3.3.9 编译报「不再支持源选项 5」，而 pom 写的是 21
    Python 依赖      少装 pipelines → 页面能开、商品是空的、日志里什么都没有
    环境变量陷阱     只设 MYSQL_PASSWORD 没设 MYSQL_USER → smartmall/<root密码>
                     → Access denied，而报错指向应用账号，像是账号没建好
    应用账号         迁移走 root 成功、应用走 smartmall 失败
    迁移是否齐全     少跑一个 → 用到那张表才发现
    jar 是否构建     没 build 直接 up，报的是「找不到 jar」而不是「你还没构建」
    端口占用         8080-8084 / 9002 被占，服务起不来或连到了别的进程
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smartmall_env as env  # noqa: E402

ROOT = env.ROOT
OK, WARN, BAD = "✓", "⚠", "✗"
_fail = 0
_warn = 0

SERVICES = env.SERVICES
WEB_PORT = env.WEB_PORT


def line(mark: str, title: str, detail: str = "") -> None:
    global _fail, _warn
    if mark == BAD:
        _fail += 1
    elif mark == WARN:
        _warn += 1
    print(f"  {mark} {title}" + (f"\n      {detail}" if detail else ""))


# ---------------------------------------------------------------- 环境

def check_env_trap() -> None:
    warning = env.db_env_warning()
    if warning:
        head, *rest = warning.splitlines()
        # 续行统一按 line() 的缩进重排。原文里为了 PowerShell 那边好看带了
        # 两个空格的前导，直接拼进来会比第一行多缩两格。
        line(BAD, "环境变量：" + head,
             "\n      ".join(ln.strip() for ln in rest))
    else:
        line(OK, "环境变量：MYSQL_USER / MYSQL_PASSWORD 没有半设状态")


def check_java() -> None:
    exe = None
    if os.environ.get("JAVA_HOME"):
        cand = Path(os.environ["JAVA_HOME"]) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if cand.is_file():
            exe = str(cand)
    exe = exe or shutil.which("java")
    if not exe:
        line(BAD, "找不到 java", "装 JDK 21：https://adoptium.net/temurin/releases/?version=21")
        return
    out = subprocess.run([exe, "-version"], capture_output=True, text=True).stderr
    m = re.search(r'version "?(\d+)', out)
    if not m:
        line(WARN, f"认不出 java 版本（{exe}）", out.splitlines()[0] if out else "")
    elif int(m.group(1)) < 21:
        line(BAD, f"JDK 版本是 {m.group(1)}，本项目要 21+",
             f"当前 {exe}；装 21 后把 JAVA_HOME 指过去")
    else:
        line(OK, f"JDK {m.group(1)}（{exe}）")


def check_maven() -> None:
    mvnw = ROOT / "apps" / "java" / ("mvnw.cmd" if os.name == "nt" else "mvnw")
    if mvnw.is_file():
        line(OK, "Maven Wrapper 就位（用 ./mvnw，不依赖本机 Maven 版本）")
    else:
        line(WARN, "缺 Maven Wrapper", "本机 mvn 需 ≥ 3.6.3")


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


# ---------------------------------------------------------------- 数据库

def check_database() -> None:
    db = env.database()
    user, password = env.app_credentials()

    try:
        import pymysql  # noqa: F401
    except ImportError:
        line(BAD, "没装 pymysql，查不了数据库", "pip install pymysql")
        return

    try:
        conn = env.connect(user, password, db)
    except Exception as exc:
        line(BAD, f"应用账号连不上库（{user}@{env.host()}:{env.port()}/{db}）",
             f"{exc}\n      db-init 会建账号并校准密码：\n"
             '      $env:MYSQL_ADMIN_PASSWORD="root密码"; .\\smartmall.ps1 db-init')
        return

    with conn:
        line(OK, f"应用账号 {user} 能连上 {db}（应用用的就是它）")
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema=%s", (db,))
            tables = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema=%s AND table_name='schema_migrations'", (db,))
            applied = 0
            if cur.fetchone()[0]:
                cur.execute("SELECT COUNT(*) FROM schema_migrations")
                applied = cur.fetchone()[0]

            total = len(list((ROOT / "deploy" / "sql" / "migrations").glob("*.sql")))
            if applied >= total:
                line(OK, f"迁移齐全（{applied}/{total}），共 {tables} 张表")
            else:
                line(BAD, f"迁移不齐（{applied}/{total}）", "跑 db-init 补上")

            missing = []
            for tbl, why in (("product", "商品页"), ("sku", "库存"),
                             ("mall_order", "下单")):
                cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                            "WHERE table_schema=%s AND table_name=%s", (db, tbl))
                if not cur.fetchone()[0]:
                    missing.append(f"{tbl}（{why}）")
            # 履约那几个字段是 008 用 ALTER TABLE 加的。只数迁移条数看不出它们在不在
            # ——手工跑过一半的库照样能记满 9 条，而缺一个字段就是运行期 Unknown column。
            # tracks 是物流轨迹，存成 mall_order 上的 JSON 列，不是单独一张表。
            for col, why in (("tracks", "物流轨迹"), ("shipped_at", "发货"),
                             ("status_before_refund", "退款")):
                cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                            "WHERE table_schema=%s AND table_name='mall_order' "
                            "AND column_name=%s", (db, col))
                if not cur.fetchone()[0]:
                    missing.append(f"mall_order.{col}（{why}）")
            if missing:
                line(BAD, "缺表/缺列：" + "、".join(missing), "跑 db-init")

            if not missing:
                cur.execute("SELECT COUNT(*) FROM product WHERE deleted=0")
                n = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM sku WHERE deleted=0 AND stock > 0")
                s = cur.fetchone()[0]
                if n and s:
                    line(OK, f"种子数据就位：商品 {n} 个，有货 SKU {s} 个")
                elif n:
                    line(WARN, f"商品 {n} 个，但没有一个 SKU 还有库存", "下单会提示库存不足")
                else:
                    line(BAD, "商品表是空的", "跑 db-init（会导入 005 的种子数据）")


# ---------------------------------------------------------------- 服务

def check_jars() -> None:
    missing = [svc for svc in SERVICES
               if not list((ROOT / "apps" / "java" / svc / "target").glob("*.jar"))]
    if missing:
        line(WARN, f"还没构建：{', '.join(missing)}", ".\\smartmall.ps1 build")
    else:
        line(OK, f"{len(SERVICES)} 个服务的 jar 都在")


def _listening(port: int) -> bool:
    s = socket.socket()
    s.settimeout(1)
    busy = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    return busy


def check_ports() -> None:
    for svc, port in SERVICES.items():
        if _listening(port):
            line(OK, f":{port} {svc} 在跑")
        elif svc == "mall-product":
            line(WARN, f":{port} {svc} 没起（店铺页下单要它）", ".\\smartmall.ps1 up")
        else:
            line(WARN, f":{port} {svc} 没起")
    if _listening(WEB_PORT):
        line(OK, f":{WEB_PORT} 店铺页在跑  http://127.0.0.1:{WEB_PORT}/")
    else:
        line(WARN, f":{WEB_PORT} 店铺页没起", ".\\smartmall.ps1 serve（另开一个终端）")


def main() -> int:
    env.load_env()
    print("==> smartMall 环境自检（本机 MySQL，不用 Docker）\n")
    print("  ---- 工具链 ----")
    check_env_trap()
    check_java()
    check_maven()
    check_python_deps()
    print("\n  ---- 数据库 ----")
    check_database()
    print("\n  ---- 服务 ----")
    check_jars()
    check_ports()

    print()
    if _fail:
        print(f"❌ {_fail} 项需要处理" + (f"，{_warn} 项提醒" if _warn else ""))
        return 1
    print("✅ 环境正常" + (f"（{_warn} 项提醒）" if _warn else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
