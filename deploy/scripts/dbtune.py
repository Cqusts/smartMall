"""MySQL 配置体检：把 SHOW VARIABLES / STATUS 翻译成"该不该调、怎么调"。

doctor.py 调用它。评估逻辑是纯函数（``assess``），不碰数据库，所以单测能把每条规则的
两边都验到；取数在 doctor 里。

**每一条建议都对应一次实测，不是照抄调优清单：**

* **buffer pool。**MySQL 默认 128MB。实测（MySQL 8、``O_DIRECT``，让 buffer pool 成为唯一的缓存；
  agent_trace 100 万行 ≈ 1GB）：128MB → 1GB 使 ``product_demand`` p50 103ms → 6.2ms，
  归档工具 2900 → 6600 行/秒，写入只涨约 10%。结论：默认值伤的是读，不是写——数据放不进
  buffer pool 之后，分析类查询的随机读都变成磁盘读。只在数据量逼近 buffer pool 时才提醒。
* **连接预算。**没有实测的"最优值"，只有算术：每个服务的池上限相加不能逼近 ``max_connections``，
  否则大促时第 301 条连接直接被拒。这里按"每个服务 1 个副本"估算，并明说这个假设。
* **慢查询日志。**本项目所有的 SQL 调优都是"找出慢查询、EXPLAIN、加索引或改写"
  （订单回收那条 310ms → 4ms 就是这么发现的）。不开日志就没有第一步。

**故意不提的：**``innodb_redo_log_capacity``。实测把它从 100MB 提到 1GB，归档速度在噪声内
（6500~7900 行/秒），``Innodb_log_waits`` 一直是 0——没有证据说明它是瓶颈，就不该建议改。
"""

from __future__ import annotations

from dataclasses import dataclass

MB = 1024 * 1024
GB = 1024 * MB
#: 默认的 buffer pool 是 128MB。数据+索引超过它的这个比例，就开始有读查询落到磁盘
POOL_PRESSURE = 0.8
#: 连接数估算/历史峰值超过上限的这个比例就提醒
CONN_PRESSURE = 0.7


@dataclass(frozen=True)
class Finding:
    level: str          # "ok" | "warn"
    title: str
    detail: str = ""


def _mb(n: float) -> str:
    return f"{n / MB:,.0f}MB" if n < GB else f"{n / GB:.1f}GB"


def recommended_pool_bytes(dataset_bytes: float) -> int:
    """建议值：数据+索引的 1.5 倍，至少 1GB，取整到 128MB（InnoDB 的 chunk 默认大小，
    不是它的整数倍会被向上取整，设了 1.2GB 实际得到 1.25GB，容易困惑）。"""
    want = max(1 * GB, dataset_bytes * 1.5)
    chunk = 128 * MB
    return int(-(-want // chunk) * chunk)


def assess(*, variables: dict[str, str], status: dict[str, str], dataset_bytes: float,
           java_pool_total: int, python_pool_per_process: int,
           python_processes: int = 2) -> list[Finding]:
    out: list[Finding] = []

    # ---------------------------------------------------------------- buffer pool
    pool = int(variables.get("innodb_buffer_pool_size", 0))
    if pool and dataset_bytes > pool * POOL_PRESSURE:
        rec = recommended_pool_bytes(dataset_bytes)
        out.append(Finding(
            "warn",
            f"InnoDB buffer pool 只有 {_mb(pool)}，而数据+索引已有 {_mb(dataset_bytes)}",
            "数据放不进 buffer pool 后，分析类读查询（如 product_demand）的随机读会落到磁盘。\n"
            "实测（O_DIRECT，agent_trace 100 万行 ≈ 1GB）：128MB → 1GB，p50 103ms → 6.2ms。\n"
            f"在线调大（8.0 起可在线改，重启保留）：SET PERSIST innodb_buffer_pool_size = {rec};\n"
            "本机还跑着 Java/Python 服务，别超过物理内存的一半。"))
    elif pool:
        out.append(Finding(
            "ok", f"buffer pool {_mb(pool)}，数据+索引 {_mb(dataset_bytes)}，放得下",
            "数据涨到 buffer pool 的 80% 以上时这里会提醒。"))

    # ---------------------------------------------------------------- 连接预算
    max_conn = int(variables.get("max_connections", 0))
    if max_conn:
        worst = java_pool_total + python_pool_per_process * python_processes
        used = int(status.get("Max_used_connections", 0))
        budget = (f"Java 各服务池上限合计 {java_pool_total} + Python "
                  f"{python_processes} 个进程 × {python_pool_per_process}（POOL_SIZE+MAX_OVERFLOW）"
                  f"= {worst}；按每个服务 1 个副本估算，扩副本要乘上去")
        if worst > max_conn * CONN_PRESSURE:
            out.append(Finding(
                "warn", f"连接预算偏紧：最坏需要 {worst} 条，max_connections 只有 {max_conn}",
                budget + "\n调大 max_connections，或调小 MYSQL_POOL_SIZE / MYSQL_MAX_OVERFLOW。"))
        elif used > max_conn * CONN_PRESSURE:
            out.append(Finding(
                "warn", f"连接数历史峰值 {used} 已超过 max_connections {max_conn} 的 {CONN_PRESSURE:.0%}",
                "有服务在用比预算多的连接——先查是不是又有哪里每个请求新建了连接池"
                "（SHOW PROCESSLIST 按 host 分组看）。"))
        else:
            out.append(Finding(
                "ok", f"连接预算：最坏 {worst} 条 / 上限 {max_conn}，历史峰值 {used}", budget))

    # ---------------------------------------------------------------- 慢查询日志
    slow = str(variables.get("slow_query_log", "OFF")).upper() in ("ON", "1")
    threshold = float(variables.get("long_query_time", 10) or 10)
    if not slow:
        out.append(Finding(
            "warn", "慢查询日志没开",
            "本项目所有的 SQL 调优都是「找出慢查询 → EXPLAIN → 加索引或改写」"
            "（订单回收那条 310ms → 4ms 就是这么发现的）。不开就没有第一步。\n"
            "SET PERSIST slow_query_log = ON; SET PERSIST long_query_time = 0.5;"))
    elif threshold > 2:
        out.append(Finding(
            "warn", f"慢查询日志开着，但阈值 {threshold:g} 秒太高",
            "本项目的慢查询在百毫秒级，10 秒的默认阈值什么都抓不到。\n"
            "SET PERSIST long_query_time = 0.5;"))
    else:
        out.append(Finding("ok", f"慢查询日志已开（阈值 {threshold:g}s）"))

    return out
