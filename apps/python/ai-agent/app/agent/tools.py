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

    def list_on_sale_product_ids(self, limit: int = 60) -> list[int]: ...

    def search_products(
        self, *, category: str | None = None, price_min: float | None = None,
        price_max: float | None = None, colors: Sequence[str] = (),
        sizes: Sequence[str] = (), in_stock_only: bool = True, limit: int = 20,
    ) -> list[dict[str, Any]]: ...

    def get_product_detail(self, product_id: int) -> dict[str, Any] | None: ...

    def get_sku_stock_price(
        self, product_id: int, spec: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_size_chart(self, product_id: int) -> dict[str, Any] | None: ...

    def list_catalog(self, limit: int = 600) -> list[dict[str, Any]]: ...

    def get_order_status(
        self, order_no: str, user_id: int | None
    ) -> dict[str, Any] | None: ...

    def recommend_products(
        self, product_id: int, kind: str = "similar"
    ) -> list[dict[str, Any]]: ...

    def answer_assets(
        self, *, product_id: int | None = None,
        asset_ids: Sequence[int] = (), limit: int = 4,
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

    degraded: list[str] = field(default_factory=list)
    """这次查询里**降级掉的部分**。

    有些数据缺了不该让整页挂掉（比如素材表还没建），但降级必须留下痕迹：
    "查失败了"和"本来就没有"长得一样的话，一条漏跑的迁移可以躲很久。"""

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

    def list_on_sale_product_ids(self, limit: int = 60) -> list[int]:
        """在售商品 ID。给店铺列表用。

        写死 ID 列表的话，上新要改代码，而且迟早出现"库里有、页面没有"。
        """
        return [
            int(r["id"]) for r in self._rows(
                "SELECT id FROM product WHERE deleted = 0 AND status = 'on_sale' "
                "ORDER BY id LIMIT :lim",
                {"lim": limit},
            )
        ]

    def search_products(
        self, *, category=None, price_min=None, price_max=None,
        colors=(), sizes=(), in_stock_only=True, limit=20,
    ) -> list[dict[str, Any]]:
        """按条件筛商品。导购 Agent 的主力工具。

        **筛选条件落在 SKU 上而不是商品上。** 颜色尺码是 SKU 的属性，
        一件商品可能只有 M 码缺货、L 码有货——按商品粒度过滤会把
        "有你要的码"和"这件商品存在"混为一谈，用户点进去才发现选不了。

        返回的每条带上**命中的 SKU**，让下游能说清"藏青 M 码有货"
        而不是含糊的"这件有货"。
        """
        where = ["p.deleted = 0", "p.status = 'on_sale'", "s.deleted = 0"]
        params: dict[str, Any] = {"lim": int(limit)}

        if category:
            where.append("(c.name LIKE :cat OR p.short_name LIKE :cat"
                         " OR p.name LIKE :cat)")
            params["cat"] = f"%{category}%"
        if price_min is not None:
            where.append("s.price >= :pmin")
            params["pmin"] = float(price_min)
        if price_max is not None:
            where.append("s.price <= :pmax")
            params["pmax"] = float(price_max)
        if in_stock_only:
            where.append("s.stock > 0 AND s.status = 'on_sale'")

        # 颜色尺码存在 spec 这个 JSON 里。用 LIKE 而不是 JSON_EXTRACT：
        # 后者在不同 MySQL 版本上行为有差异，而这里只需要包含匹配
        for i, c in enumerate(colors):
            where.append(f"s.spec LIKE :c{i}")
            params[f"c{i}"] = f'%{c}%'
        for i, z in enumerate(sizes):
            where.append(f"s.spec LIKE :z{i}")
            params[f"z{i}"] = f'%{z}%'

        rows = self._rows(
            "SELECT p.id, p.name, p.short_name, p.main_image, c.name AS category,"
            " s.sku_no, s.spec, s.price, s.origin_price, s.stock "
            "FROM product p JOIN sku s ON s.product_id = p.id "
            "LEFT JOIN category c ON c.id = p.category_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY s.price, p.id LIMIT :lim",
            params,
        )

        # 同一商品的多个 SKU 合并成一条，SKU 挂在下面
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            item = out.setdefault(int(r["id"]), {
                "id": int(r["id"]), "name": r["name"],
                "short_name": r["short_name"], "category": r["category"],
                "main_image": r.get("main_image") or "", "skus": [],
                "price_from": float(r["price"]),
            })
            item["skus"].append({
                "sku_no": r["sku_no"], "spec": r["spec"],
                "price": float(r["price"]), "stock": int(r["stock"]),
            })
            item["price_from"] = min(item["price_from"], float(r["price"]))
        return list(out.values())

    _BASE_COLS = "p.id, p.name, p.short_name, p.brand, p.status, c.name AS category"

    def get_product_detail(self, product_id: int) -> dict[str, Any] | None:
        def _q(cols: str) -> list[dict[str, Any]]:
            return self._rows(
                f"SELECT {cols} FROM product p "
                "LEFT JOIN category c ON c.id = p.category_id "
                "WHERE p.id = :pid AND p.deleted = 0",
                {"pid": product_id},
            )

        try:
            rows = _q(f"{self._BASE_COLS}, p.main_image")
        except Exception:  # noqa: BLE001
            # 正常走不到：main_image 在 01_product.sql 里就有。留着是因为
            # 这一处的失败模式是**整页白屏**——建库方式稍有出入，用户
            # git pull 完看到的就是空店铺，再去翻是哪张表出了问题。
            rows = _q(self._BASE_COLS)
            for r in rows:
                r["main_image"] = ""
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

    def list_catalog(self, limit: int = 600) -> list[dict[str, Any]]:
        """店铺列表要的全部数据，**一共七次查询**（其中五次是 IN 批量）。

        原先店铺页是这么拼的：先拿 ID 列表，再对每个商品分别查详情、
        查属性、查 SKU、查尺码表 —— 每个商品四次往返。12 个商品时
        看不出来（49 次查询，几十毫秒）；上到 579 个商品实测
        **2317 次查询 / 597ms**，而那还是 SQLite 这种进程内的库。
        MySQL 走 TCP，每次往返按 0.2ms 算就是四百多毫秒起步。

        这里换成 IN 批量查询然后在内存里归拢。**不是提前优化**：
        它是加完 500 个商品之后量出来的，量之前谁也不知道差多少。
        """
        import json

        ids = self.list_on_sale_product_ids(limit)
        if not ids:
            return []
        # IN 列表用绑定参数一个个占位，不拼字符串——ID 是内部来的，
        # 但拼 SQL 这个习惯本身不该有
        marks = ",".join(f":i{n}" for n in range(len(ids)))
        params = {f"i{n}": v for n, v in enumerate(ids)}

        rows = self._rows(
            f"SELECT {self._BASE_COLS}, p.main_image, p.category_id FROM product p "
            "LEFT JOIN category c ON c.id = p.category_id "
            f"WHERE p.id IN ({marks}) AND p.deleted = 0", params)
        items = {int(r["id"]): dict(r, attrs={}, skus=[], size_chart=None, assets=[])
                 for r in rows}

        for r in self._rows(
                "SELECT product_id, attr_key, attr_value FROM product_attr "
                f"WHERE product_id IN ({marks})", params):
            item = items.get(int(r["product_id"]))
            if item is not None:
                item["attrs"][r["attr_key"]] = r["attr_value"]

        for r in self._rows(
                "SELECT product_id, sku_no, spec, price, origin_price, stock,"
                f" status FROM sku WHERE product_id IN ({marks})"
                " AND deleted = 0 ORDER BY sku_no", params):
            item = items.get(int(r["product_id"]))
            if item is None:
                continue
            item["skus"].append({
                "sku_no": r["sku_no"], "spec": r["spec"],
                "price": float(r["price"]),
                "origin_price": float(r["origin_price"] or 0) or None,
                "stock": int(r["stock"]),
                "in_stock": r["stock"] > 0 and r["status"] == "on_sale",
            })

        for r in self._rows(
                "SELECT product_id, chart, note FROM size_chart "
                f"WHERE product_id IN ({marks})", params):
            item = items.get(int(r["product_id"]))
            if item is None:
                continue
            chart = r["chart"]
            item["size_chart"] = {
                "chart": json.loads(chart) if isinstance(chart, str) else chart,
                "note": r["note"] or "",
            }

        # 一级类目名。**页面侧边栏要的是「食品」，不是「饼干糕点」**——
        # 570 个商品铺开有 65 个三级类目，全列出来没人翻得动，而按
        # 一级分只有二十来个，正好是一屏。
        #
        # 在 Python 里拼而不是在 SQL 里递归 JOIN：类目表统共一百来行，
        # 一次全捞进来做字典查，比一条自连接的 SQL 好读得多
        cat_rows = self._rows(
            "SELECT id, name, path FROM category WHERE deleted = 0", {})
        by_id = {int(r["id"]): r["name"] for r in cat_rows}
        # path 形如 /4/60/4060，第一段就是一级类目
        root_of = {}
        for r in cat_rows:
            head = str(r["path"] or "").strip("/").split("/")[0]
            root_of[int(r["id"])] = by_id.get(int(head)) if head.isdigit() else None

        detail_rows = {int(r["id"]): r for r in rows}
        for pid, item in items.items():
            cid = detail_rows.get(pid, {}).get("category_id")
            item["root_category"] = (
                root_of.get(int(cid)) if cid else None) or item.get("category") or ""

        # AI 素材。**只取 review_status='approved'**——这是那道审核闸门
        # 唯一真正起作用的地方。写成 != 'rejected' 之类的补集就废了：
        # 新加一个审核状态会默认落进"可展示"，而没人会注意到。
        #
        # 表可能还没建（011/014 是后加的迁移）。这时候让整个店铺页 500
        # 是过度反应——商品本身是好的，缺的只是素材。但**降级不能装成
        # 正常**：捞失败与"这个商品没有素材"长得一模一样的话，
        # 迁移漏跑了没有任何人会发现。所以记一笔到 degraded，
        # /api/products 会把它带出去。
        try:
            for r in self._rows(
                    "SELECT product_id, kind, usage_tag, local_path, model"
                    f" FROM marketing_asset WHERE product_id IN ({marks})"
                    " AND review_status = 'approved' AND local_path <> ''"
                    " ORDER BY id", params):
                item = items.get(int(r["product_id"]))
                if item is None:
                    continue
                item["assets"].append({
                    "kind": r["kind"], "usage": r["usage_tag"] or "",
                    "path": r["local_path"],
                    # 《人工智能生成合成内容标识办法》：生成内容必须可识别。
                    # 标识跟着数据走，不靠展示层记得加
                    "ai_generated": True, "model": r["model"] or "",
                })
        except ToolError as exc:
            self.degraded.append(f"素材表读不到，商品页不显示 AI 素材：{exc}")

        # 顺序按 ID，与 list_on_sale_product_ids 一致
        return [items[i] for i in ids if i in items]

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


    # ------------------------------------------------------------ 素材

    #: ``asset`` 表里算「可以给买家看」的状态。与 ``marketing_asset`` 的
    #: ``review_status='approved'`` 是同一道闸门，只是那张表的状态机更细
    #: （draft|reviewing|approved|online|offline|rejected|archived）。
    #:
    #: **白名单而不是补集。** 写成 ``not in ('rejected','draft')`` 的话，
    #: 将来加一个状态就会默认落进"可展示"——而漏出去的是没审过的内容。
    _SHOWABLE_ASSET_STATUS = ("approved", "online")

    def answer_assets(
        self, *, product_id: int | None = None,
        asset_ids: Sequence[int] = (), limit: int = 4,
    ) -> list[dict[str, Any]]:
        """答案能挂的素材。两条来源，都过审核闸门。

        1. ``asset_ids`` —— 命中的那条知识**显式关联**的素材，来自
           ``knowledge_item.asset_ids``，落在 ``asset`` 表。它们与这条
           知识是绑定的，所以相关性由数据保证。
        2. ``product_id`` —— 这个商品审核通过的运营素材，落在
           ``marketing_asset``。相关性由调用方的判据保证
           （见 ``nodes.mount_assets``：只有商品知识类问题才挂）。

        **两条都只取审核通过的。** 这里要是漏了，商家后台点"通过"这
        整件事就被绕过去了——买家在商品页看不到的图，换个入口从客服
        对话里看到了。
        """
        out: list[dict[str, Any]] = []

        ids = [int(i) for i in asset_ids][:limit]
        if ids:
            marks = ",".join(f":a{n}" for n in range(len(ids)))
            params: dict[str, Any] = {f"a{n}": v for n, v in enumerate(ids)}
            sts = ",".join(f":s{n}" for n in range(len(self._SHOWABLE_ASSET_STATUS)))
            params.update({f"s{n}": v
                           for n, v in enumerate(self._SHOWABLE_ASSET_STATUS)})
            try:
                for r in self._rows(
                        f"SELECT id, modality, scene, oss_key, cdn_url,"
                        f" ai_generated, gen_model FROM asset"
                        f" WHERE id IN ({marks}) AND deleted = 0"
                        f" AND status IN ({sts})", params):
                    url = (r.get("cdn_url") or r.get("oss_key") or "").strip()
                    if not url:
                        continue
                    out.append({
                        "asset_id": int(r["id"]),
                        "kind": "video" if r["modality"] == "video" else "image",
                        "url": url, "usage": r.get("scene") or "",
                        "ai_generated": bool(r.get("ai_generated")),
                        "model": r.get("gen_model") or "", "source": "knowledge",
                    })
            except ToolError as exc:
                self.degraded.append(f"asset 表读不到，知识关联素材挂不上：{exc}")

        if product_id and len(out) < limit:
            try:
                for r in self._rows(
                        "SELECT id, kind, usage_tag, local_path, model"
                        " FROM marketing_asset WHERE product_id = :pid"
                        " AND review_status = 'approved' AND local_path <> ''"
                        " ORDER BY id DESC LIMIT :n",
                        {"pid": int(product_id), "n": limit - len(out)}):
                    out.append({
                        "asset_id": int(r["id"]),
                        "kind": "video" if r["kind"] == "video" else "image",
                        "url": r["local_path"], "usage": r.get("usage_tag") or "",
                        # marketing_asset 里的都是 AI 生成的，表里写死 1
                        "ai_generated": True,
                        "model": r.get("model") or "", "source": "product",
                    })
            except ToolError as exc:
                self.degraded.append(f"素材表读不到，答案挂不上图：{exc}")

        return out[:limit]


@dataclass
class StubToolBox:
    """测试替身。"""

    products: dict[int, dict] = field(default_factory=dict)
    skus: dict[int, list[dict]] = field(default_factory=dict)
    charts: dict[int, dict] = field(default_factory=dict)
    orders: dict[str, dict] = field(default_factory=dict)
    catalog: list[dict] = field(default_factory=list)
    """search_products 的候选池。测试直接给结果，不模拟 SQL 筛选——
    要验的是 Agent 拿到 0 条 / 很多条时怎么决策，不是 WHERE 拼得对不对。"""
    assets: dict[int, list[dict]] = field(default_factory=dict)
    """product_id → 已审核通过的素材。**替身里只放通过的**，
    "没通过的会不会漏出去"要拿真 SQL 验（见 test_tools 的 TestAnswerAssets），
    在替身上验等于验自己写的 if。"""
    knowledge_assets: dict[int, dict] = field(default_factory=dict)
    """asset_id → 知识显式关联的素材。"""
    fail: bool = False
    calls: list[str] = field(default_factory=list)
    permission_denials: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    last_search: dict = field(default_factory=dict)

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.fail:
            raise ToolError("注入的工具故障")

    def list_on_sale_product_ids(self, limit=60):
        self._check("list_on_sale_product_ids")
        return sorted(self.products)[:limit]

    def search_products(self, *, category=None, price_min=None, price_max=None,
                        colors=(), sizes=(), in_stock_only=True, limit=20):
        self._check("search_products")
        self.last_search = {
            "category": category, "price_min": price_min, "price_max": price_max,
            "colors": list(colors), "sizes": list(sizes),
        }
        return self.catalog[:limit]

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

    def list_catalog(self, limit=600):
        self._check("list_catalog")
        out = []
        for pid in sorted(self.products)[:limit]:
            d = dict(self.products[pid])
            d.setdefault("attrs", {})
            d["skus"] = self.skus.get(pid, [])
            d["size_chart"] = self.charts.get(pid)
            out.append(d)
        return out

    def recommend_products(self, product_id, kind="similar"):
        self._check("recommend_products")
        return []

    def answer_assets(self, *, product_id=None, asset_ids=(), limit=4):
        self._check("answer_assets")
        out = [dict(self.knowledge_assets[i], source="knowledge")
               for i in asset_ids if i in self.knowledge_assets]
        if product_id and len(out) < limit:
            out += [dict(a, source="product")
                    for a in self.assets.get(product_id, [])][:limit - len(out)]
        return out[:limit]


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
