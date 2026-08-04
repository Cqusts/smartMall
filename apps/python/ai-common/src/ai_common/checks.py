"""通用依赖检查。

M0 阶段各服务尚未接入具体客户端（Milvus / MySQL / Kafka SDK），
但依赖是否可达这件事现在就能验证——用 TCP 连通性与 HTTP 探测即可。
后续里程碑接入真实客户端后，把对应的检查替换为深度检查（如 Milvus 的 collection 是否存在）。
"""

from __future__ import annotations

import asyncio

import httpx

from .health import DependencyCheck


def tcp_check(host: str, port: int) -> DependencyCheck:
    """TCP 连通性检查。适用于 MySQL / Redis / Kafka / Milvus 等。"""

    async def _check() -> None:
        reader, writer = await asyncio.open_connection(host, port)
        writer.close()
        await writer.wait_closed()
        del reader

    return _check


def http_check(url: str, *, expect_status: int = 200) -> DependencyCheck:
    """HTTP 探测。适用于下游 HTTP 服务（Java 业务服务、ComfyUI、Langfuse）。"""

    async def _check() -> None:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
        if resp.status_code != expect_status:
            raise RuntimeError(f"expected {expect_status}, got {resp.status_code}")

    return _check


def kafka_check(bootstrap_servers: str) -> DependencyCheck:
    """Kafka 检查：对 bootstrap 列表中第一个 broker 做 TCP 探测。"""
    first = bootstrap_servers.split(",")[0].strip()
    host, _, port = first.rpartition(":")
    return tcp_check(host or "localhost", int(port or 9092))
