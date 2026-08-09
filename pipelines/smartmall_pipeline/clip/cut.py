"""ffmpeg 切片：把片段的时间戳变成一个个可播放的文件。

**默认不重编码**（``-c copy``）。代价是切点被对齐到最近的关键帧。
实测（640×360、关键帧间隔 2 秒的素材，切一段 9.4 秒）::

    precise=False   实际 11.1s   0.2s   183KB   ← 差 1.7 秒
    precise=True    实际  9.4s   0.8s   148KB

慢四倍还只是这个尺寸；一场两小时的直播切几十段，重编码是几十分钟起，
而且每切一次画质掉一档。对"这段在讲针织衫"这种用途偏一两秒无所谓，
所以默认走 copy。真要卡帧再开 ``precise=True``。

**每段都留前后余量。** 语音识别给的时间戳是"这句话的声音"的起止，
按它硬切出来的片段，开头第一个字会被削掉半个音节，结尾也戛然而止。
人看着会觉得"没剪好"，而这个感受和内容质量无关。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .segment import Segment


class FfmpegMissing(RuntimeError):
    """没装 ffmpeg。这条路径没有降级方案——切片就是它干的活。"""


#: 片段前后各留的余量。0.4 秒足够让第一个字完整发出来，
#: 又不会把上一句的尾巴带进来
PAD_MS = 400

#: 太短的片段没有观看价值，也几乎不可能包含完整的一次讲解
MIN_CLIP_MS = 3000

_SAFE = re.compile(r"[^\w一-鿿-]+")


def ffmpeg_path() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FfmpegMissing(
            "找不到 ffmpeg。\n"
            "  · Windows：winget install ffmpeg，或从 gyan.dev 下载后加进 PATH\n"
            "  · macOS：brew install ffmpeg\n"
            "  · Linux：apt install ffmpeg"
        )
    return exe


def probe_duration_ms(path: str | Path) -> int:
    """读视频总时长。用来把超出结尾的切点收回来——

    ASR 的最后一句时间戳偶尔会超过视频实际长度（VAD 补的尾巴），
    照着切 ffmpeg 会产出一个 0 字节的文件，而它看起来和成功没区别。
    """
    exe = shutil.which("ffprobe")
    if not exe:
        return 0
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        return int(float(json.loads(out)["format"]["duration"]) * 1000)
    except Exception:  # noqa: BLE001  探测失败就当没有上限
        return 0


@dataclass
class ClipFile:
    segment: Segment
    path: Path
    begin_ms: int
    end_ms: int
    bytes: int = 0

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.begin_ms


@dataclass
class CutResult:
    clips: list[ClipFile] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def cut_segments(
    video: str | Path, segments: Sequence[Segment], out_dir: str | Path,
    *, pad_ms: int = PAD_MS, min_ms: int = MIN_CLIP_MS, precise: bool = False,
    timeout: float = 300.0,
) -> CutResult:
    """把片段切成文件。

    只切**有用**的片段（``Segment.is_useful``）——催单话术切出来没人看，
    而每切一段都要读写一遍磁盘。
    """
    exe = ffmpeg_path()
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"找不到视频：{video}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = probe_duration_ms(video)
    result = CutResult()

    for i, seg in enumerate(segments, 1):
        if not seg.is_useful:
            result._skip("非讲解片段")
            continue

        begin = max(0, seg.begin_ms - pad_ms)
        end = seg.end_ms + pad_ms
        if total:
            end = min(end, total)
        if end - begin < min_ms:
            result._skip("片段过短")
            continue

        name = _SAFE.sub("_", seg.topic or f"seg{i}")[:40] or f"seg{i}"
        out = out_dir / f"{i:03d}_{seg.product_id or 'na'}_{name}.mp4"

        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"]
        if precise:
            # 精确模式：先解码到起点再切，慢但帧准
            cmd += ["-i", str(video), "-ss", f"{begin / 1000:.3f}",
                    "-to", f"{end / 1000:.3f}",
                    "-c:v", "libx264", "-c:a", "aac", "-preset", "veryfast"]
        else:
            # -ss 放在 -i 之前才是快速定位（直接跳到关键帧），
            # 放在后面会从头解码，一场两小时的直播能跑到天荒地老
            cmd += ["-ss", f"{begin / 1000:.3f}", "-i", str(video),
                    "-t", f"{(end - begin) / 1000:.3f}", "-c", "copy",
                    "-avoid_negative_ts", "make_zero"]
        cmd.append(str(out))

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            result.errors.append(f"{out.name}：ffmpeg 超时")
            continue

        if proc.returncode != 0:
            result.errors.append(f"{out.name}：{proc.stderr.strip()[:160]}")
            continue
        # **返回码 0 不代表切出了东西。** -c copy 在起点落在关键帧之后时
        # 会产出一个 0 字节的文件，而 ffmpeg 一声不吭
        size = out.stat().st_size if out.exists() else 0
        if size == 0:
            out.unlink(missing_ok=True)
            result._skip("产出空文件")
            continue

        result.clips.append(ClipFile(segment=seg, path=out, begin_ms=begin,
                                     end_ms=end, bytes=size))
    return result
