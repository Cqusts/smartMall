"""MCP 工具层。

存在的理由：库存、价格、物流每分钟都在变，知识库里那句"目前有货"
是三个月前某段对话里说的。拿它回答"还有货吗"，用户下单才发现缺货——
比说"我帮您转人工"糟糕得多。

这一层的**安全约束比功能更要紧**：查订单必须同时匹配订单号与当前会话
用户，否则用户说出别人的单号就能查到他人的收货信息与物流轨迹。
这是 AI 客服真实存在的攻击面，而且极易被忽略——因为功能本身
"看起来是好的"。
"""

from __future__ import annotations

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

from app.agent import graph  # noqa: E402
from app.agent.llm import FakeLlmClient  # noqa: E402
from app.agent.nodes import Deps  # noqa: E402
from app.agent.retriever import StubRetriever  # noqa: E402
from app.agent.state import AgentState, Citation, HandoverReason  # noqa: E402
from app.agent.tools import (  # noqa: E402
    MySqlToolBox, StubToolBox, ToolError, extract_order_no,
    render_order, render_size_chart, render_skus,
)

_DDL = [
    """CREATE TABLE product (id INTEGER PRIMARY KEY, product_no TEXT, name TEXT,
        short_name TEXT, category_id INTEGER, brand TEXT, status TEXT,
        main_image TEXT DEFAULT '', deleted INTEGER DEFAULT 0)""",
    # 照 01_product.sql 的列写。**别自己编一个最小表**：少一列 path，
    # list_catalog 就查不出一级类目，而那种失败在页面上表现为侧边栏空白
    """CREATE TABLE category (id INTEGER PRIMARY KEY, parent_id INTEGER
        NOT NULL DEFAULT 0, name TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 1,
        path TEXT NOT NULL DEFAULT '', sort_order INTEGER DEFAULT 0,
        deleted INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE product_attr (product_id INTEGER, attr_key TEXT,
        attr_value TEXT, is_core INTEGER DEFAULT 0)""",
    """CREATE TABLE sku (id INTEGER PRIMARY KEY AUTOINCREMENT, sku_no TEXT,
        product_id INTEGER, spec TEXT, price REAL, origin_price REAL,
        stock INTEGER, status TEXT, deleted INTEGER DEFAULT 0)""",
    """CREATE TABLE size_chart (product_id INTEGER PRIMARY KEY, chart TEXT,
        note TEXT)""",
    """CREATE TABLE mall_order (id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT UNIQUE, user_id INTEGER, product_id INTEGER, sku_no TEXT,
        spec TEXT, quantity INTEGER, amount REAL, status TEXT,
        express_company TEXT, express_no TEXT, tracks TEXT,
        created_at TEXT, shipped_at TEXT)""",
    """CREATE TABLE marketing_asset (id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER, kind TEXT, usage_tag TEXT, local_path TEXT,
        model TEXT, review_status TEXT DEFAULT 'pending')""",
]

_SEED = [
    "INSERT INTO category VALUES (1, 0, '服装', 1, '/1', 0, 0)",
    "INSERT INTO category VALUES (24, 1, '上装', 2, '/1/24', 0, 0)",
    "INSERT INTO category VALUES (1024, 24, '针织衫', 3, '/1/24/1024', 0, 0)",
    "INSERT INTO category VALUES (1030, 24, '夹克', 3, '/1/24/1030', 0, 0)",
    "INSERT INTO product VALUES (9001,'P9001','米白针织衫','针织衫',1024,"
    "'smartMall','on_sale','9001.jpg',0)",
    "INSERT INTO product_attr VALUES (9001,'材质','100%羊毛',1)",
    "INSERT INTO sku (sku_no,product_id,spec,price,origin_price,stock,status) "
    "VALUES ('S-M',9001,'{\"尺码\":\"M\"}',299.0,399.0,38,'on_sale')",
    "INSERT INTO sku (sku_no,product_id,spec,price,origin_price,stock,status) "
    "VALUES ('S-L',9001,'{\"尺码\":\"L\"}',299.0,399.0,0,'on_sale')",
    # 导购要按颜色+尺码筛，所以这几条 spec 里两样都有
    "INSERT INTO product VALUES (9002,'P9002','复古工装夹克','夹克',1030,"
    "'smartMall','on_sale','9002.jpg',0)",
    "INSERT INTO sku (sku_no,product_id,spec,price,origin_price,stock,status) "
    "VALUES ('J-A',9002,'{\"颜色\":\"藏青\",\"尺码\":\"M\"}',459.0,599.0,12,"
    "'on_sale')",
    "INSERT INTO sku (sku_no,product_id,spec,price,origin_price,stock,status) "
    "VALUES ('J-B',9002,'{\"颜色\":\"藏青\",\"尺码\":\"L\"}',479.0,599.0,0,"
    "'on_sale')",
    "INSERT INTO sku (sku_no,product_id,spec,price,origin_price,stock,status) "
    "VALUES ('J-C',9002,'{\"颜色\":\"焦糖\",\"尺码\":\"M\"}',459.0,599.0,7,"
    "'on_sale')",
    "INSERT INTO product VALUES (9003,'P9003','已下架夹克','夹克',1030,"
    "'smartMall','off_sale','9003.jpg',0)",
    "INSERT INTO sku (sku_no,product_id,spec,price,origin_price,stock,status) "
    "VALUES ('X-A',9003,'{\"颜色\":\"藏青\",\"尺码\":\"M\"}',199.0,299.0,50,"
    "'on_sale')",
    "INSERT INTO size_chart VALUES (9001,"
    "'{\"表头\":[\"尺码\",\"胸围\"],\"行\":[[\"M\",100],[\"L\",104]]}',"
    "'宽松版型')",
    "INSERT INTO mall_order (order_no,user_id,product_id,spec,quantity,amount,"
    "status,express_company,express_no,tracks) VALUES "
    "('2026080100001',10086,9001,'米白 M',1,299.0,'shipped','中通','78123',"
    "'[{\"ts\":\"08-02 09:15\",\"desc\":\"到达杭州转运中心\"}]')",
    "INSERT INTO mall_order (order_no,user_id,product_id,spec,quantity,amount,"
    "status,express_company,express_no,tracks) VALUES "
    "('2026080400004',10087,9001,'白 L',1,899.0,'shipped','京东','JD99',NULL)",
    # 素材三条：通过的、待审的、通过但没文件的。三条都要有，
    # 只放一条通过的话「只显示 approved」这条断言等于没验
    "INSERT INTO marketing_asset (product_id,kind,usage_tag,local_path,model,"
    "review_status) VALUES (9001,'image','white','generated/9001-w.png',"
    "'qwen-image','approved')",
    "INSERT INTO marketing_asset (product_id,kind,usage_tag,local_path,model,"
    "review_status) VALUES (9001,'image','scene','generated/9001-s.png',"
    "'qwen-image','pending')",
    "INSERT INTO marketing_asset (product_id,kind,usage_tag,local_path,model,"
    "review_status) VALUES (9001,'video','','','wan2.7','approved')",
]


@pytest.fixture
def box():
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for stmt in _DDL + _SEED:
            conn.execute(text(stmt))
    return MySqlToolBox(engine=engine)


# ---------------------------------------------------------------- 越权


class TestOrderPermission:
    """**这一组是整个工具层最重要的部分。**

    不校验的话，用户说出别人的订单号就能查到他人的收货信息与物流轨迹。
    校验必须在这一层做——提示词能被绕过，SQL 的 WHERE 条件不能。
    """

    def test_owner_can_query(self, box):
        order = box.get_order_status("2026080100001", user_id=10086)
        assert order and order["status"] == "shipped"
        assert order["tracks"], "物流轨迹要能拿到"

    def test_other_user_cannot(self, box):
        """别人的订单查不到——这是核心断言。"""
        assert box.get_order_status("2026080400004", user_id=10086) is None

    def test_denial_is_indistinguishable_from_not_found(self, box):
        """越权与"订单不存在"必须返回**完全相同**的结果。

        报"无权访问"会泄露订单是否存在——攻击者可以靠枚举订单号
        确认哪些是真的。安全上这叫存在性泄露。
        """
        denied = box.get_order_status("2026080400004", user_id=10086)
        missing = box.get_order_status("9999999999999", user_id=10086)
        assert denied == missing is None

    def test_attempt_is_recorded_even_though_response_is_silent(self, box):
        """对用户静默，但对运维不能静默——
        有人拿别人的单号来试，本身就是需要告警的信号。"""
        box.get_order_status("2026080400004", user_id=10086)
        assert box.permission_denials
        assert "10087" in box.permission_denials[0]

    def test_anonymous_session_cannot_query_orders(self, box):
        """没有身份就没有"属于你"这回事。"""
        assert box.get_order_status("2026080100001", user_id=None) is None
        assert box.permission_denials

    def test_empty_order_no(self, box):
        assert box.get_order_status("", user_id=10086) is None


# ---------------------------------------------------------------- 商品数据


class TestSearchProducts:
    """导购 Agent 的主力工具。**这段 SQL 真跑起来才算数。**

    这个项目里 SQL 已经栽过一次（``LAST_INSERT_ID`` 之类的 MySQL 方言
    在 SQLite 上直接炸），而工具层出错的表现是"搜不到"——
    和"确实没货"长得一模一样，从对话上根本分辨不出来。
    """

    def test_finds_by_category(self, box):
        found = box.search_products(category="夹克")
        assert [i["id"] for i in found] == [9002]

    def test_skus_are_merged_under_one_product(self, box):
        """同一件商品的多个 SKU 要合成一条。

        不合并的话，一件夹克按颜色尺码铺开成六条，用户看到的是
        "为您找到 6 款"，点进去全是同一件。
        """
        item = box.search_products(category="夹克")[0]
        assert len(item["skus"]) == 2  # 藏青 L 库存 0，被 in_stock_only 滤掉
        assert item["price_from"] == 459.0

    def test_filters_at_sku_level_not_product_level(self, box):
        """颜色尺码是 SKU 的属性。

        按商品粒度过滤会把"这件有藏青"和"藏青这个码有货"混为一谈——
        用户点进去才发现选不了，正是工具层存在的意义被抵消的地方。
        """
        found = box.search_products(colors=["藏青"], sizes=["L"])
        assert found == [], "藏青 L 库存为 0，不该出现在结果里"
        assert [i["id"] for i in box.search_products(colors=["藏青"],
                                                    sizes=["M"])] == [9002]

    def test_price_range_is_applied(self, box):
        assert box.search_products(price_max=300)[0]["id"] == 9001
        assert box.search_products(price_min=400)[0]["id"] == 9002
        assert box.search_products(price_min=1000) == []

    def test_off_sale_products_are_excluded(self, box):
        """下架商品不能出现在推荐里，哪怕它库存充足、正好符合条件。"""
        ids = [i["id"] for i in box.search_products(colors=["藏青"])]
        assert 9003 not in ids

    def test_out_of_stock_only_skus_drop_the_product(self, box):
        assert box.search_products(colors=["藏青"], sizes=["L"]) == []

    def test_in_stock_only_can_be_turned_off(self, box):
        found = box.search_products(colors=["藏青"], sizes=["L"],
                                    in_stock_only=False)
        assert [i["id"] for i in found] == [9002]

    def test_no_conditions_returns_everything_on_sale(self, box):
        """导购第一轮往往什么条件都没有——这时候不能报错也不能返回空。"""
        assert {i["id"] for i in box.search_products()} == {9001, 9002}

    def test_failure_raises_instead_of_returning_empty(self, box):
        """**查不到 ≠ 没有货。** 返回空列表等于对上层撒谎。"""
        from sqlalchemy import create_engine

        broken = MySqlToolBox(engine=create_engine("sqlite://"))
        with pytest.raises(ToolError):
            broken.search_products(category="夹克")


class TestProductTools:
    def test_detail_includes_attrs(self, box):
        d = box.get_product_detail(9001)
        assert d["name"] == "米白针织衫"
        assert d["attrs"]["材质"] == "100%羊毛"
        assert d["category"] == "针织衫"

    def test_missing_product(self, box):
        assert box.get_product_detail(99999) is None

    def test_out_of_stock_skus_are_returned_and_flagged(self, box):
        """缺货的也要返回。

        只返回有货的会让客服说"有货"，而用户想要的那个码恰恰没有——
        他下单时才发现，体验比直接说"L 码暂时缺货"差得多。
        """
        skus = box.get_sku_stock_price(9001)
        assert len(skus) == 2
        by_stock = {s["sku_no"]: s["in_stock"] for s in skus}
        assert by_stock == {"S-M": True, "S-L": False}

    def test_size_chart_is_parsed(self, box):
        c = box.get_size_chart(9001)
        assert c["chart"]["表头"][0] == "尺码"
        assert c["note"] == "宽松版型"

    def test_no_size_chart_is_not_an_error(self, box):
        """查不到是确定结论，不是故障——继续走检索即可。"""
        assert box.get_size_chart(9002) is None

    def test_query_failure_raises_rather_than_returning_empty(self, box):
        """故障必须抛出。返回空列表会被上层当成"确实没有规格"，
        于是对用户说"这款没有在售规格"——而真相是数据库连不上。"""
        from sqlalchemy import text

        with box.engine.begin() as conn:
            conn.execute(text("DROP TABLE sku"))
        with pytest.raises(ToolError):
            box.get_sku_stock_price(9001)


class TestListCatalog:
    """店铺列表的批量取数。

    **这一组盯的是查询次数，不是返回值。** 老写法每个商品四次往返，
    12 个商品时看不出来；加到 579 个商品实测 2317 次查询 / 597ms，
    而那还是 SQLite。功能测试对这个退化完全无感——它返回的数据是对的。
    """

    def _count_queries(self, box, fn):
        from sqlalchemy import event

        n = {"q": 0}

        def bump(*a, **kw):
            n["q"] += 1

        event.listen(box.engine, "before_cursor_execute", bump)
        try:
            out = fn()
        finally:
            event.remove(box.engine, "before_cursor_execute", bump)
        return out, n["q"]

    def test_查询次数不随商品数增长(self, box):
        items, n = self._count_queries(box, lambda: box.list_catalog())
        assert len(items) >= 2
        # ID 列表 + 商品 + 属性 + SKU + 尺码表 + 类目表 + 素材 = 7
        assert n <= 7, f"用了 {n} 次查询，说明又退回逐个商品查了"

    def test_只带出审核通过且有文件的素材(self, box):
        """**这是那道审核闸门唯一真正起作用的地方。**

        后台点"通过"如果只是把徽章翻个色、买家侧照旧什么都看不到，
        那个按钮就是装饰。反过来，这里漏掉过滤，审核就白做了——
        待审的素材会直接挂到商品详情页上。
        """
        item = {i["id"]: i for i in box.list_catalog()}[9001]
        paths = [a["path"] for a in item["assets"]]
        assert paths == ["generated/9001-w.png"], (
            "只有 approved 且有文件的那条该出来；"
            f"实际：{paths}（pending 的漏出来了，或者没文件的也带出来了）"
        )
        assert item["assets"][0]["ai_generated"] is True, (
            "《标识办法》要求生成内容可识别，标识跟着数据走")
        assert box.degraded == []

    def test_没有素材的商品是空列表不是缺键(self, box):
        """缺键的话页面上 ``p.assets.map`` 直接抛异常，整块不渲染。"""
        item = {i["id"]: i for i in box.list_catalog()}[9002]
        assert item["assets"] == []

    def test_素材表读不到时降级但留痕(self, box):
        """表没建（迁移漏跑）不该让整个店铺页挂掉，但也不能装成
        "这些商品本来就没有素材"——那样漏跑的迁移能躲很久。"""
        from sqlalchemy import text

        with box.engine.begin() as conn:
            conn.execute(text("DROP TABLE marketing_asset"))
        items = box.list_catalog()
        assert len(items) >= 2, "商品还得出得来"
        assert all(i["assets"] == [] for i in items)
        assert box.degraded and "素材" in box.degraded[0], "降级必须留痕"

    def test_一次给全页面要的东西(self, box):
        item = {i["id"]: i for i in box.list_catalog()}[9001]
        assert item["name"] == "米白针织衫"
        assert item["attrs"]["材质"] == "100%羊毛"
        assert len(item["skus"]) == 2
        assert item["size_chart"]["chart"]["表头"][0] == "尺码"
        # 缺货的也要在列表里带出来并标好，理由同 get_sku_stock_price
        assert {s["sku_no"]: s["in_stock"] for s in item["skus"]} == {
            "S-M": True, "S-L": False}

    def test_没有尺码表不是错误(self, box):
        item = {i["id"]: i for i in box.list_catalog()}[9002]
        assert item["size_chart"] is None

    def test_带出一级类目(self, box):
        """侧边栏要的是「服装」，不是「针织衫」——570 个商品有 65 个
        三级类目，全列出来没人翻得动。"""
        item = {i["id"]: i for i in box.list_catalog()}[9001]
        assert item["root_category"] == "服装"

    def test_类目没有路径时退回三级名(self, box):
        """老种子里的类目有 path，手工插的可能没有。没有 path 时
        退回自己的名字，而不是让侧边栏出现一堆空白项。"""
        from sqlalchemy import text

        with box.engine.begin() as conn:
            conn.execute(text("UPDATE category SET path = ''"))
        item = {i["id"]: i for i in box.list_catalog()}[9001]
        assert item["root_category"] == "针织衫"

    def test_查询失败照样抛出(self, box):
        from sqlalchemy import text

        with box.engine.begin() as conn:
            conn.execute(text("DROP TABLE product_attr"))
        with pytest.raises(ToolError):
            box.list_catalog()


# ---------------------------------------------------------------- 订单号抽取


class TestOrderNoExtraction:
    @pytest.mark.parametrize("text,expected", [
        ("我的订单2026080100001到哪了", "2026080100001"),
        ("订单号 2026080100001 查一下", "2026080100001"),
        ("2026080100001", "2026080100001"),
    ])
    def test_extracts(self, text, expected):
        assert extract_order_no(text) == expected

    @pytest.mark.parametrize("text", [
        "我买的那件到哪了", "160cm穿什么码", "这个299贵不贵", "",
    ])
    def test_ignores_short_numbers(self, text):
        """尺码、价格、身高都是数字，别当成订单号去查。"""
        assert extract_order_no(text) is None


# ---------------------------------------------------------------- 渲染


class TestRendering:
    def test_skus_show_stock_state(self):
        out = render_skus([
            {"spec": "M", "price": 299.0, "origin_price": 399.0,
             "stock": 38, "in_stock": True},
            {"spec": "L", "price": 299.0, "origin_price": None,
             "stock": 0, "in_stock": False},
        ])
        assert "有货" in out and "缺货" in out
        assert "原价 399" in out

    def test_empty_skus(self):
        assert render_skus([]).strip()

    def test_order_status_is_chinese(self):
        out = render_order({
            "order_no": "X1", "status": "shipped", "spec": "米白 M",
            "quantity": 1, "amount": 299.0, "express_company": "中通",
            "express_no": "78123", "tracks": [{"ts": "08-02", "desc": "运输中"}],
        })
        assert "已发货" in out and "中通" in out and "运输中" in out

    def test_size_chart_is_tabular(self):
        out = render_size_chart({
            "chart": {"表头": ["尺码", "胸围"], "行": [["M", 100]]},
            "note": "宽松",
        })
        assert "尺码 | 胸围" in out and "M | 100" in out and "宽松" in out


# ---------------------------------------------------------------- 接进图


def _deps(tools=None, intent="realtime_stock_price", hits=None, llm=None) -> Deps:
    return Deps(
        llm=llm or FakeLlmClient(intent=intent),
        retriever=StubRetriever(hits if hits is not None else []),
        tools=tools,
    )


def _state(product_id=9001, user_id=10086) -> AgentState:
    st = AgentState()
    st.session.current_product_id = product_id
    st.session.user_id = user_id
    return st


class TestToolsInGraph:
    def _box(self) -> StubToolBox:
        return StubToolBox(
            skus={9001: [{"spec": "M", "price": 299.0, "origin_price": None,
                          "stock": 38, "in_stock": True}]},
            charts={9001: {"chart": {"表头": ["尺码"], "行": [["M"]]}, "note": ""}},
            orders={"2026080100001": {
                "user_id": 10086, "order_no": "2026080100001", "status": "shipped",
                "spec": "米白 M", "quantity": 1, "amount": 299.0,
                "express_company": "中通", "express_no": "78123", "tracks": [],
            }},
        )

    def test_stock_question_uses_tools_not_rag(self):
        """库存问题绝不能走 RAG——历史对话里的"有货"早就过期了。"""
        deps = _deps(tools=self._box())
        s = graph.safe_run_turn("还有货吗", _state(), deps)

        assert not s.handover
        assert "get_sku_stock_price" in deps.tools.calls
        assert deps.retriever.queries == [], "实时类问题不该走检索"
        assert "sku" in s.tool_results

    def test_order_question_queries_the_order(self):
        deps = _deps(tools=self._box(), intent="order_logistics")
        s = graph.safe_run_turn("我的订单2026080100001到哪了", _state(), deps)
        assert not s.handover
        assert "order" in s.tool_results

    def test_someone_elses_order_is_not_leaked(self):
        """端到端的越权拦截——这是整条链路上最不能出错的一处。"""
        box = self._box()
        box.orders["2026080400004"] = {"user_id": 10087, "order_no": "2026080400004",
                                       "status": "shipped"}
        deps = _deps(tools=box, intent="order_logistics")
        s = graph.safe_run_turn("查一下订单2026080400004", _state(user_id=10086), deps)

        assert "order" not in s.tool_results
        assert "shipped" not in s.answer and "已发货" not in s.answer
        assert box.permission_denials

    def test_missing_order_no_asks_instead_of_handover(self):
        """没给订单号是可以自己解决的，别浪费一次人工。"""
        deps = _deps(tools=self._box(), intent="order_logistics")
        s = graph.safe_run_turn("我的快递到哪了", _state(), deps)
        assert not s.handover
        assert "订单号" in s.answer

    def test_sizing_uses_chart_and_still_retrieves(self):
        """尺码表给硬数据，知识库给"偏大偏小"的经验——两个都要。"""
        hit = Citation(item_id=1, title="尺码建议", content="偏大一码",
                       score=0.8, dense_score=0.8, bm25_score=1.0)
        deps = _deps(tools=self._box(), intent="sizing", hits=[hit])
        s = graph.safe_run_turn("160cm穿什么码", _state(), deps)

        assert "size_chart" in s.tool_results
        assert deps.retriever.queries, "尺码问题仍要走检索"
        assert not s.handover

    def test_no_toolbox_means_handover_not_stale_rag(self):
        """没接工具就老实转人工，绝不退回 RAG 拿过期数据顶上。"""
        deps = _deps(tools=None)
        s = graph.safe_run_turn("还有货吗", _state(), deps)
        assert s.handover
        assert s.handover_reason is HandoverReason.TOOL_FAILURE
        assert deps.retriever.queries == []

    def test_tool_failure_goes_to_human(self):
        deps = _deps(tools=StubToolBox(fail=True))
        s = graph.safe_run_turn("还有货吗", _state(), deps)
        assert s.handover
        assert s.handover_reason is HandoverReason.TOOL_FAILURE

    def test_unknown_product_asks_which_one(self):
        deps = _deps(tools=self._box())
        s = graph.safe_run_turn("多少钱", _state(product_id=None), deps)
        assert not s.handover
        assert "哪款" in s.answer

    def test_tool_calls_land_in_trace(self):
        """没有埋点就没法回答"这个答案是查出来的还是编的"。"""
        deps = _deps(tools=self._box())
        s = graph.safe_run_turn("还有货吗", _state(), deps)
        assert s.trace.tools_called
        call = s.trace.tools_called[0]
        assert call["name"] == "get_sku_stock_price" and call["hit"] is True

    def test_tool_data_is_marked_authoritative_in_the_prompt(self):
        """工具数据必须标明优先于知识库。

        不标的话，模型会把三个月前的"目前有货"和刚查到的"库存 0"
        平等对待然后挑一个说——而实时数据存在的全部意义就是覆盖过期知识。
        """
        from app.agent.nodes import _tools_text

        st = _state()
        st.tool_results["sku"] = "- M：299 元，缺货"
        text = _tools_text(st)
        assert "以此为准" in text and "缺货" in text

    def test_no_tool_results_adds_nothing_to_the_prompt(self):
        from app.agent.nodes import _tools_text

        assert _tools_text(_state()) == ""


class TestOrderNoBoundary:
    """中文与数字之间没有 ``\\b`` 词边界。

    中文字符在 Python 的 Unicode 模式下也算 ``\\w``，所以
    "订单2026080100001"的"单"与"2"之间不构成词边界——用 ``\\b``
    会一个订单号都提不出来。而中文用户恰恰习惯把单号紧贴着字写。
    """

    @pytest.mark.parametrize("text", [
        "我的订单2026080100001到哪了",
        "订单号2026080100001",
        "帮我查2026080100001这单",
        "2026080100001这个订单",
    ])
    def test_digits_glued_to_chinese(self, text):
        assert extract_order_no(text) == "2026080100001"

    def test_still_rejects_longer_digit_runs(self):
        """25 位以上不是订单号，别把一串乱码当单号去查。"""
        assert extract_order_no("1" * 30) is None
