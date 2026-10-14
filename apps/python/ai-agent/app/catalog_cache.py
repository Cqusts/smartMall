"""店铺首页商品列表的缓存。

**要解决的问题（压测量出来的，不是猜的）。** ``/api/products`` 一次返回全部 582 个
商品（490KB），每个请求都要：新建连接池 → 4 条 SQL → Python 里组装 582 个对象 →
序列化。单请求约 190ms，**吞吐固定 5.2 req/s，与并发无关**（1、10、30 并发都是
5.1–5.2）——并发 30 时 p99 为 14 秒。而这是店铺首页，每次页面访问都会打到它。

商品数据变化得慢（改价、上下架、库存），读得极快（每个访客每次进页面一次），
所以缓存是对的。但缓存会让页面和客服"看到的数据不一致"，这条要讲清楚。

------------------------------------------------------------ 一致性取舍

本项目有一条明确的原则：**页面显示什么，客服就得答什么**——两者读同一张表，
否则页面说"有货"而客服说"缺货"，用户会以为系统在骗人（见 ``ws.products``）。
客服工具层读的是实时数据，没有缓存。所以这里的缓存会引入一个窗口：
页面上的库存最多比客服落后 ``ttl`` 秒。

收窄这个窗口的办法有两个：

1. **经本服务转发的订单动作成功后立刻作废缓存**（:meth:`CatalogCache.invalidate`）。
   用户自己刚买完，页面马上就能看到库存变了——这是最容易被注意到的一种不一致。
2. **TTL 很短**（默认 5 秒）。别人下单、超时回收释放库存这类不经过本服务的变化，
   最多 5 秒后可见。

库存的**权威判定**永远在下单那一刻的条件 UPDATE（``stock >= ?``），页面上的数字只是
展示——显示过期最坏是"点了购买被告知库存不足"，不会超卖。``CATALOG_CACHE_TTL=0`` 关掉缓存。

------------------------------------------------------------ 并发语义

* **单飞（single-flight）**：缓存过期时只有一个线程去重建，其余的等它建完拿同一份结果。
  不做的话，过期的那一刻 30 个并发请求会同时去重建 30 次——缓存形同虚设，
  恰恰在流量大的时候。
* **作废与重建的竞态**：重建开始前先记下版本号，建完后带着*开始时*的版本号入库。
  重建途中发生了作废，这份结果在下次读取时就会被判为过期——不会把"作废之前的数据"
  当成"作废之后的数据"留在缓存里。
* **失败也缓存，但只缓存很短时间**（默认 2 秒）。库连不上时，每个连接超时要等好几秒；
  不缓存的话 30 个排队的请求会**串行**各等一遍超时。缓存太久又会拖慢恢复。
* **缓存键含连库配置**：``MYSQL_HOST`` 之类变了，旧缓存就不作数——否则换了库
  还在返回上一个库的商品。

------------------------------------------------------------ 传输

序列化后的 JSON 与 gzip 后的字节都在重建时算好，请求只是把字节吐出去。
490KB 的 JSON 压缩后约 40KB。ETag 取内容哈希：数据没变时，TTL 到期重建出的
ETag 和上次一样，浏览器带 ``If-None-Match`` 来就直接 304，连响应体都不用发。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CatalogSnapshot:
    key: str
    ok: bool
    body: bytes
    gz: bytes
    etag: str
    built_at: float
    version: int


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class CatalogCache:
    def __init__(
        self,
        build: Callable[[], dict[str, Any]],
        *,
        ttl: float | None = None,
        fail_ttl: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._build = build
        self.ttl = _env_float("CATALOG_CACHE_TTL", 5.0) if ttl is None else ttl
        self.fail_ttl = (_env_float("CATALOG_CACHE_FAIL_TTL", 2.0)
                         if fail_ttl is None else fail_ttl)
        self._clock = clock
        self._snap: CatalogSnapshot | None = None
        self._version = 0
        self._vlock = threading.Lock()
        self._lock = threading.Lock()
        self.builds = 0
        """重建次数。测试靠它证明"30 个并发只重建一次"。"""

    # ------------------------------------------------------------ 读

    def get(self, key: str) -> CatalogSnapshot:
        if self.ttl <= 0:                       # 缓存关闭：每次都建，不排队
            return self._rebuild(key)

        snap = self._snap
        if snap is not None and self._fresh(snap, key):
            return snap

        with self._lock:                         # 单飞：同一时刻只有一个线程重建
            snap = self._snap
            if snap is not None and self._fresh(snap, key):
                return snap                      # 排队期间别的线程已经建好了
            snap = self._rebuild(key)
            self._snap = snap
            return snap

    def _fresh(self, snap: CatalogSnapshot, key: str) -> bool:
        if snap.key != key or snap.version != self._version:
            return False
        age = self._clock() - snap.built_at
        return age < (self.ttl if snap.ok else self.fail_ttl)

    # ------------------------------------------------------------ 作废

    def invalidate(self) -> None:
        """数据变了（比如刚成功下了一单）。下一次读取一定重建。"""
        with self._vlock:
            self._version += 1

    # ------------------------------------------------------------ 建

    def _rebuild(self, key: str) -> CatalogSnapshot:
        # 版本号必须在**开始建之前**取：建的过程中发生作废，这份结果带着旧版本号入库，
        # 下次读取就会被判为过期。反过来在建完之后才取的话，作废之前读到的数据
        # 会被当成作废之后的新数据留下来。
        version = self._version
        payload = self._build()
        self.builds += 1

        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":"), default=str).encode("utf-8")
        ok = bool(payload.get("ok"))
        return CatalogSnapshot(
            key=key,
            ok=ok,
            body=body,
            # compresslevel=5：默认的 9 对 JSON 只多省几个百分点，却慢好几倍
            gz=gzip.compress(body, compresslevel=5, mtime=0),
            etag='W/"' + hashlib.sha1(body).hexdigest()[:16] + '"',
            built_at=self._clock(),
            version=version,
        )


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """``If-None-Match`` 可以是逗号分隔的多个值，也可以带 ``W/`` 前缀或写成 ``*``。"""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    bare = etag.removeprefix("W/")
    return any(tok.strip().removeprefix("W/") == bare
               for tok in if_none_match.split(","))
