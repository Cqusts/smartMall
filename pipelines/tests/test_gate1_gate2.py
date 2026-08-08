"""关卡①②的单元测试。

合成数据故意注入了各关卡应清除的噪声，因此"关卡有没有生效"是可断言的。
"""

from __future__ import annotations

import pytest

from smartmall_pipeline.gates import gate1_machine as g1
from smartmall_pipeline.gates import gate2_rules as g2
from smartmall_pipeline.gates import pii
from smartmall_pipeline.ingest import synthetic
from smartmall_pipeline.models import Dialogue, Turn


def _dlg(*contents: tuple[str, str], session_no: str = "T1") -> Dialogue:
    return Dialogue(
        session_no=session_no,
        source_type="test",
        turns=[
            Turn(turn_index=i, role=r, content=c)  # type: ignore[arg-type]
            for i, (r, c) in enumerate(contents)
        ],
    )


# ---------------------------------------------------------------- PII


class TestPii:
    def test_masks_phone(self):
        r = pii.mask("我手机号13812345678，麻烦发货前联系我")
        assert "13812345678" not in r.text
        assert "<PHONE>" in r.text
        assert r.hits["phone"] == 1

    def test_masks_id_card_before_bank_card(self):
        """身份证 18 位，若先跑银行卡规则（16-19 位）会被误判。"""
        r = pii.mask("身份证330106199001011234")
        assert "<ID_CARD>" in r.text
        assert "<BANK_CARD>" not in r.text

    def test_masks_bank_card(self):
        r = pii.mask("退到我卡上6222021234567890123可以吗")
        assert "<BANK_CARD>" in r.text
        assert "6222021234567890123" not in r.text

    def test_bank_card_does_not_leak_phone(self):
        """银行卡中间恰好含 11 位数字，不能被手机号规则先吃掉。"""
        r = pii.mask("6222021381234567890")
        assert r.text == "<BANK_CARD>"

    def test_masks_address(self):
        r = pii.mask("地址是浙江省杭州市西湖区文三路100号")
        assert "<ADDRESS>" in r.text
        assert "文三路" not in r.text

    @pytest.mark.parametrize("addr", [
        "北京市朝阳区建国路88号",
        "上海市浦东新区世纪大道1号",
        "天津市和平区南京路12号",
        "重庆市渝中区解放碑步行街5号",
    ])
    def test_masks_municipality_addresses(self, addr):
        """**这里漏过一个真实的洞。**

        规则原先是 ``[一-龥]{2,8}(?:省|自治区)?``——省字可选，但那 2-8 个
        汉字是必需的。于是"北京市朝阳区…"匹配不上，因为"北京市"前面
        什么都没有。四个直辖市的收货地址就这样从关卡①穿了过去，
        而北京上海恰恰是地址最密集的地方。

        是做图片转述那条路时撞出来的：一张快递面单，电话被脱敏了，
        地址原样留着。
        """
        assert pii.mask(f"收货地址 {addr}").text.count("<ADDRESS>") == 1

    @pytest.mark.parametrize("text", [
        "退货区域的运费5号之前免",
        "这个小区不让进",
        "市区内当天送达",
    ])
    def test_does_not_swallow_ordinary_sentences(self, text):
        """把省市整段放宽以后要防过度匹配——脱敏误伤的是正常语料，
        知识库会莫名其妙缺一块。"""
        assert pii.mask(text).text == text

    def test_masks_email(self):
        r = pii.mask("发我邮箱 zhang.san+1@example.com 谢谢")
        assert "<EMAIL>" in r.text

    def test_leaves_clean_text_untouched(self):
        text = "这件针织衫是100%羊毛的"
        r = pii.mask(text)
        assert r.text == text
        assert not r.changed

    def test_contains_pii_detects_residual(self):
        assert pii.contains_pii("手机13812345678")
        assert not pii.contains_pii("手机<PHONE>")


# ---------------------------------------------------------------- 关卡①


class TestGate1:
    def test_drops_too_short(self):
        kept, stats = g1.run([_dlg(("user", "在"), ("agent", "嗯"))])
        assert kept == []
        assert stats.dropped["文本过短"] == 1

    def test_drops_too_few_turns(self):
        kept, stats = g1.run([_dlg(("user", "这件针织衫是什么面料的呢请问"))])
        assert kept == []
        assert "轮次过少" in stats.dropped

    def test_drops_non_chinese(self):
        kept, stats = g1.run([
            _dlg(("user", "what is the material of this sweater"),
                 ("agent", "it is made of pure wool fabric"))
        ])
        assert kept == []
        assert "非中文/乱码" in stats.dropped

    def test_drops_external_link(self):
        kept, stats = g1.run([
            _dlg(("user", "这个多少钱呀想了解一下"),
                 ("agent", "加微信 abc123456 详聊哦亲"))
        ])
        assert kept == []
        assert "含外链/引流" in stats.dropped

    def test_drops_flagged_words(self):
        kept, stats = g1.run([
            _dlg(("user", "你们这个是原单的吗质量怎么样"),
                 ("agent", "是的亲，品质有保证的呢"))
        ])
        assert kept == []
        assert "敏感词/竞品词" in stats.dropped

    def test_masks_pii_and_keeps_raw(self):
        d = _dlg(
            ("user", "我手机号13812345678，发货前联系我谢谢"),
            ("agent", "好的亲已经备注啦，我们会提前联系您的"),
        )
        kept, stats = g1.run([d])
        assert len(kept) == 1
        assert "<PHONE>" in kept[0].turns[0].content
        # 原文保留在 raw_content，落库时加密
        assert kept[0].turns[0].raw_content is not None
        assert "13812345678" in kept[0].turns[0].raw_content
        assert stats.modified["PII脱敏:phone"] == 1

    def test_dedups_near_duplicates(self):
        """真实会话长度下，追加一句寒暄不应逃过去重。

        注意 Jaccard 阈值对文本长度敏感：会话越短，追加同样一句话
        对相似度的冲击越大。这里用接近真实的长度（真实客服会话动辄十几轮），
        短文本的去重由「文本过短」规则先行拦截。
        """
        base = (
            ("user", "这件针织衫会不会起球呢我有点担心洗几次就不能穿了"),
            ("agent", "不会的亲我们这款做了抗起球处理实验室洗五次都没有明显起球"),
            ("user", "那面料成分是什么呀"),
            ("agent", "是100%羊毛的克重320g秋冬单穿就够暖了"),
            ("user", "怎么洗比较好呢"),
            ("agent", "建议手洗或者干洗水温不要超过30度反面洗更好"),
        )
        d1 = _dlg(*base, session_no="A")
        d2 = _dlg(*base, ("user", "好的谢谢啦"), session_no="B")
        kept, stats = g1.run([d1, d2])
        assert len(kept) == 1
        assert stats.dropped["近重复"] == 1

    def test_does_not_dedup_different_topics(self):
        """不同话题的等长会话不应被误判为重复。"""
        d1 = _dlg(
            ("user", "这件针织衫会不会起球呢我有点担心洗几次就不能穿了"),
            ("agent", "不会的亲我们这款做了抗起球处理实验室洗五次都没有明显起球"),
            session_no="A",
        )
        d2 = _dlg(
            ("user", "请问你们发什么快递呀大概几天能到浙江这边"),
            ("agent", "默认发中通快递的亲江浙沪一般两到三天就能签收"),
            session_no="B",
        )
        kept, stats = g1.run([d1, d2])
        assert len(kept) == 2
        assert "近重复" not in stats.dropped

    def test_keeps_distinct_dialogues(self):
        d1 = _dlg(("user", "这件针织衫会不会起球"), ("agent", "不会的做了抗起球处理"), session_no="A")
        d2 = _dlg(("user", "发什么快递呀大概几天到"), ("agent", "默认中通江浙沪两三天"), session_no="B")
        kept, _ = g1.run([d1, d2])
        assert len(kept) == 2

    def test_minhash_is_deterministic(self):
        """去重结果必须可复现，否则同一批数据两次跑出不同的知识库。"""
        text = "这件针织衫是100%羊毛的克重320g"
        assert g1.minhash_signature(text) == g1.minhash_signature(text)


# ---------------------------------------------------------------- 关卡②


class TestParameterize:
    def test_replaces_amount(self):
        out, hits = g2.parameterize("这件99元包邮")
        assert "<AMOUNT>" in out
        assert hits["amount"] == 1

    def test_replaces_date(self):
        out, _ = g2.parameterize("预计3月1日送达")
        assert "<DATE>" in out

    def test_replaces_order_no(self):
        out, hits = g2.parameterize("您的订单3421789023已发货")
        assert "<ORDER_NO>" in out
        assert hits["order_no"] == 1

    def test_preserves_duration(self):
        """时长是知识本身，不能被占位化——这是最容易写错的地方。"""
        out, _ = g2.parameterize("我们48小时内安排发货")
        assert "48小时" in out
        assert "<AMOUNT>" not in out
        assert "<ORDER_NO>" not in out

    def test_preserves_duration_alongside_order_no(self):
        out, _ = g2.parameterize("订单3421789023我们48小时内发货")
        assert "48小时" in out
        assert "<ORDER_NO>" in out

    def test_keeps_material_percentage(self):
        out, _ = g2.parameterize("这款是100%羊毛的")
        assert "100%羊毛" in out


class TestGate2:
    def test_strips_system_turns(self):
        d = _dlg(
            ("system", "正在为您转接人工客服"),
            ("user", "这件针织衫什么面料"),
            ("agent", "亲这款是100%羊毛的呢"),
        )
        kept, stats = g2.run([d])
        assert len(kept) == 1
        assert all(t.role != "system" for t in kept[0].turns)
        assert stats.modified["剥离system轮次"] == 1

    def test_strips_welcome_phrase(self):
        d = _dlg(
            ("agent", "您好，很高兴为您服务，请问有什么可以帮您的呢？"),
            ("user", "这件针织衫什么面料"),
            ("agent", "亲这款是100%羊毛的呢"),
        )
        kept, stats = g2.run([d])
        assert stats.modified["剥离系统话术"] == 1
        assert len(kept[0].turns) == 2

    def test_strips_pleasantries(self):
        d = _dlg(
            ("user", "在吗"),
            ("agent", "在的亲～"),
            ("user", "这件针织衫什么面料"),
            ("agent", "亲这款是100%羊毛的呢"),
            ("user", "好的谢谢"),
        )
        kept, stats = g2.run([d])
        assert len(kept[0].turns) == 2
        assert stats.modified["剥离无效寒暄"] >= 2

    def test_drops_when_no_qa_pair(self):
        d = _dlg(("user", "这件针织衫什么面料"), ("user", "有人吗在不在"))
        kept, stats = g2.run([d])
        assert kept == []
        assert "缺少问答配对" in stats.dropped

    def test_drops_when_only_pleasantries(self):
        d = _dlg(("user", "在吗"), ("agent", "在的"), ("user", "好的"))
        kept, stats = g2.run([d])
        assert kept == []
        assert "有效轮次不足" in stats.dropped

    def test_reindexes_turns(self):
        d = _dlg(
            ("system", "正在为您转接人工客服"),
            ("user", "这件针织衫什么面料"),
            ("agent", "亲这款是100%羊毛的呢"),
        )
        kept, _ = g2.run([d])
        assert [t.turn_index for t in kept[0].turns] == [0, 1]


# ---------------------------------------------------------------- 串联


class TestPipelineChain:
    def test_synthetic_batch_flows_through_both_gates(self):
        dialogues = synthetic.generate_batch(200, seed=7)
        after1, s1 = g1.run(dialogues)
        after2, s2 = g2.run(after1)

        # 每一关都必须真的淘汰了东西，否则说明规则没生效
        assert s1.drop_count > 0, "关卡①未淘汰任何数据"
        assert s1.dropped.get("近重复", 0) > 0, "MinHash 去重未生效"
        assert any(k.startswith("PII脱敏") for k in s1.modified), "PII 脱敏未生效"
        assert s2.modified.get("剥离无效寒暄", 0) > 0, "寒暄剥离未生效"
        assert len(after2) > 0, "全被清空了，规则过严"

        # 出口不得残留 PII —— 这是合规红线
        for dlg in after2:
            for turn in dlg.turns:
                assert not pii.contains_pii(turn.content), (
                    f"残留 PII: {turn.content}"
                )

    def test_funnel_is_monotonically_decreasing(self):
        dialogues = synthetic.generate_batch(100, seed=11)
        after1, s1 = g1.run(dialogues)
        after2, s2 = g2.run(after1)
        assert s1.input_count >= s1.output_count
        assert s2.input_count == s1.output_count
        assert s2.input_count >= s2.output_count
