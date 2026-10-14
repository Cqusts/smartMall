"""测试用的真 redis-server。机器上没有时相关测试跳过（而不是悄悄通过或用假实现冒充）。"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RedisServer:
    def __init__(self, port: int | None = None) -> None:
        self.port = port or free_port()
        self.proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["redis-server", "--port", str(self.port), "--bind", "127.0.0.1",
             "--save", "", "--appendonly", "no", "--loglevel", "warning"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("redis-server 没起来")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(10)
            self.proc = None


needs_redis = pytest.mark.skipif(shutil.which("redis-server") is None,
                                 reason="没有 redis-server，跳过真 Redis 测试")
redis_asyncio = pytest.importorskip("redis.asyncio")
