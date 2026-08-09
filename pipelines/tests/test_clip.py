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


class TestLocalAsrModelChoice:
    """**第一版默认选了 SenseVoiceSmall，两样致命的问题。**

    它更小更快、还能识别情绪，看起来是个好选择。实测：40 秒音频返回
    **一整句**（FunASR 打了 ``punctuation timestamps could not be aligned,
    falling back to VAD segments``），而且热词参数传进去不报错、只是不生效——
    那套"商品名→热词→转写更准"的闭环一直在空转，没有任何迹象。
    """

    def test_default_model_supports_timestamps_and_hotwords(self):
        from smartmall_pipeline.clip.asr import LocalFunAsrClient

        c = LocalFunAsrClient()
        assert "paraformer" in c.model.lower(), (
            "默认必须是 Paraformer 系：时间戳和热词只有它两样都有"
        )
        assert c.supports_hotwords

    def test_sensevoice_is_flagged_as_unable(self):
        from smartmall_pipeline.clip.asr import LocalFunAsrClient

        assert not LocalFunAsrClient(model="iic/SenseVoiceSmall").supports_hotwords

    def test_hotwords_are_not_silently_dropped(self, capsys):
        """静默失效是最糟的一种：不报错、只是不生效，
        而"商品名全是错字"看起来像模型不行，不像配置错了。"""
        from smartmall_pipeline.clip.asr import LocalFunAsrClient

        c = LocalFunAsrClient(model="iic/SenseVoiceSmall")
        c._pipe = _StubPipe([{"sentence_info": [
            {"text": "甲", "start": 0, "end": 1000}]}])
        c.transcribe("x.wav", hotwords=["针织衫", "马丁靴"])
        assert "不支持热词" in capsys.readouterr().out

    def test_hotwords_reach_a_capable_model(self):
        from smartmall_pipeline.clip.asr import LocalFunAsrClient

        c = LocalFunAsrClient()
        c._pipe = pipe = _StubPipe([{"sentence_info": [
            {"text": "甲", "start": 0, "end": 1000}]}])
        c.transcribe("x.wav", hotwords=["针织衫"])
        assert "针织衫" in pipe.kwargs["hotword"]

    def test_a_single_sentence_over_long_audio_is_flagged(self, capsys):
        """整段音频只回来一两句 = 时间戳没对上，模型退化成 VAD 整段。

        不检查的话下游会安静地把整场直播当成一个片段，而"分段效果差"
        看起来像提示词问题，不像转写问题——实测就是这么被绕进去的。
        """
        from smartmall_pipeline.clip.asr import LocalFunAsrClient

        c = LocalFunAsrClient()
        c._pipe = _StubPipe([{"sentence_info": [
            {"text": "整整四十秒都在这一句里", "start": 0, "end": 40000}]}])
        t = c.transcribe("x.wav")
        assert len(t.sentences) == 1
        assert "时间戳多半没对上" in capsys.readouterr().out

    def test_normal_output_is_quiet(self, capsys):
        from smartmall_pipeline.clip.asr import LocalFunAsrClient

        c = LocalFunAsrClient()
        c._pipe = _StubPipe([{"sentence_info": [
            {"text": f"第{i}句", "start": i * 4000, "end": (i + 1) * 4000}
            for i in range(10)]}])
        c.transcribe("x.wav", hotwords=["针织衫"])
        assert capsys.readouterr().out == ""


class _StubPipe:
    def __init__(self, result):
        self.result = result
        self.kwargs: dict = {}

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return self.result


class TestModelNameResolution:
    """**同一个坑这个项目栽过三次了，这次钉住。**

    网关别名（chat-default / chat-light）只有 DashScope 的客户端翻译得了。
    把它当默认值写死，非 DashScope 用户必然撞 400，而症状离病因很远：
    第一次是服务端 new AgentConfig，表现成"这个问题我不太确定，帮您转接
    人工"；第二次是 clip 的 --segment-model，表现成"分段 0 段"。
    """

    def test_explicit_wins(self, monkeypatch):
        from smartmall_pipeline.cli import _resolve_model_name

        monkeypatch.setenv("SMARTMALL_SEGMENT_MODEL", "from-env")
        assert _resolve_model_name("cli-said", "SMARTMALL_SEGMENT_MODEL") == "cli-said"

    def test_env_beats_the_alias(self, monkeypatch):
        from smartmall_pipeline.cli import _resolve_model_name

        monkeypatch.setenv("SMARTMALL_EXTRACT_MODEL", "deepseek-chat")
        got = _resolve_model_name(None, "SMARTMALL_SEGMENT_MODEL",
                                  "SMARTMALL_EXTRACT_MODEL")
        assert got == "deepseek-chat"

    def test_alias_only_as_the_last_resort(self, monkeypatch):
        from smartmall_pipeline.cli import _resolve_model_name

        monkeypatch.delenv("SMARTMALL_SEGMENT_MODEL", raising=False)
        monkeypatch.delenv("SMARTMALL_EXTRACT_MODEL", raising=False)
        assert _resolve_model_name(None, "SMARTMALL_SEGMENT_MODEL") == "chat-default"

    def test_no_subcommand_hardcodes_a_gateway_alias(self):
        """扫整个 parser 树：任何参数的 default 都不许是网关别名。

        这是一条**结构性**的守卫——它拦的不是这一次的 bug，
        是下一次有人图省事写 default="chat-default"。
        """
        from smartmall_pipeline.cli import build_parser

        aliases = {"chat-default", "chat-light", "chat-fallback", "reasoning"}
        bad = []

        def walk(parser, path=""):
            for act in parser._actions:
                if isinstance(act.default, str) and act.default in aliases:
                    bad.append(f"{path} {act.option_strings or act.dest}")
                # choices 对普通参数是 list（--llm 的取值），
                # 只有子命令那一层才是 {名字: 子 parser}
                choices = getattr(act, "choices", None)
                if isinstance(choices, dict):
                    for name, sub in choices.items():
                        if hasattr(sub, "_actions"):
                            walk(sub, f"{path}/{name}")

        walk(build_parser())
        assert not bad, f"这些参数把网关别名写死成了默认值：{bad}"


# ---------------------------------------------------------------- 出口①素材


@pytest.fixture
def repo(tmp_path):
    """一套真的（SQLite）建好表的库。

    mock 掉 execute 只能证明"我调了一次"，证明不了那条 INSERT 写得对——
    而这几条 SQL 里出错的恰恰是列名和方言（LAST_INSERT_ID、INSERT IGNORE、
    ON DUPLICATE KEY 全是 MySQL 独有的）。
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    with engine.begin() as c:
        c.execute(text("""CREATE TABLE asset (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_no TEXT,
            modality TEXT, scene TEXT, oss_key TEXT, file_size INT,
            mime_type TEXT, duration_ms INT, file_hash TEXT, source TEXT,
            status TEXT, vlm_description TEXT, desc_review_status TEXT,
            tags TEXT, ai_generated INT DEFAULT 0, deleted INT DEFAULT 0)"""))
        c.execute(text("""CREATE TABLE asset_product_rel (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INT,
            product_id INT, rel_type TEXT, bind_source TEXT,
            bind_conf REAL, UNIQUE(asset_id, product_id, rel_type))"""))
        c.execute(text("""CREATE TABLE asset_clip_meta (
            asset_id INT PRIMARY KEY, live_id INT, live_title TEXT,
            start_ms INT, end_ms INT, transcript TEXT, objection_qa TEXT)"""))
        c.execute(text("""CREATE TABLE product (
            id INT PRIMARY KEY, name TEXT, alias TEXT)"""))
        c.execute(text("INSERT INTO product VALUES (9001, '针织衫', NULL)"))
        c.execute(text("""CREATE TABLE clip_alias_proposal (
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INT,
            alias TEXT, times INT DEFAULT 1, source_ref TEXT, sample TEXT,
            status TEXT DEFAULT 'pending', UNIQUE(product_id, alias))"""))

    class R:
        pass

    r = R()
    r.engine = engine
    return r


class TestRegisterClips:
    """**素材落在磁盘上不算入库。**

    只有进了 asset 表，切片才谈得上被商品详情页挂载、被客服答案引用、
    被审核流管起来——否则它就是一堆没人知道存在的 mp4。

    用真的 SQLite 跑真的 SQL：mock 掉 execute 只能证明"我调了一次"，
    证明不了那条 INSERT 写得对。
    """

    @pytest.fixture
    def clips(self, tmp_path):
        from smartmall_pipeline.clip.cut import ClipFile

        out = []
        for i, (kind, pid, alias) in enumerate(
            [("feature", 9001, ""), ("qa", 9001, "米白针织")]
        ):
            p = tmp_path / f"c{i}.mp4"
            p.write_bytes(b"fake-video-" + str(i).encode())
            seg = Segment(i * 9000, (i + 1) * 9000, kind, pid, f"话题{i}",
                          f"话术{i}", raw_text=f"原话{i}", matched_alias=alias)
            out.append(ClipFile(segment=seg, path=p, begin_ms=seg.begin_ms,
                                end_ms=seg.end_ms, bytes=p.stat().st_size))
        return out

    def test_writes_all_three_tables(self, repo, clips):
        """三张表一起写。只写主表不写关联的话，素材存在但挂不到商品上，
        看起来"入库成功"实际没用。"""
        from sqlalchemy import text

        from smartmall_pipeline.clip.register import register_clips

        r = register_clips(repo, clips, live_id=7, source_ref="live-0801")
        assert r.assets == 2 and r.relations == 2

        with repo.engine.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM asset")).scalar() == 2
            assert c.execute(
                text("SELECT COUNT(*) FROM asset_clip_meta")).scalar() == 2
            meta = c.execute(text(
                "SELECT live_id, start_ms, transcript FROM asset_clip_meta "
                "ORDER BY asset_id")).mappings().first()
            assert meta["live_id"] == 7 and meta["transcript"] == "原话0"

    def test_spoken_alias_gets_higher_confidence(self, repo, clips):
        """主播说出口的叫法是字面匹配，模型猜的是概率——不该同等对待。
        schema 里 bind_conf < 0.85 要进人工队列。"""
        from sqlalchemy import text

        from smartmall_pipeline.clip.register import register_clips

        register_clips(repo, clips, live_id=7)
        with repo.engine.connect() as c:
            confs = [r[0] for r in c.execute(text(
                "SELECT bind_conf FROM asset_product_rel ORDER BY asset_id"))]
        assert confs[0] < 0.85 <= confs[1]

    def test_reruns_do_not_duplicate(self, repo, clips):
        """同一段直播重跑一次不该产生两份素材。按文件 hash 去重。"""
        from smartmall_pipeline.clip.register import register_clips

        register_clips(repo, clips, live_id=7)
        again = register_clips(repo, clips, live_id=7)
        assert again.assets == 0 and again.skipped_duplicate == 2

    def test_clips_are_not_marked_ai_generated(self, repo, clips):
        """直播切片是真人录像的片段，不是 AI 生成的。标错会给它加上本不该
        有的 AI 标识角标——那是另一种意义上的不实标注。"""
        from sqlalchemy import text

        from smartmall_pipeline.clip.register import register_clips

        register_clips(repo, clips, live_id=7)
        with repo.engine.connect() as c:
            assert all(r[0] == 0 for r in c.execute(
                text("SELECT ai_generated FROM asset")))

    def test_qa_script_is_stored_separately(self, repo, clips):
        """异议处理话术是价值最高的一类，单独存一份便于捞取。"""
        from sqlalchemy import text

        from smartmall_pipeline.clip.register import register_clips

        register_clips(repo, clips, live_id=7)
        with repo.engine.connect() as c:
            qas = [r[0] for r in c.execute(
                text("SELECT objection_qa FROM asset_clip_meta ORDER BY asset_id"))]
        assert qas[0] is None and "话术1" in qas[1]

    def test_empty_input_is_a_noop(self, repo):
        from smartmall_pipeline.clip.register import register_clips

        assert register_clips(repo, [], live_id=1).assets == 0


class TestAliasLoop:
    """**闭环的最后一环。**

    商品名 → 热词 → 转写更准 → 对齐更准 → 主播叫法 → 人工确认 → 回写别名
    → 下一场的热词。缺了回写这一步，前面那条链就不是环，只是一条线。
    """

    def test_proposals_are_queued_not_applied(self, repo):
        """**不自动回写。** 对齐命中的词可能是巧合——"这颜色跟刚才那件
        米白很像"讲的其实是另一件商品。错别名喂回热词表会让往后每一场
        直播都朝错的方向偏，这类错误自己会放大。"""
        from sqlalchemy import text

        from smartmall_pipeline.clip.register import propose_aliases

        segs = [Segment(0, 1, "qa", 9001, "t", "s", raw_text="这件米白真软",
                        matched_alias="米白")]
        assert propose_aliases(repo, segs, source_ref="live-0801") == 1

        with repo.engine.connect() as c:
            row = c.execute(text(
                "SELECT alias, status, sample FROM clip_alias_proposal"
            )).mappings().first()
            assert row["status"] == "pending"
            assert "这件米白真软" in row["sample"], "人工要看上下文才能判断"
            # 还没进商品别名
            assert c.execute(
                text("SELECT alias FROM product WHERE id=9001")).scalar() is None

    def test_repeats_accumulate(self, repo):
        """说得越多越可信。次数是人工排序的依据。"""
        from sqlalchemy import text

        from smartmall_pipeline.clip.register import propose_aliases

        segs = [Segment(0, 1, "qa", 9001, "t", "s", raw_text="x",
                        matched_alias="米白")]
        propose_aliases(repo, segs)
        propose_aliases(repo, segs)
        with repo.engine.connect() as c:
            assert c.execute(
                text("SELECT times FROM clip_alias_proposal")).scalar() == 2

    def test_approve_writes_into_product_alias(self, repo):
        """写进去之后，下一次 build_hotwords 就会把它算进热词表。"""
        import json as _j

        from sqlalchemy import text

        from smartmall_pipeline.clip.register import apply_alias, propose_aliases

        propose_aliases(repo, [Segment(0, 1, "qa", 9001, "t", "s",
                                       raw_text="x", matched_alias="米白")])
        with repo.engine.connect() as c:
            pid = c.execute(text("SELECT id FROM clip_alias_proposal")).scalar()

        assert apply_alias(repo, pid) == (9001, "米白")
        with repo.engine.connect() as c:
            alias = _j.loads(c.execute(
                text("SELECT alias FROM product WHERE id=9001")).scalar())
            assert alias == ["米白"]
            assert c.execute(
                text("SELECT status FROM clip_alias_proposal")).scalar() == "approved"

    def test_the_loop_closes_into_hotwords(self, repo):
        """**把整个环跑一遍。** 确认后的叫法必须真的出现在热词表里，
        否则这条链只是看起来闭合。"""
        import json as _j

        from sqlalchemy import text

        from smartmall_pipeline.clip.register import apply_alias, propose_aliases

        propose_aliases(repo, [Segment(0, 1, "qa", 9001, "t", "s",
                                       raw_text="x", matched_alias="米白")])
        with repo.engine.connect() as c:
            pid = c.execute(text("SELECT id FROM clip_alias_proposal")).scalar()
        apply_alias(repo, pid)

        with repo.engine.connect() as c:
            alias = _j.loads(c.execute(
                text("SELECT alias FROM product WHERE id=9001")).scalar())
        assert "米白" in build_hotwords([
            {"id": 9001, "name": "针织衫", "short_name": "针织衫", "alias": alias}
        ])

    def test_approving_twice_is_harmless(self, repo):
        from smartmall_pipeline.clip.register import apply_alias, propose_aliases
        from sqlalchemy import text

        propose_aliases(repo, [Segment(0, 1, "qa", 9001, "t", "s",
                                       raw_text="x", matched_alias="米白")])
        with repo.engine.connect() as c:
            pid = c.execute(text("SELECT id FROM clip_alias_proposal")).scalar()
        assert apply_alias(repo, pid) is not None
        assert apply_alias(repo, pid) is None, "已处理的提案不该再生效"

    def test_segments_without_an_alias_are_not_queued(self, repo):
        from smartmall_pipeline.clip.register import propose_aliases

        segs = [Segment(0, 1, "qa", 9001, "t", "s", raw_text="x")]
        assert propose_aliases(repo, segs) == 0
