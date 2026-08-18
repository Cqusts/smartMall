"""本地开发的公共配置解析。migrate / doctor / run-java 三个脚本共用。

抽出来是因为 `load_env()` 和「哪个环境变量是给谁用的」这套规则，之前在
migrate.py 与 doctor.py 里各写了一份。两份实现漂移过一次，代价是一整轮
排查：migrate 认的是 MYSQL_ADMIN_*，doctor 认的还是 MYSQL_*，于是同一台
机器上「迁移说成功、自检说连不上」。这类规则只该有一处定义。

**两套凭据，语义不能混：**

    MYSQL_ADMIN_USER / MYSQL_ADMIN_PASSWORD    建库建表用，通常是 root
    MYSQL_USER       / MYSQL_PASSWORD          应用连库用，默认 smartmall

早先两者共用 MYSQL_USER/MYSQL_PASSWORD，真实后果是这样的：为了跑迁移在终端里
设了 $env:MYSQL_PASSWORD="root的密码"，然后在同一个终端起服务 —— 应用的
MYSQL_USER 没设、取默认值 smartmall，密码却拿到了 root 的，于是
smartmall/<root密码> → Access denied。报错指向应用账号，人却以为是账号没建好。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Java 服务 → 端口。**顺序即启动顺序**：网关最后起，前面的都在了再开门。
#:
#: run-java.py 与 doctor.py 都要这份清单。放在这里而不是各存一份，是因为
#: 加一个模块时漏改另一处，症状是「起是起了，自检说没起」这种自相矛盾的输出。
SERVICES: dict[str, int] = {
    "mall-product": 8081,
    "mall-asset": 8082,
    "mall-dataplat": 8083,
    "mall-kpi": 8084,
    "mall-gateway": 8080,
}

#: 店铺页下单只要这一个，其余三个业务模块目前还是骨架。
ESSENTIAL = "mall-product"

#: 店铺页（Python ai-agent）的端口。
WEB_PORT = 9002


def load_env() -> None:
    """读 deploy/.env（存在的话）。已经设过的环境变量优先，不覆盖。"""
    env = ROOT / "deploy" / ".env"
    if not env.is_file():
        return
    for raw in env.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def host() -> str:
    return os.environ.get("MYSQL_HOST", "127.0.0.1")


def port() -> int:
    return int(os.environ.get("MYSQL_PORT", "3306"))


def database() -> str:
    return os.environ.get("MYSQL_DATABASE", "smartmall")


def admin_credentials() -> tuple[str, str]:
    """建库建表用的账号。回落到旧变量名是为了兼容已有的 deploy/.env。"""
    user = (os.environ.get("MYSQL_ADMIN_USER")
            or os.environ.get("MYSQL_ROOT_USER")
            or "root")
    password = (os.environ.get("MYSQL_ADMIN_PASSWORD")
                or os.environ.get("MYSQL_ROOT_PASSWORD")
                or "")
    return user, password


def app_credentials() -> tuple[str, str]:
    """应用（mall-* 与 ai-agent）连库用的账号。

    默认值必须和 application.yml、repository.py 里写死的默认值一致，
    否则自检查的是一套、应用连的是另一套。
    """
    user = os.environ.get("MYSQL_USER") or os.environ.get("SMARTMALL_APP_USER") or "smartmall"
    password = (os.environ.get("MYSQL_PASSWORD")
                or os.environ.get("SMARTMALL_APP_PASSWORD")
                or "smartmall")
    return user, password


def db_env_warning() -> str | None:
    """半设状态的告警文案；没问题时返回 None。

    只设 MYSQL_PASSWORD 不设 MYSQL_USER 是最容易踩的一种：用户名取默认值
    smartmall，密码却是 root 的，两者拼出一个根本不存在的组合。
    """
    if os.environ.get("MYSQL_PASSWORD") and not os.environ.get("MYSQL_USER"):
        return (
            "这个终端只设了 MYSQL_PASSWORD，没设 MYSQL_USER。\n"
            "  应用的用户名会取默认值 smartmall，配上你设的密码 —— 多半 Access denied。\n"
            "  二选一：清掉它（$env:MYSQL_PASSWORD=$null），或把 MYSQL_USER 一起设上。\n"
            "  给 db-init 的管理员密码请用 MYSQL_ADMIN_PASSWORD。"
        )
    return None


def connect(user: str, password: str, db: str | None = None, timeout: int = 5):
    """连本机 MySQL。整个项目只有这一条连库路径。"""
    import pymysql

    return pymysql.connect(
        host=host(), port=port(), user=user, password=password,
        database=db, charset="utf8mb4", connect_timeout=timeout,
        autocommit=True,
    )
