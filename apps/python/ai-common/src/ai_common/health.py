"""健康检查。

刻意区分两个端点，与 Java 侧 ``HealthController`` 的语义保持一致：

* ``/health``  —— 存活探针。只回答"进程活着吗"，不探测任何下游，永远快速返回。
  用于容器 healthcheck，避免 MySQL/Milvus 抖动时容器被反复重启。
* ``/ready``   —— 就绪探针。执行全部已注册的依赖检查，任一 required 依赖失败即 503。
  用于流量接入判断与监控告警。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

DependencyCheck = Callable[[], Awaitable[None]]
"""依赖检查协程。检查通过则正常返回，失败则抛异常（异常信息会进响应）。"""


@dataclass
class _Registered:
    name: str
    check: DependencyCheck
    required: bool
    timeout: float


@dataclass
class HealthRegistry:
    """依赖检查注册表。服务在启动时注册自己依赖的中间件。"""

    _checks: list[_Registered] = field(default_factory=list)
    _started_at: float = field(default_factory=time.monotonic)

    def register(
        self,
        name: str,
        check: DependencyCheck,
        *,
        required: bool = True,
        timeout: float = 3.0,
    ) -> None:
        """注册一个依赖检查。

        Args:
            name: 依赖名，出现在 ``/ready`` 响应里。
            check: 检查协程。
            required: 为 False 时该依赖失败不影响整体就绪状态，只在响应中标注为 degraded。
                用于可降级依赖——例如 Langfuse 挂了不该阻断客服服务。
            timeout: 单个检查的超时秒数。检查本身卡住不能拖垮就绪探针。
        """
        self._checks.append(_Registered(name, check, required, timeout))

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_at

    async def run(self) -> tuple[bool, dict[str, dict[str, object]]]:
        """执行全部检查，返回 ``(整体是否就绪, 各依赖明细)``。"""
        if not self._checks:
            return True, {}

        async def _one(reg: _Registered) -> tuple[str, dict[str, object]]:
            started = time.monotonic()
            try:
                await asyncio.wait_for(reg.check(), timeout=reg.timeout)
                status = "UP"
                detail: dict[str, object] = {}
            except asyncio.TimeoutError:
                status = "DOWN"
                detail = {"error": f"timeout after {reg.timeout}s"}
            except Exception as exc:  # noqa: BLE001 — 检查失败原因需要原样透出
                status = "DOWN"
                detail = {"error": f"{type(exc).__name__}: {exc}"}
            return reg.name, {
                "status": status,
                "required": reg.required,
                "latencyMs": round((time.monotonic() - started) * 1000, 1),
                **detail,
            }

        results = await asyncio.gather(*(_one(r) for r in self._checks))
        details = dict(results)
        ready = all(
            d["status"] == "UP"
            for d in details.values()
            if d["required"]
        )
        return ready, details
