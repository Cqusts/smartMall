"""直播切片。

重点不在"能不能转写"——那是 FunASR/百炼的事。重点在**这条链路比现成的
切片工具多做了什么**：热词闭环、商品对齐、以及口播话术进知识库那个出口。
"""

from __future__ import annotations

import pytest

from smartmall_pipeline.clip.asr import FakeAsrClient
from smartmall_pipeline.clip.segment import (
    Segment, align_products, segment_transcript, to_knowledge_items,
)
from smartmall_pipeline.clip.transcript import (
    Sentence, Transcript, build_hotwords,
)
from smartmall_pipeline.models import BizType, ReviewStatus

PRODUCTS = [
    {"id": 9001, "name": "米白色圆领宽松针织衫", "short_name": "针织衫",
     "alias": ["米白针织"], "attrs": {"材质": "100%羊毛"}},
    {"id": 9011, "name": "头层牛皮系带马丁短靴", "short_name": "马丁短靴",
     "alias": [], "attrs": {"材质": "头层牛皮"}},
]


class FakeSegmenter:
    """按固定脚本返回分段结果。"""

    def __init__(self, segments=None, fail=False):
        self.segments = segments
        self.fail = fail
        self.prompts: list[str] = []

    def complete_json(self, *, model, system, user):
        from smartmall_pipeline.gates.gate3_model import LlmUnavailableError

        self.prompts.append(user)
        if self.fail:
            raise LlmUnavailableError("注入的故障")
        if self.segments is not None:
            return {"segments": self.segments}
        return {"segments": [
            {"begin_line": 0, "end_line": 2, "kind": "feature",
             "product_id": 9001, "topic": "针织衫材质",
             "script": "这件针织衫是100%羊毛，克重320克，做了抗起球处理。"},
            {"begin_line": 3, "end_line": 3, "kind": "urge",
             "product_id": 9001, "topic": "催单", "script": "三二一上链接"},
            {"begin_line": 4, "end_line": 5, "kind": "qa",
             "product_id": 9011, "topic": "马丁靴选码",
             "script": "脚背高的建议大一码。"},
        ]}


# ---------------------------------------------------------------- 热词


class TestHotwords:
    """**这是这条链路的闭环所在。**

    "莱赛尔""醋酸""马丁靴"是通用 ASR 最容易听错的一类词——专有名词，
    而且新词层出不穷。但它们全都写在商品表里。
    """

    def test_product_names_become_hotwords(self):
        hw = build_hotwords(PRODUCTS)
        assert "针织衫" in hw and "马丁短靴" in hw

    def test_aliases_are_included(self):
        """主播的口语叫法才是 ASR 真正会听到的。"""
        assert "米白针织" in build_hotwords(PRODUCTS)

    def test_material_words_are_included(self):
        """材质是误识别重灾区："莱赛尔"会被听成"来赛尔"。"""
        hw = build_hotwords([{"id": 1, "name": "x", "short_name": "x",
                              "attrs": {"材质": "莱赛尔纤维 55%／棉 45%"}}])
        assert "莱赛尔纤维" in hw and "棉" not in hw  # 单字不进热词表

    def test_long_names_are_broken_into_speakable_fragments(self):
        """主播不会一字不差地念"米白色圆领宽松针织衫"。
        剥掉通用修饰后的碎片才是他会说出口的。"""
        hw = build_hotwords([{"id": 1, "name": "米白色圆领宽松针织衫",
                              "short_name": ""}])
        assert any("针织衫" in w for w in hw)
        assert "宽松" not in hw, "通用修饰词当热词只会制造噪音"

    def test_single_characters_are_excluded(self):
        """ASR 热词是带权重的，塞进"款""色"会让它到处误召回。"""
        assert all(len(w) >= 2 for w in build_hotwords(PRODUCTS))

    def test_no_duplicates(self):
        hw = build_hotwords(PRODUCTS + PRODUCTS)
        assert len(hw) == len(set(hw))

    def test_hotwords_actually_reach_the_asr(self):
        """算出来不传过去等于没做。"""
        client = FakeAsrClient()
        client.transcribe("x.mp4", hotwords=build_hotwords(PRODUCTS))
        assert "针织衫" in client.seen_hotwords


# ---------------------------------------------------------------- 转写模型


class TestTranscript:
    def test_normalises_field_names(self):
        """各家字段名不统一，在这里一次归一，别让下游每处都猜。"""
        t = Transcript.from_rows([
            {"text": "甲", "begin_time": 0, "end_time": 1000},
            {"sentence": "乙", "start": 1000, "end": 2000, "speaker_id": "2"},
        ])
        assert [s.text for s in t.sentences] == ["甲", "乙"]
        assert t.sentences[1].speaker == "2"

    def test_empty_sentences_are_dropped(self):
        t = Transcript.from_rows([{"text": "  ", "begin_time": 0, "end_time": 1}])
        assert t.sentences == []

    def test_sorted_by_time(self):
        t = Transcript.from_rows([
            {"text": "后", "begin_time": 5000, "end_time": 6000},
            {"text": "前", "begin_time": 0, "end_time": 1000},
        ])
        assert [s.text for s in t.sentences] == ["前", "后"]

    def test_slice_takes_overlapping_not_contained(self):
        """**按重叠取而不是按包含取。**

        分段边界经常落在句子中间，按包含取会把跨界那句整句丢掉——
        而那常常是承上启下的一句。
        """
        t = Transcript(sentences=[
            Sentence("甲", 0, 5000), Sentence("乙", 4000, 9000),
            Sentence("丙", 9000, 12000),
        ])
        assert [s.text for s in t.slice(4500, 8000)] == ["甲", "乙"]


# ---------------------------------------------------------------- 分段


class TestSegmentation:
    def _t(self):
        return FakeAsrClient().transcribe("live.mp4")

    def test_produces_segments_with_timestamps(self):
        segs, _ = segment_transcript(FakeSegmenter(), self._t(), PRODUCTS)
        assert segs and all(s.end_ms > s.begin_ms for s in segs)

    def test_urge_talk_is_dropped(self):
        """"三二一上链接"没有任何可复用的信息，
        进了知识库只会污染检索。"""
        segs, stats = segment_transcript(FakeSegmenter(), self._t(), PRODUCTS)
        assert all(s.kind != "urge" for s in segs if s.is_useful)
        assert "催单话术" in stats.dropped

    def test_qa_segments_are_kept(self):
        """主播回答观众提问是**最有价值**的片段——那是真实的客服知识。"""
        segs, _ = segment_transcript(FakeSegmenter(), self._t(), PRODUCTS)
        assert any(s.kind == "qa" for s in segs)

    def test_a_failed_window_does_not_kill_the_run(self):
        segs, stats = segment_transcript(FakeSegmenter(fail=True), self._t(), PRODUCTS)
        assert segs == []
        assert "分段调用失败" in stats.dropped, (
            "模型故障不能显示成'主播没说话'"
        )

    def test_out_of_range_line_numbers_are_clamped(self):
        """模型常把最后一段的 end_line 多写一行。
        丢掉这一条而不是整窗。"""
        segs, _ = segment_transcript(
            FakeSegmenter(segments=[
                {"begin_line": 0, "end_line": 999, "kind": "feature",
                 "product_id": 9001, "topic": "t", "script": "s"},
                {"begin_line": -3, "end_line": -1, "kind": "feature",
                 "product_id": 9001, "topic": "t", "script": "s"},
            ]),
            self._t(), PRODUCTS,
        )
        assert len(segs) == 1

    def test_empty_transcript(self):
        segs, stats = segment_transcript(FakeSegmenter(), Transcript(), PRODUCTS)
        assert segs == [] and stats.input_count == 0

    def test_catalog_is_in_the_prompt(self):
        """不给商品清单，模型只能瞎猜 product_id。"""
        seg = FakeSegmenter()
        segment_transcript(seg, self._t(), PRODUCTS)
        assert "9001" in seg.prompts[0] and "马丁短靴" in seg.prompts[0]

    def test_overlapping_duplicates_are_merged(self):
        """窗口重叠会让同一片段产出两次。留话术更长的那份——
        被边界截断的那半整理出来明显更短。"""
        from smartmall_pipeline.clip.segment import _dedup

        long_ = Segment(0, 10000, script="完整的一段话术，内容比较长")
        short = Segment(500, 9000, script="截断的")
        assert _dedup([short, long_]) == [long_]


# ---------------------------------------------------------------- 商品对齐


class TestProductAlignment:
    """**这是切片工具做不了的部分**——它们没有商品库。

    切片要挂到商品详情页，话术要按商品进知识库，对不上商品的切片没用。
    """

    def test_spoken_name_wins_over_the_model_guess(self):
        """模型看的是一个窗口的上下文，在商品切换处经常判错。
        主播实际说出口的叫法更可信。"""
        seg = Segment(0, 5000, product_id=9001,
                      raw_text="下面看这双马丁短靴，头层牛皮的。")
        aligned, pending = align_products([seg], PRODUCTS)
        assert aligned[0].product_id == 9011 and not pending

    def test_the_spoken_alias_is_recorded_for_writeback(self):
        """**闭环的最后一环。** 人工确认后回写成别名，
        下一场直播它就是 ASR 热词。"""
        seg = Segment(0, 5000, product_id=9011, raw_text="这件米白针织真的软")
        aligned, _ = align_products([seg], PRODUCTS)
        assert aligned[0].product_id == 9001
        assert aligned[0].matched_alias == "米白针织"

    def test_longer_names_win(self):
        """「马丁短靴」比「靴」更可信。"""
        products = PRODUCTS + [{"id": 7, "name": "靴", "short_name": "靴"}]
        seg = Segment(0, 1, raw_text="这双马丁短靴")
        aligned, _ = align_products([seg], products)
        assert aligned[0].product_id == 9011

    def test_unmatched_goes_to_the_human_queue(self):
        """对不上就交给人，不要硬塞一个商品——挂错商品的切片
        比没有切片糟糕得多。"""
        seg = Segment(0, 5000, raw_text="今天天气不错啊家人们")
        aligned, pending = align_products([seg], PRODUCTS)
        assert not aligned and len(pending) == 1

    def test_no_alias_recorded_when_the_model_was_already_right(self):
        """模型判对了就不算"新叫法"，别把商品自己的名字回写成别名。"""
        seg = Segment(0, 1, product_id=9001, raw_text="这件针织衫")
        aligned, _ = align_products([seg], PRODUCTS)
        assert aligned[0].matched_alias == ""


# ---------------------------------------------------------------- 出口


class TestKnowledgeExit:
    """**第二个出口才是这条链路接进这套体系的理由。**

    FunClip、HotClip 的产物只有短视频。而主播讲了两小时"这个面料起球吗、
    掉色吗、薄不薄"——那是最真实的商品知识，而客服知识库现在全是通用问答。
    """

    def _segs(self):
        return [
            Segment(0, 9000, "feature", 9001, "针织衫材质", "100%羊毛，抗起球。",
                    raw_text="x"),
            Segment(9000, 14000, "qa", 9001, "起球", "做了抗起球处理。", raw_text="x"),
            Segment(14000, 16000, "urge", 9001, "催单", "三二一上链接", raw_text="x"),
        ]

    def test_only_useful_segments_become_knowledge(self):
        items = to_knowledge_items(self._segs(), source_ref="live-0801")
        assert len(items) == 2, "催单话术不该进知识库"

    def test_biz_type_is_script(self):
        """口播话术不是问答对。分错了覆盖度矩阵会算进错误的格子，
        "哪里缺知识"就看不准了。"""
        items = to_knowledge_items(self._segs(), source_ref="live-0801")
        assert all(i.biz_type is BizType.SCRIPT for i in items)

    def test_traceable_back_to_the_timestamp(self):
        """能追回是哪场直播的第几秒——否则话术出了错没法核。"""
        items = to_knowledge_items(self._segs(), source_ref="live-0801")
        assert items[0].source_ref == "live-0801#0-9000"
        assert items[0].source == "live_clip"

    def test_everything_waits_for_review(self):
        """**和商品图那边不一样。** 那边"颜色能对上 SKU"是一次真实的
        交叉印证，可以自动通过；这里转写有错字、模型整理有偏差，
        没有任何东西印证过它。
        """
        items = to_knowledge_items(self._segs(), source_ref="x")
        assert all(i.review_status is ReviewStatus.PENDING for i in items)

    def test_product_binding_is_carried(self):
        items = to_knowledge_items(self._segs(), source_ref="x",
                                   category_of={9001: 1024})
        assert items[0].product_ids == [9001] and items[0].category_id == 1024

    def test_qa_segments_are_tagged(self):
        """答疑类要能被单独捞出来——它们是知识库里质量最高的一批。"""
        items = to_knowledge_items(self._segs(), source_ref="x")
        qa = [i for i in items if "答疑" in i.tags]
        assert len(qa) == 1


# ---------------------------------------------------------------- 切片

import shutil

from smartmall_pipeline.clip.cut import (
    CutResult, FfmpegMissing, cut_segments, ffmpeg_path, probe_duration_ms,
)

pytestmark_ff = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="需要 ffmpeg"
)


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    """造一段 25 秒的测试视频，关键帧间隔 2 秒。

    间隔要明确设死：``-c copy`` 的切点误差完全由关键帧密度决定，
    不设的话编码器默认值一变，测试就会莫名其妙地飘。
    """
    if shutil.which("ffmpeg") is None:
        pytest.skip("需要 ffmpeg")
    import subprocess

    out = tmp_path_factory.mktemp("clip") / "live.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=25",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
        "-c:a", "aac", "-shortest", str(out),
    ], check=True, capture_output=True, timeout=120)
    return out


@pytestmark_ff
class TestCutting:
    def _segs(self):
        return [
            Segment(0, 9000, "feature", 9001, "针织衫材质", "羊毛的", raw_text="x"),
            Segment(9000, 11000, "urge", 9001, "催单", "上链接", raw_text="x"),
            Segment(16000, 25000, "qa", 9011, "选码", "大一码", raw_text="x"),
        ]

    def test_cuts_useful_segments_only(self, video, tmp_path):
        """催单话术切出来没人看，而每切一段都要读写一遍磁盘。"""
        r = cut_segments(video, self._segs(), tmp_path)
        assert len(r.clips) == 2
        assert r.skipped.get("非讲解片段") == 1

    def test_files_are_playable(self, video, tmp_path):
        """**返回码 0 不代表切出了东西。** ``-c copy`` 在起点落在关键帧
        之后时会产出 0 字节文件，而 ffmpeg 一声不吭。"""
        r = cut_segments(video, self._segs(), tmp_path)
        for c in r.clips:
            assert c.bytes > 0 and probe_duration_ms(c.path) > 0

    def test_padding_is_applied(self, video, tmp_path):
        """按 ASR 时间戳硬切，开头第一个字会被削掉半个音节。"""
        r = cut_segments(video, self._segs()[:1], tmp_path, pad_ms=400)
        assert r.clips[0].begin_ms == 0          # 已经在 0，不能切成负数
        assert r.clips[0].end_ms == 9400

    def test_end_is_clamped_to_the_video(self, video, tmp_path):
        """ASR 最后一句的时间戳偶尔超过视频实际长度（VAD 补的尾巴），
        照切会产出 0 字节文件。"""
        seg = [Segment(20000, 99000, "qa", 9001, "尾巴", "话术", raw_text="x")]
        r = cut_segments(video, seg, tmp_path)
        assert r.clips and r.clips[0].end_ms <= 25000

    def test_too_short_is_skipped(self, video, tmp_path):
        seg = [Segment(5000, 5500, "qa", 9001, "太短", "话术", raw_text="x")]
        r = cut_segments(video, seg, tmp_path, pad_ms=0)
        assert not r.clips and r.skipped.get("片段过短") == 1

    def test_precise_mode_is_tighter_than_copy(self, video, tmp_path):
        """把这个取舍钉住，别让人以为 copy 是精确的。

        实测 9.4 秒的片段：copy 切出 11.1 秒（关键帧对齐），
        precise 切出 9.4 秒但慢四倍。
        """
        seg = [Segment(16000, 25000, "qa", 9011, "选码", "大一码", raw_text="x")]
        fast = cut_segments(video, seg, tmp_path / "f", precise=False).clips[0]
        exact = cut_segments(video, seg, tmp_path / "p", precise=True).clips[0]
        drift_fast = abs(probe_duration_ms(fast.path) - fast.duration_ms)
        drift_exact = abs(probe_duration_ms(exact.path) - exact.duration_ms)
        assert drift_exact < drift_fast

    def test_filenames_carry_product_and_topic(self, video, tmp_path):
        """人工审片时靠文件名判断，点开一个个看太慢。"""
        r = cut_segments(video, self._segs(), tmp_path)
        assert "9001" in r.clips[0].path.name
        assert "针织衫材质" in r.clips[0].path.name

    def test_missing_video_fails_loudly(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            cut_segments(tmp_path / "nope.mp4", self._segs(), tmp_path)


class TestFfmpegAvailability:
    def test_missing_ffmpeg_says_how_to_install(self, monkeypatch):
        """这条路径没有降级方案，所以报错要能照着做。"""
        monkeypatch.setattr(shutil, "which", lambda _: None)
        with pytest.raises(FfmpegMissing) as e:
            ffmpeg_path()
        assert "winget" in str(e.value) or "brew" in str(e.value)
