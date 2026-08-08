"""商品图打标。

这一层的价值全在**它拒绝说什么**上。会看图的模型到处都是；不肯把
"这看起来像羊毛"说成"材质：100%羊毛"的才有用——因为后一句会被客服
当事实播给用户。
"""

from __future__ import annotations

import pytest

from smartmall_pipeline.models import Modality, ReviewStatus
from smartmall_pipeline.vision import (
    ASSET_USER,
    FakeVisionClient,
    ProductFacts,
    color_conflicts,
    describe_asset,
    tag_assets,
    to_knowledge_item,
    unobservable_claims,
)


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "9001.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return str(p)


@pytest.fixture
def facts(img):
    return ProductFacts(
        product_id=9001, name="米白色圆领宽松针织衫", category="针织衫",
        category_id=1024, sku_colors=["米白", "藏青"],
        attrs={"材质": "100%羊毛", "克重": "320g"}, image_path=img,
    )


# ---------------------------------------------------------------- 越界复查


class TestUnobservableClaims:
    """**提示词是请求，不是约束。**

    提示词里已经明写"不要出现材质克重"，模型仍然很愿意顺口补一句
    "采用优质羊毛面料"。而这句话进了知识库，客服就会当事实说出去。
    所以输出后必须再用规则复查一遍。
    """

    @pytest.mark.parametrize("text", [
        "米白色圆领针织衫，采用优质羊毛面料，手感柔软。",
        "版型宽松，克重适中。",
        "建议手洗，水温不超过30度。",
        "衣长约 68cm，肩宽 44 厘米。",
        "产地为浙江，做工精细。",
    ])
    def test_caught(self, text):
        assert unobservable_claims({"description": text}), f"没拦住：{text}"

    @pytest.mark.parametrize("text", [
        "米白色圆领长袖针织衫，版型宽松，袖口与下摆有罗纹收边。",
        "焦糖色飞行员夹克，金属拉链，双开口袋，风格复古。",
        "红底白波点连衣裙，方领设计，裙摆飘逸，适合度假。",
    ])
    def test_normal_descriptions_pass(self, text):
        """误报的代价是把本来正确的描述也剪掉，知识库越洗越空。"""
        assert not unobservable_claims({"description": text})

    @pytest.mark.parametrize("text", [
        "复古水洗做旧工艺，颜色有层次。",       # 水洗是做旧工艺，看得见
        "粗针织质感，纹路明显。",                # 织法，看得见
        "灯芯绒面，竖条纹理。",                  # 质感，看得见
        "牛仔布料拼接，双线明缝。",              # 布种，看得见
        "绗缝格纹，蓬松感强。",                  # 工艺，看得见
    ])
    def test_texture_and_weave_are_observable(self, text):
        """**线划在"成分"而不是"任何和布有关的词"上。**

        针织、牛仔、灯芯绒、绗缝是织法与质感，肉眼分得出来；
        羊毛还是腈纶分不出来。一刀切掉所有布料词，会把正确的描述也剪光，
        知识库越洗越空——那和编造是两种错，但都让这条链路白做。
        """
        assert not unobservable_claims({"description": text}), f"误伤：{text}"

    def test_details_are_checked_too(self):
        """越界内容藏在 details 里一样要抓——那也是会进知识库的自由文本。"""
        assert unobservable_claims({"details": ["罗纹袖口", "320g 加厚"]})

    def test_numbers_with_units_are_caught_by_pattern(self):
        """关键词列不全所有写法。"260克""24cm"靠正则兜住。"""
        assert unobservable_claims({"description": "重约260克，很压手。"})

    def test_a_bare_number_is_not_a_violation(self):
        """"3 个口袋"是看得见的，不该被数字规则误伤。"""
        assert not unobservable_claims({"description": "正面有 3 个贴袋。"})


class TestPromptStatesTheBoundary:
    def test_forbidden_topics_are_spelled_out(self):
        """靠规则兜底不代表提示词可以不写——两道都要有，
        规则拦下的每一条都是一次浪费掉的模型调用。"""
        body = ASSET_USER.format(name="x", category="y")
        for w in ("面料成分", "克重", "洗涤方式", "产地", "价格"):
            assert w in body

    def test_prompt_says_blank_is_the_right_answer(self):
        """不写这句，模型会把每个字段都填满——空着才是正确答案。"""
        body = ASSET_USER.format(name="x", category="y")
        assert "留空是正确答案" in body


# ---------------------------------------------------------------- 交叉验证


class TestColorConflicts:
    """VLM 说的颜色对不上 SKU，通常不是模型眼神差，是**图配错了商品**。

    而配错图没人会主动发现：页面上看着挺好，只有客服答"这款有藏青色"
    而图里是红的时候才暴露——那时已经在用户面前了。
    """

    def test_matching_colors_are_fine(self, facts):
        assert color_conflicts({"colors": ["米白"]}, facts) == []

    def test_loose_match_on_the_color_suffix(self, facts):
        """SKU 写"米白"、模型说"米白色"，是同一个。"""
        assert color_conflicts({"colors": ["米白色"]}, facts) == []

    def test_unknown_color_is_flagged(self, facts):
        assert color_conflicts({"colors": ["酒红"]}, facts) == ["酒红"]

    def test_no_sku_colors_means_no_check(self, img):
        """包类商品可能没有颜色规格。没有权威值时不能凭空判错。"""
        f = ProductFacts(product_id=1, name="包", image_path=img)
        assert color_conflicts({"colors": ["酒红"]}, f) == []


# ---------------------------------------------------------------- 组装


class TestKnowledgeItem:
    def test_authoritative_attrs_come_from_the_database(self, facts):
        """材质克重不是模型看出来的，是运营填在 product_attr 里的。

        这是这条链路的核心分工：VLM 负责"看见"，数据库负责"知道"。
        """
        item = to_knowledge_item({"colors": ["米白"], "confidence": 0.9}, facts)
        assert "100%羊毛" in item.content and "320g" in item.content
        assert "商品档案登记值" in item.content, "要标明这段不是看出来的"

    def test_modality_and_source_mark_the_path(self, facts):
        item = to_knowledge_item({"colors": ["米白"]}, facts)
        assert item.modality is Modality.IMAGE
        assert item.source == "vlm_asset"
        assert item.source_ref and "9001" in item.source_ref, "要能追回是哪张图"
        assert item.product_ids == [9001]

    def test_clean_output_is_auto_approved(self, facts):
        """**不能一律 pending。** 全 pending 等于这条链路白做——
        知识永远进不了索引，而人没那个功夫逐条点。

        颜色对得上 SKU、没有越界描述，说明它已经被权威数据印证过一次，
        这条界线是可判定的。
        """
        item = to_knowledge_item({"colors": ["米白"]}, facts, flags=[])
        assert item.review_status is ReviewStatus.APPROVED

    def test_flagged_output_waits_for_a_human(self, facts):
        """**也不能一律 approved**——那等于把模型的话直接当事实。"""
        item = to_knowledge_item({"colors": ["酒红"]}, facts,
                                 flags=["颜色与SKU不符:酒红"])
        assert item.review_status is ReviewStatus.PENDING

    def test_offending_sentence_is_removed_not_the_whole_item(self, facts):
        """整条丢掉的话，一句多余的"优质面料"会废掉一整条本来正确的
        颜色版型描述；留着又会被当事实播出去。剔掉那一句是唯一说得通的。"""
        item = to_knowledge_item({
            "colors": ["米白"],
            "description": "米白色圆领针织衫，版型宽松。采用优质羊毛面料。袖口有罗纹。",
        }, facts)
        assert "羊毛面料" not in (item.summary or "") + item.content.split(
            "以下为商品档案登记值")[0]
        assert "版型宽松" in item.content, "正确的部分不该被连坐"

    def test_confidence_is_capped(self, facts):
        """模型报 0.99 不代表真有 0.99。留出余量，
        免得下游按"接近确定"处理一条没人看过的模型输出。"""
        item = to_knowledge_item({"colors": ["米白"], "confidence": 1.0}, facts)
        assert item.quality_score <= 0.95


# ---------------------------------------------------------------- 流程


class TestDescribeAsset:
    def test_happy_path(self, facts):
        r = describe_asset(FakeVisionClient(), facts)
        assert r.ok and not r.flags
        assert r.item.review_status is ReviewStatus.APPROVED

    def test_model_failure_is_not_an_empty_result(self, facts):
        """**调用失败 ≠ 这张图没有知识。** 和关卡③、检索层同一条原则：
        业务结论只能来自成功的响应。算成"没产出"会让漏斗数字说谎。
        """
        r = describe_asset(FakeVisionClient(raise_on="米白色圆领"), facts)
        assert not r.ok and r.error and r.item is None

    def test_missing_image_is_reported_not_crashed(self, facts):
        facts.image_path = "/nope/missing.jpg"
        r = describe_asset(FakeVisionClient(), facts)
        assert not r.ok and "读图失败" in r.error

    def test_unsupported_format_is_rejected_early(self, tmp_path, facts):
        p = tmp_path / "x.bmp"
        p.write_bytes(b"x")
        facts.image_path = str(p)
        r = describe_asset(FakeVisionClient(), facts)
        assert not r.ok and "读图失败" in r.error

    def test_overreaching_model_output_is_flagged_and_trimmed(self, facts):
        client = FakeVisionClient(payload={
            "colors": ["米白"],
            "description": "米白色针织衫。采用 100% 羊毛面料，克重 320g。",
            "confidence": 0.9,
        })
        r = describe_asset(client, facts)
        assert r.ok
        assert any("越界描述" in f for f in r.flags)
        assert r.item.review_status is ReviewStatus.PENDING

    def test_wrong_photo_is_caught_by_the_sku_cross_check(self, facts):
        client = FakeVisionClient(payload={"colors": ["酒红"], "confidence": 0.9})
        r = describe_asset(client, facts)
        assert any("颜色与SKU不符" in f for f in r.flags)


class TestBatch:
    def test_stats_separate_failures_from_review(self, facts, tmp_path):
        bad = ProductFacts(product_id=2, name="坏图", image_path="/nope.jpg")
        results, stats = tag_assets(FakeVisionClient(), [facts, bad])

        assert stats.input_count == 2 and stats.output_count == 1
        assert stats.input_unit == "张图" and stats.output_unit == "条目"
        assert "模型调用或读图失败" in stats.dropped

    def test_flagged_items_still_count_as_output(self, facts):
        """送人工复核的是**产出**，不是淘汰——它只是还没定稿。
        算进 dropped 会让漏斗看起来像模型不работает。"""
        client = FakeVisionClient(payload={"colors": ["酒红"]})
        results, stats = tag_assets(client, [facts])
        assert stats.output_count == 1 and not stats.dropped
        assert "送人工复核" in stats.modified
