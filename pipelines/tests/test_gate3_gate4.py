"""关卡③④ 测试。

关卡③的 LLM 客户端通过协议注入，用 FakeLlmClient 替身即可离线测试编排逻辑：
判定门槛、抽取过滤、失败路径、活动类有效期、风格标注是否只学风格不学知识。
"""

from __future__ import annotations

import pytest

from smartmall_pipeline.gates import gate3_model as g3
from smartmall_pipeline.gates import gate4_human as g4
from smartmall_pipeline.models import (
    BizType,
    Dialogue,
    KnowledgeItem,
    KnowledgeType,
    ReviewStatus,
    Turn,
)


def _dlg(session_no: str = "S1", product_ids: list[int] | None = None) -> Dialogue:
    return Dialogue(
        session_no=session_no,
        source_type="synthetic",
        agent_id=101,
        product_ids=product_ids or [1024],
        turns=[
            Turn(turn_index=0, role="user", content="这件针织衫是什么面料"),
            Turn(turn_index=1, role="agent", content="亲这款是100%羊毛的呢"),
        ],
    )


def _item(**kw) -> KnowledgeItem:
    base = dict(
        biz_type=BizType.QA,
        title="这件是什么面料",
        content="100%羊毛",
        source="synthetic",
        source_ref="session:S1",
        confidence=0.95,
        product_ids=[1024],
    )
    base.update(kw)
    return KnowledgeItem(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- JSON 容错


class TestParseJsonLenient:
    def test_plain_json(self):
        assert g3.parse_json_lenient('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert g3.parse_json_lenient('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_lang(self):
        assert g3.parse_json_lenient('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_prose_around(self):
        assert g3.parse_json_lenient('好的，结果如下：{"a": 1} 希望有帮助') == {"a": 1}

    def test_raises_on_garbage(self):
        with pytest.raises(g3.LlmError):
            g3.parse_json_lenient("完全不是 JSON")


# ---------------------------------------------------------------- 关卡③


class TestGate3:
    def test_extracts_multiple_items_from_one_dialogue(self):
        """一段对话里可能藏着多个知识点，要拆成多条。"""
        llm = g3.FakeLlmClient(items_per_dialogue=3)
        items, samples, stats, _ = g3.run([_dlg()], llm)
        assert len(items) == 3
        assert stats.output_count == 3
        assert all(i.biz_type is BizType.QA for i in items)

    def test_drops_when_no_reusable_knowledge(self):
        llm = g3.FakeLlmClient(has_knowledge=False)
        items, _, stats, _ = g3.run([_dlg()], llm)
        assert items == []
        assert stats.dropped["无可复用知识"] == 1

    def test_drops_outdated(self):
        llm = g3.FakeLlmClient(is_outdated=True)
        items, _, stats, _ = g3.run([_dlg()], llm)
        assert items == []
        assert stats.dropped["已过期"] == 1

    def test_drops_not_generalizable(self):
        llm = g3.FakeLlmClient(generalizable=0.2)
        items, _, stats, _ = g3.run([_dlg()], llm)
        assert items == []
        assert stats.dropped["不可泛化"] == 1

    def test_drops_low_quality(self):
        llm = g3.FakeLlmClient(quality=0.1)
        items, _, stats, _ = g3.run([_dlg()], llm)
        assert items == []
        assert stats.dropped["质量分过低"] == 1

    def test_survives_llm_failure(self):
        """单条失败不能拖垮整批。"""
        llm = g3.FakeLlmClient(raise_on="坏样本")
        bad = _dlg("BAD")
        bad.turns[0].content = "坏样本"
        items, _, stats, _ = g3.run([bad, _dlg("OK")], llm)
        assert len(items) == 2  # 只有好的那条产出了 2 个 item
        assert any(k.startswith("判定失败") for k in stats.dropped)

    def test_items_are_pending_review(self):
        """未审核的知识绝不进索引——出口一律 pending。"""
        llm = g3.FakeLlmClient()
        items, _, _, _ = g3.run([_dlg()], llm)
        assert all(i.review_status is ReviewStatus.PENDING for i in items)

    def test_promotion_gets_expiry(self):
        """活动类必须设有效期，否则客服会一直播报过期活动。"""
        llm = g3.FakeLlmClient(knowledge_type="promotion")
        items, _, stats, _ = g3.run([_dlg()], llm)
        assert all(i.valid_to is not None for i in items)
        assert stats.modified["活动类设置有效期"] >= 1

    def test_non_promotion_has_no_expiry(self):
        llm = g3.FakeLlmClient(knowledge_type="spec")
        items, _, _, _ = g3.run([_dlg()], llm)
        assert all(i.valid_to is None for i in items)

    def test_source_ref_is_traceable(self):
        """任意一条知识都必须能追回原始记录。"""
        llm = g3.FakeLlmClient()
        d = _dlg("TRACE1")
        d.ods_id = 4242
        items, _, _, _ = g3.run([d], llm)
        assert all(i.source_ref == "ods:4242" for i in items)

    def test_uses_cheap_model_for_triage(self):
        """粗筛必须走便宜模型，否则百万级会话的成本不可接受。"""
        llm = g3.FakeLlmClient()
        g3.run([_dlg()], llm)
        triage_calls = [m for m, s in llm.calls if s.startswith("你是电商客服对话")]
        assert triage_calls == ["chat-light"]

    def test_sft_sample_carries_style_not_knowledge(self):
        """微调样本的 system prompt 不能含具体商品知识。

        否则模型会把"这件是 100% 羊毛"当成通用知识记住，
        回答别的商品时也说羊毛（docs/10-finetune.md 的失败模式）。
        """
        llm = g3.FakeLlmClient()
        _, samples, _, _ = g3.run([_dlg()], llm)
        assert len(samples) == 1
        sample = samples[0]
        system = sample.messages[0]
        assert system["role"] == "system"
        assert "羊毛" not in system["content"]
        assert sample.style_tags == ["口语化", "短句"]
        assert sample.scene_tag == "售前咨询"
        assert sample.agent_id == 101


class TestGate3Uncertainty:
    def test_uncertain_answer_gets_confidence_capped(self):
        """含"我确认一下"的答案不能作为权威知识直接上线。"""

        class Uncertain(g3.FakeLlmClient):
            def complete_json(self, *, model, system, user):
                if system.startswith("你是电商知识库的编辑"):
                    return {
                        "items": [{
                            "question": "能机洗吗",
                            "answer": "这个我确认一下再回复您",
                            "tags": [], "confidence": 0.95,
                        }]
                    }
                return super().complete_json(model=model, system=system, user=user)

        items, _, stats, _ = g3.run([_dlg()], Uncertain())
        assert len(items) == 1
        assert items[0].confidence < g3.Gate3Config().human_review_below
        assert stats.modified["含不确定表述→降置信度"] == 1


# ---------------------------------------------------------------- 关卡④


class TestGate4Triage:
    def test_high_confidence_auto_approves(self):
        """高置信度自动通过，否则人工就是吞吐瓶颈。"""
        items = [_item(confidence=0.97) for _ in range(20)]
        tasks, approved, stats = g4.triage(items)
        assert len(approved) > 0
        assert all(i.review_status is ReviewStatus.APPROVED for i in approved)
        assert stats.modified["自动通过"] == len(approved)

    def test_low_confidence_goes_p0(self):
        tasks, approved, _ = g4.triage([_item(confidence=0.3)])
        assert approved == []
        assert len(tasks) == 1
        assert tasks[0].priority is g4.Priority.P0

    def test_uncertain_wording_goes_p0(self):
        tasks, _, _ = g4.triage([_item(confidence=0.95, content="这个可能要看批次")])
        assert tasks[0].priority is g4.Priority.P0
        assert "不确定表述" in tasks[0].reason

    def test_attribute_conflict_goes_p1(self):
        """客服说的与商品属性对不上，是最危险的一类错误。"""
        item = _item(confidence=0.95, content="这件的材质是纯棉的哦")
        tasks, _, _ = g4.triage(
            [item], product_attrs={1024: {"材质": "100%羊毛"}}
        )
        assert len(tasks) == 1
        assert tasks[0].priority is g4.Priority.P1
        assert "材质" in tasks[0].reason

    def test_hot_question_goes_p1(self):
        tasks, _, _ = g4.triage(
            [_item(confidence=0.95)],
            question_freq={"这件是什么面料": 200},
        )
        assert tasks[0].priority is g4.Priority.P1
        assert "高频" in tasks[0].reason

    def test_tasks_sorted_by_priority(self):
        items = [
            _item(confidence=0.95, title="hot"),
            _item(confidence=0.2, title="low"),
            _item(confidence=0.7, title="mid"),
        ]
        tasks, _, _ = g4.triage(items, question_freq={"hot": 999})
        assert [t.priority for t in tasks] == sorted(t.priority for t in tasks)
        assert tasks[0].priority is g4.Priority.P0

    def test_active_learning_reduces_human_load(self):
        """主动学习的意义：绝大多数高置信度条目不该占用人工。"""
        items = [_item(confidence=0.96) for _ in range(200)]
        tasks, approved, _ = g4.triage(items)
        assert len(approved) / len(items) > 0.8, "人工负载过高，主动学习未生效"

    def test_label_studio_payload_has_prediction(self):
        """预标注是效率关键：人工只需确认或改，而不是从零标注。"""
        tasks, _, _ = g4.triage([_item(confidence=0.3)])
        payload = tasks[0].to_label_studio()
        assert payload["data"]["question"]
        assert payload["predictions"][0]["model_version"] == "gate3-extract"
        results = payload["predictions"][0]["result"]
        assert any(r["from_name"] == "answer" for r in results)


class TestApplyAnnotation:
    def test_approve(self):
        item = _item(confidence=0.4)
        g4.apply_annotation(item, {"action": "approve"})
        assert item.review_status is ReviewStatus.APPROVED
        assert item.confidence == 1.0

    def test_reject(self):
        item = _item()
        g4.apply_annotation(item, {"action": "reject"})
        assert item.review_status is ReviewStatus.REJECTED

    def test_revise_updates_content(self):
        item = _item(content="旧答案")
        g4.apply_annotation(item, {"action": "approve", "answer": "人工改写的新答案"})
        assert item.content == "人工改写的新答案"
        assert item.review_status is ReviewStatus.REVISED

    def test_revise_updates_tags(self):
        item = _item()
        g4.apply_annotation(item, {"action": "approve", "tags": ["材质", "养护"]})
        assert item.tags == ["材质", "养护"]
