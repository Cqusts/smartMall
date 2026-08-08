"""列宽计算。

这一层看着琐碎，但它决定了所有终端表格能不能竖着扫——而"竖着扫"
正是漏斗报告、覆盖度热力图这些输出唯一的用法。
"""

from __future__ import annotations

from smartmall_pipeline.coverage import build_matrix
from smartmall_pipeline.models import (
    BizType, FunnelReport, GateStats, KnowledgeItem, KnowledgeType,
)
from smartmall_pipeline.textwidth import display_width, pad, truncate


def _item(category_id: int = 1, ktype: KnowledgeType = KnowledgeType.SIZING):
    return KnowledgeItem(
        biz_type=BizType.QA, title="标题", content="正文", source="test",
        category_id=category_id, knowledge_type=ktype,
    )


class TestDisplayWidth:
    def test_ascii_is_one_column_each(self):
        assert display_width("abc") == 3

    def test_cjk_is_two(self):
        assert display_width("女装") == 4

    def test_mixed(self):
        assert display_width("女装 T恤") == 4 + 1 + 1 + 2

    def test_fullwidth_punctuation_counts_as_two(self):
        """中文标点是全角（F），漏算它会让带"（）"的标签整体偏移。"""
        assert display_width("（空）") == 6

    def test_combining_marks_take_no_space(self):
        """组合字符叠在前一个字上，不占位。算成一列会让整行右移。"""
        assert display_width("é") == 1          # U+0065 U+0301
        assert display_width("é") == 1          # U+00E9，同一个字的两种编码

    def test_empty(self):
        assert display_width("") == 0


class TestPad:
    def test_pads_to_columns_not_characters(self):
        """**这条是这个模块存在的理由。**

        "女装"两个字符、四列；补到 10 列应该给 6 个空格，
        而 ``f"{'女装':<10}"`` 会给 8 个——于是这一行比别人宽两列。
        """
        assert display_width(pad("女装", 10)) == 10
        assert display_width(f"{'女装':<10}") == 12

    def test_every_label_lands_on_the_same_column(self):
        labels = ["女装", "T恤", "运动户外", "3C"]
        assert {display_width(pad(x, 12)) for x in labels} == {12}

    def test_right_align(self):
        assert pad("次数", 8, right=True) == "    次数"

    def test_oversized_is_returned_as_is(self):
        """补齐不该偷偷截断——丢数据要由调用方显式决定。"""
        assert pad("一二三四五", 4) == "一二三四五"


class TestTruncate:
    def test_short_enough_is_untouched(self):
        assert truncate("女装", 10) == "女装"

    def test_cuts_by_columns(self):
        out = truncate("春秋新款针织开衫", 10)
        assert display_width(out) <= 10 and out.endswith("…")

    def test_never_exceeds_the_budget(self):
        """省略号自己也占一列。不给它留位置，截出来的串会正好超一列——
        这种一列的溢出最难发现，只有恰好被截断的那几行会歪。"""
        for w in range(1, 20):
            assert display_width(truncate("春秋新款针织开衫外套", w)) <= w

    def test_does_not_split_a_wide_char_in_half(self):
        """奇数列预算下不能把一个汉字切成一列。"""
        out = truncate("女装外套", 5)
        assert display_width(out) <= 5


class TestRenderersAreAligned:
    """渲染层的回归护栏：改了模板忘了走 pad 就会被这里挡住。"""

    def _col(self, line: str, needle: str) -> int:
        return display_width(line[: line.index(needle)])

    def test_funnel_drop_reasons_line_up(self):
        """五个关卡名恰好一样长，所以关卡那一列碰巧没露馅——
        丢弃原因才是长短不一的那些，而它们正是要竖着比数量的行。
        """
        rep = FunnelReport(batch_id="test-batch", stages=[
            GateStats(
                gate="①机器清洗", input_count=100, output_count=60,
                dropped={"近重复": 20, "含敏感信息未通过脱敏": 15, "PII": 5},
            ),
        ])
        rows = [ln for ln in rep.render().splitlines() if ln.startswith("    ✗")]
        assert len(rows) == 3
        widths = {display_width(ln) for ln in rows}
        assert len(widths) == 1, f"数量列没对齐：{[(r, display_width(r)) for r in rows]}"

    def test_funnel_gate_names_line_up(self):
        rep = FunnelReport(batch_id="test-batch", stages=[
            GateStats(gate="①机器清洗", input_count=100, output_count=90),
            GateStats(gate="gate-x", input_count=90, output_count=88),
        ])
        rows = [ln for ln in rep.render().splitlines() if "→" in ln]
        assert len(rows) == 2
        assert len({self._col(ln, "→") for ln in rows}) == 1, rows

    def test_coverage_matrix_columns_line_up(self):
        """热力图按列读——类目名长短不一时列错位，图就白画了。"""
        m = build_matrix([_item(1), _item(2)],
                         categories={1: "女装", 2: "运动户外鞋服"})
        rows = [ln for ln in m.render().splitlines()
                if ln.startswith(("女装", "运动户外"))]
        assert len(rows) == 2
        widths = {display_width(ln) for ln in rows}
        assert len(widths) == 1, f"矩阵行宽不一致：{[(r, display_width(r)) for r in rows]}"

    def test_matrix_header_keeps_a_gutter_between_columns(self):
        """截断宽度必须小于列宽，否则相邻列名会挨在一起连成一串——
        对齐了但读不出来，和没对齐一样糟。"""
        import re

        lines = build_matrix([_item(1)], categories={1: "女装"}).render().splitlines()
        header = next(ln for ln in lines if ln.startswith("类目"))

        labels = re.findall(r"\S+", header[len("类目") :])
        assert len(labels) >= 6, f"列名粘在一起了：{header!r}"
        assert all(display_width(x) <= 6 for x in labels), labels

    def test_matrix_rule_matches_the_header(self):
        """分隔线用 len() 算就会比表头短两列——表头里有"类目"。"""
        lines = build_matrix([_item(1)], categories={1: "女装"}).render().splitlines()
        header = next(ln for ln in lines if ln.startswith("类目"))
        rule = next(ln for ln in lines if set(ln) == {"="})
        assert display_width(rule) == display_width(header)

    def test_long_category_name_does_not_blow_the_column(self):
        """``name[:14]`` 砍的是字符：14 个汉字是 28 列，整张表被撑爆。"""
        m = build_matrix([_item(1)], categories={1: "女士秋冬加厚羊毛混纺大衣系列"})
        lines = m.render().splitlines()
        row = next(ln for ln in lines if ln.startswith("女士"))
        header = next(ln for ln in lines if ln.startswith("类目"))
        assert display_width(row) == display_width(header)
