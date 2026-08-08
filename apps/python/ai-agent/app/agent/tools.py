"""MCP 工具层：客服 Agent 能查的业务数据。

**为什么这一层必须存在。** 库存、价格、物流每分钟都在变，知识库里
那句"目前有货"是三个月前某段对话里说的。拿它回答"还有货吗"，
用户下单才发现缺货——这比说"我帮您转人工"糟糕得多。所以这类问题
一律走工具查结构化数据，绝不走 RAG。

尺码表是同一个道理的另一面：它是一张二维表。「160cm 90斤穿什么码」
需要的是查表加换算，不是找一段相似的对话——向量检索对这类查询
天然很差。

**两条硬约束：**

1. **全部只读。** AI 误触发的退款、改价是不可逆的资金损失。读操作
   出错最多说错话，写操作出错是真金白银。唯一的写是建转人工工单。
2. **越权校验在这一层做，不靠提示词。** 提示词能被绕过，SQL 的
   WHERE 条件不能。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable


class ToolError(RuntimeError):
    """工具调用失败。

    与"查不到"是两回事：查不到是确定结论（这个商品确实没有尺码表），
    失败是不知道。对用户说"没有尺码表"而真相是数据库连不上，
    是同一类撒谎——处置应当是转人工。
    """


#: 订单号。用户会连在句子里说："我的订单2026080100001到哪了"。
#:
#: 用 ``(?<!\d)...(?!\d)`` 而不是 ``\b``：中文字符在 Python 的 Unicode
#: 模式下也算 ``\w``，所以"订单2026..."的"单"与"2"之间**没有**词边界，
#: 用 ``\b`` 会一个都提不出来。而中文用户恰恰习惯把单号紧贴着字写。
#:
#: 下限取 10 位：短于这个的数字大概率是尺码、价格、身高或日期。
ORDER_NO_RE = re.compile(r"(?<!\d)(\d{10,24})(?!\d)")


def extract_order_no(text: str) -> str | None:
    return m.group(1) if (m := ORDER_NO_RE.search(text or "")) else None


@runtime_checkable
class ToolBox(Protocol):
    """工具集协议。生产实现读 MySQL，测试用 StubToolBox。"""

    def get_product_detail(self, product_id: int) -> dict[str, Any] | None: ...

    def get_sku_stock_price(
        self, product_id: int, spec: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_size_chart(self, product_id: int) -> dict[str, Any] | None: ...

    def get_order_status(
        self, order_no: str, user_id: int | None
    ) -> dict[str, Any] | None: ...

    def recommend_products(
        self, product_id: int, kind: str = "similar"
    ) -> list[dict[str, Any]]: ...


@dataclass
class MySqlToolBox:
    """直连 MySQL 的只读工具集。

    与知识库同一个库。生产环境这层应该走 mall-product 等业务服务
    （权限、缓存、限流都在那边），但直读让整条链路在只有 MySQL 的
    环境里也能跑通——和检索层的取舍一致。
    """

    engine: Any
    permission_denials: list[str] = field(default_factory=list)
    """越权尝试。不返回给用户，但要留痕——
    有人拿别人的订单号来试，这本身就是需要告警的信号。"""

    @classmethod
    def from_env(cls) -> "MySqlToolBox":
        from smartmall_pipeline.repository import DwsRepository

        return cls(engine=DwsRepository.from_env().engine)

    def _rows(self, sql: str, params: dict) -> list[dict]:
        from sqlalchemy import text

        try:
            with self.engine.connect() as conn:
                return [dict(r) for r in conn.execute(text(sql), params).mappings()]
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"查询失败：{type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------ 商品

    def get_product_detail(self, product_id: int) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT p.id, p.name, p.short_name, p.brand, p.status, c.name AS category "
            "FROM product p LEFT JOIN category c ON c.id = p.category_id "
            "WHERE p.id = :pid AND p.deleted = 0",
            {"pid": product_id},
        )
        if not rows:
            return None
        detail = rows[0]
        detail["attrs"] = {
            r["attr_key"]: r["attr_value"]
            for r in self._rows(
                "SELECT attr_key, attr_value FROM product_attr WHERE product_id = :pid",
                {"pid": product_id},
            )
        }
        return detail

    def get_sku_stock_price(
        self, product_id: int, spec: str | None = None
    ) -> list[dict[str, Any]]:
        """各 SKU 的实时库存与价格。

        **缺货的也返回**，并标出来。只返回有货的会让客服说"有货"，
        而用户想要的那个码恰恰没有——他下单时才发现，体验更差。
        """
        rows = self._rows(
            "SELECT sku_no, spec, price, origin_price, stock, status FROM sku "
            "WHERE product_id = :pid AND deleted = 0 ORDER BY sku_no",
            {"pid": product_id},
        )
        for r in rows:
            r["price"] = float(r["price"])
            r["origin_price"] = float(r["origin_price"] or 0) or None
            r["in_stock"] = r["stock"] > 0 and r["status"] == "on_sale"
        if spec:
            rows = [r for r in rows if spec in str(r["spec"])] or rows
        return rows

    def get_size_chart(self, product_id: int) -> dict[str, Any] | None:
        import json

        rows = self._rows(
            "SELECT chart, note FROM size_chart WHERE product_id = :pid",
            {"pid": product_id},
        )
        if not rows:
            return None
        chart = rows[0]["chart"]
        return {
            "chart": json.loads(chart) if isinstance(chart, str) else chart,
            "note": rows[0]["note"] or "",
        }

    # ------------------------------------------------------------ 订单

    def get_order_status(
        self, order_no: str, user_id: int | None
    ) -> dict[str, Any] | None:
        """查订单。**必须同时匹配订单号与当前会话的用户。**

        不校验的话，用户说出别人的订单号就能查到他人的收货信息与
        物流轨迹——这是 AI 客服真实存在的攻击面，而且很容易被忽略，
        因为功能本身"看起来是好的"。

        越权时返回 ``None``（与"订单不存在"完全相同的响应），而不是
        报"无权访问"。后者会泄露订单是否存在——攻击者可以靠枚举
        订单号来确认哪些是真的。但尝试本身要记进
        :attr:`permission_denials`：拿别人的单号来试，是需要告警的信号。
        """
        if not order_no:
            return None
        if user_id is None:
            # 匿名会话不允许查订单。没有身份就没有"属于你"这回事
            self.permission_denials.append(f"匿名查询订单 {order_no}")
            return None

        rows = self._rows(
            "SELECT order_no, user_id, product_id, spec, quantity, amount, status,"
            " express_company, express_no, tracks, created_at, shipped_at "
            "FROM mall_order WHERE order_no = :no",
            {"no": order_no},
        )
        if not rows:
            return None

        order = rows[0]
        if int(order["user_id"]) != int(user_id):
            self.permission_denials.append(
                f"用户 {user_id} 试图查询属于 {order['user_id']} 的订单 {order_no}"
            )
            return None

        import json

        tracks = order.get("tracks")
        order["tracks"] = json.loads(tracks) if isinstance(tracks, str) else (tracks or [])
        order["amount"] = float(order["amount"])
        order.pop("user_id", None)  # 不必回给上层，避免顺手带进提示词
        return order

    # ------------------------------------------------------------ 推荐

    def recommend_products(
        self, product_id: int, kind: str = "similar"
    ) -> list[dict[str, Any]]:
        """相似款（同类目）或搭配款（不同类目）。"""
        same = "=" if kind == "similar" else "<>"
        return self._rows(
            f"SELECT p.id, p.name, p.short_name FROM product p "
            f"WHERE p.deleted = 0 AND p.status = 'on_sale' AND p.id <> :pid "
            f"  AND p.category_id {same} ("
            f"    SELECT category_id FROM product WHERE id = :pid) "
            f"LIMIT 3",
            {"pid": product_id},
        )


@dataclass
class StubToolBox:
    """测试替身。"""

    products: dict[int, dict] = field(default_factory=dict)
    skus: dict[int, list[dict]] = field(default_factory=dict)
    charts: dict[int, dict] = field(default_factory=dict)
    orders: dict[str, dict] = field(default_factory=dict)
    fail: bool = False
    calls: list[str] = field(default_factory=list)
    permission_denials: list[str] = field(default_factory=list)

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.fail:
            raise ToolError("注入的工具故障")

    def get_product_detail(self, product_id):
        self._check("get_product_detail")
        return self.products.get(product_id)

    def get_sku_stock_price(self, product_id, spec=None):
        self._check("get_sku_stock_price")
        return self.skus.get(product_id, [])

    def get_size_chart(self, product_id):
        self._check("get_size_chart")
        return self.charts.get(product_id)

    def get_order_status(self, order_no, user_id):
        self._check("get_order_status")
        order = self.orders.get(order_no)
        if order is None:
            return None
        if user_id is None or int(order.get("user_id", -1)) != int(user_id):
            self.permission_denials.append(f"{user_id} → {order_no}")
            return None
        return {k: v for k, v in order.items() if k != "user_id"}

    def recommend_products(self, product_id, kind="similar"):
        self._check("recommend_products")
        return []


# ---------------------------------------------------------------- 渲染


def render_skus(skus: Sequence[dict]) -> str:
    """把 SKU 列表渲染进提示词。

    有货无货都列出来并标明——模型据此才能说"米白 M 有货，L 暂时缺货"，
    而不是笼统一句"有货"。
    """
    if not skus:
        return "（暂无在售规格）"
    lines = []
    for s in skus:
        spec = s.get("spec")
        stock = "有货" if s.get("in_stock") else "缺货"
        price = s.get("price")
        origin = s.get("origin_price")
        tail = f"（原价 {origin:.0f}）" if origin and origin > (price or 0) else ""
        lines.append(f"- {spec}：{price:.0f} 元{tail}，{stock}（库存 {s.get('stock')}）")
    return "\n".join(lines)


def render_order(order: dict) -> str:
    status_cn = {
        "pending_payment": "待付款", "paid": "已付款待发货", "shipped": "已发货",
        "delivered": "已送达", "completed": "已完成",
        "cancelled": "已取消", "refunding": "退款中",
    }
    lines = [
        f"订单号：{order.get('order_no')}",
        f"状态：{status_cn.get(order.get('status'), order.get('status'))}",
        f"商品：{order.get('spec')} × {order.get('quantity')}，"
        f"实付 {order.get('amount')} 元",
    ]
    if order.get("express_no"):
        lines.append(f"快递：{order.get('express_company')} {order.get('express_no')}")
    for t in (order.get("tracks") or [])[-3:]:
        lines.append(f"  {t.get('ts')} {t.get('desc')}")
    return "\n".join(lines)


def render_size_chart(data: dict) -> str:
    chart = data.get("chart") or {}
    header = chart.get("表头") or []
    rows = chart.get("行") or []
    out = [" | ".join(str(h) for h in header)]
    out += [" | ".join(str(c) for c in row) for row in rows]
    if data.get("note"):
        out.append(f"备注：{data['note']}")
    return "\n".join(out)
