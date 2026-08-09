"""语音转写。默认走百炼的录音文件识别，可换本地 FunASR。

选型（2026-08 复核过）：

============  ==========  =================  =========================
项目          许可        形态               结论
============  ==========  =================  =========================
FunASR        MIT         **库**             ✅ 本地后端建在它上面
FunClip       MIT         Gradio 应用        借鉴思路，不集成
HotClip       AGPL-3.0    Electron 桌面      ❌ 不能 vendor
百炼录音识别  云 API      服务               ✅ 默认后端
============  ==========  =================  =========================

**为什么不 vendor 现成的切片工具。**

HotClip 的多信号高光检测（LLM + 响度峰值 + 转场 + 人脸 + 弹幕密度）思路
很好，但它是 **AGPL-3.0**：链进来，整个 smartMall 就得按 AGPL 开源。
对一个作品集项目这未必是问题，但它会限制往后任何商业化的说法，
而收益只是省掉一段本来就该自己写的编排代码。思路可以学，代码不进来。

FunClip 是 MIT，可以用，但它是个 Gradio 应用不是库——把一个应用塞进
服务里，等于把它的 UI 状态、进程模型、依赖全吃进来。它下面那层 FunASR
才是库，直接用那一层。

**为什么默认走云 API。** 百炼的录音文件识别给的是句级 + 字级双时间戳、
说话人分离、以及**自定义热词**——三样这条链路全都要，而且不占显存。
本地 FunASR 走 CPU 也能跑（SenseVoice-Small 约 120M 参数），
作为无网络时的退路，接口一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from ..gates.gate3_model import LlmConfigError, LlmError, LlmUnavailableError
from .transcript import Transcript


@runtime_checkable
class AsrClient(Protocol):
    def transcribe(
        self, audio_url: str, *, hotwords: Sequence[str] = (), speakers: bool = True
    ) -> Transcript:
        ...


@dataclass
class DashScopeAsrClient:
    """百炼录音文件识别。**异步接口**：提交任务 → 轮询 → 取结果。

    音频要有公网可达的 URL——这是这条路唯一的硬约束。本地文件得先传
    到 OSS 之类的地方。演示时可以用任意公开的音视频链接，
    或者退回 :class:`LocalFunAsrClient` 直接读本地文件。
    """

    api_key: str = ""
    model: str = "paraformer-v2"
    base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    poll_interval: float = 5.0
    timeout: float = 1800.0
    """一场两小时的直播转写要几分钟。默认给到半小时，
    比"超时了但其实快好了"强。"""

    def __post_init__(self) -> None:
        import os

        self.api_key = (self.api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        if not self.api_key:
            raise LlmConfigError(
                "缺少 DASHSCOPE_API_KEY。\n"
                "  · 到 https://bailian.console.aliyun.com/ 申请\n"
                "  · 或用 --fake-asr 跑通链路（不调真实服务、不产生费用）"
            )

    def _post(self, path: str, body: dict[str, Any], *, async_task: bool) -> dict:
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        if async_task:
            headers["X-DashScope-Async"] = "enable"
        try:
            resp = httpx.post(f"{self.base_url}{path}", headers=headers,
                              json=body, timeout=60.0)
        except Exception as exc:  # noqa: BLE001
            raise LlmUnavailableError(f"无法连接转写服务：{type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise LlmUnavailableError(f"转写服务返回 {resp.status_code}")
        if resp.status_code != 200:
            raise LlmConfigError(f"转写调用失败 HTTP {resp.status_code}: "
                                 f"{resp.text[:300]}")
        return resp.json()

    def transcribe(self, audio_url, *, hotwords=(), speakers=True) -> Transcript:
        import httpx

        params: dict[str, Any] = {}
        if speakers:
            params["diarization_enabled"] = True
        if hotwords:
            # 热词是这条链路准确率的主要来源。上限各家不同，
            # 截断而不是整批放弃——少几个词好过一个都不生效
            params["vocabulary"] = list(hotwords)[:100]

        task = self._post(
            "/services/audio/asr/transcription",
            {"model": self.model,
             "input": {"file_urls": [audio_url]},
             "parameters": params},
            async_task=True,
        )
        task_id = (task.get("output") or {}).get("task_id")
        if not task_id:
            raise LlmConfigError(f"转写服务未返回 task_id：{str(task)[:300]}")

        deadline = time.time() + self.timeout
        while True:
            if time.time() > deadline:
                raise LlmUnavailableError(f"转写超时（{self.timeout:.0f}s），"
                                          f"task_id={task_id}")
            time.sleep(self.poll_interval)
            r = httpx.get(f"{self.base_url}/tasks/{task_id}",
                          headers={"Authorization": f"Bearer {self.api_key}"},
                          timeout=30.0)
            out = (r.json().get("output") or {})
            status = out.get("task_status")
            if status == "SUCCEEDED":
                return self._collect(out, audio_url)
            if status in ("FAILED", "CANCELED"):
                raise LlmConfigError(f"转写失败：{str(out)[:300]}")

    def _collect(self, output: dict, source: str) -> Transcript:
        """结果是一个 JSON 文件的 URL，不是内联的。"""
        import httpx

        rows: list[dict] = []
        for item in output.get("results") or []:
            url = item.get("transcription_url")
            if not url:
                continue
            doc = httpx.get(url, timeout=60.0).json()
            for tr in doc.get("transcripts") or []:
                rows.extend(tr.get("sentences") or [])
        return Transcript.from_rows(rows, source=source)


#: 支持句级时间戳与热词的模型。**这条链路两样都必须有**：
#: 没有时间戳切不了片，没有热词商品名全是错字。
_TIMESTAMP_MODELS = ("paraformer", "seaco")


@dataclass
class LocalFunAsrClient:
    """本地 FunASR。读本地文件，不需要公网 URL，CPU 也能跑。

    **默认必须是 Paraformer 系，不能用 SenseVoice。** 第一版默认选了
    SenseVoiceSmall——它更小更快、还能识别情绪，看起来是个好选择，
    实测下来两样致命：

    * **它不产可靠的句级时间戳。** FunASR 会打一行
      ``punctuation timestamps could not be aligned, falling back to VAD
      segments``，然后把 40 秒音频返回成**一整句**。时间戳是这条链路的
      全部意义——没有它，分段和切片都无从谈起。
    * **热词只有 Paraformer 支持。** ``hotword`` 参数传给 SenseVoice
      不报错，只是不生效——于是那套"商品名→热词→转写更准"的闭环
      一直在空转，而且没有任何迹象。

    SeACo-Paraformer（``paraformer-zh``）两样都有，代价是模型大一些、
    慢一些。这条路上跑得准比跑得动重要——跑得动但结果没法用，
    等于没跑。
    """

    model: str = "paraformer-zh"
    device: str = "cpu"
    _pipe: Any = field(default=None, repr=False)

    @property
    def supports_hotwords(self) -> bool:
        return any(k in self.model.lower() for k in _TIMESTAMP_MODELS)

    def _load(self):
        if self._pipe is None:
            try:
                from funasr import AutoModel
            except ImportError as exc:  # pragma: no cover
                raise LlmConfigError(
                    "本地转写需要 funasr：pip install funasr\n"
                    "  首次运行会下载模型（约 1GB）。"
                    "不想装的话用百炼的录音文件识别。"
                ) from exc
            self._pipe = AutoModel(model=self.model, device=self.device,
                                   vad_model="fsmn-vad", punc_model="ct-punc")
        return self._pipe

    def transcribe(self, audio_url, *, hotwords=(), speakers=True) -> Transcript:
        pipe = self._load()

        kwargs: dict[str, Any] = {"input": audio_url, "sentence_timestamp": True}
        if hotwords:
            if self.supports_hotwords:
                kwargs["hotword"] = " ".join(hotwords)
            else:
                # 静默失效是最糟的一种：参数传进去不报错，只是不生效，
                # 而"商品名全是错字"看起来像模型不行，不像配置错了
                print(f"  ⚠ {self.model} 不支持热词，{len(hotwords)} 个热词不会生效。"
                      f"\n    换 --asr-model paraformer-zh 才有热词与句级时间戳。")

        try:
            res = pipe.generate(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise LlmUnavailableError(f"本地转写失败：{type(exc).__name__}: {exc}") from exc

        first = res[0] if isinstance(res, list) and res else {}
        rows = first.get("sentence_info") or []
        transcript = Transcript.from_rows(rows, source=str(audio_url))

        # 整段音频只回来一两句 = 时间戳没对上，模型退化成了 VAD 整段。
        # 不检查的话下游会安静地把整场直播当成一个片段，
        # 而"分段效果差"看起来像提示词问题，不像转写问题
        if transcript.duration_ms > 30_000 and len(transcript.sentences) <= 2:
            print(f"  ⚠ {transcript.duration_ms / 1000:.0f} 秒音频只转出 "
                  f"{len(transcript.sentences)} 句，时间戳多半没对上。"
                  f"\n    {self.model} 若不是 paraformer 系，换 "
                  f"--asr-model paraformer-zh。")
        return transcript


@dataclass
class FakeAsrClient:
    """确定性替身。真实转写既慢又要钱，链路测试用它。"""

    transcript: Transcript | None = None
    raise_on: str | None = None
    seen_hotwords: list[str] = field(default_factory=list)

    def transcribe(self, audio_url, *, hotwords=(), speakers=True) -> Transcript:
        self.seen_hotwords = list(hotwords)
        if self.raise_on and self.raise_on in str(audio_url):
            raise LlmUnavailableError("注入的故障")
        if self.transcript is not None:
            return self.transcript
        from .transcript import Sentence

        return Transcript(source=str(audio_url), sentences=[
            Sentence("家人们看一下这件针织衫，米白色的。", 0, 4000, "主播"),
            Sentence("这个是百分百羊毛的，克重320克，特别厚实。", 4000, 9000, "主播"),
            Sentence("很多人问会不会起球，我们做了抗起球处理。", 9000, 14000, "主播"),
            Sentence("三二一上链接！", 14000, 16000, "助播"),
            Sentence("下面看这双马丁靴，头层牛皮的。", 16000, 21000, "主播"),
            Sentence("脚背高的姐妹建议大一码。", 21000, 25000, "主播"),
        ])
