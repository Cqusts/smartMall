"""图与视频生成：百炼（DashScope）客户端。

**两个模型的形状完全不同，不能套同一个封装。**

    qwen-image-2.0-pro          同步，秒级返回，结果在 choices 里
    wan2.7-t2v-2026-06-12       异步，X-DashScope-Async 头 → task_id → 轮询

硬把它们抹平成一个 ``generate(prompt) -> url`` 的接口，代价是视频那条路要在
函数里阻塞轮询 1–5 分钟——挂在 HTTP 请求上就是超时，挂在页面上就是转圈。
所以视频这条路径**返回 task_id 而不是结果**，什么时候取由调用方决定。

---

**生成的 URL 只有 24 小时有效期。** 万相文生视频的文档明写，图片同理。
只存 URL 的话，今天演示完、明天打开就是一片裂图。所以拿到 URL 后要
立刻下载到本地（见 :func:`download`），库里存本地路径。

---

错误分类沿用整个项目那条判据：**业务结论只能来自 200 响应。**
限流、超时、5xx 都不是"生成失败"，是"这次没跑成"——前者该让人去改提示词，
后者该重试。混为一谈的结果是运营对着一张能生成的图反复改词。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

#: 默认端点。百炼有两种形态：
#:
#: * 通用域名 ``https://dashscope.aliyuncs.com``
#: * 工作空间域名 ``https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com``
#:
#: 默认用通用域名；账号只开了工作空间域名的话，改环境变量
#: ``DASHSCOPE_BASE_URL`` 即可，**不要改代码**——两个环境用不同的常量，
#: 迟早有人把自己的那个提交上去。
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"

IMAGE_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
VIDEO_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
TASK_PATH = "/api/v1/tasks/"


class MediaError(RuntimeError):
    """生成失败的基类。"""


class MediaUnavailableError(MediaError):
    """连不上、超时、限流、5xx —— **重试可能成功**。

    与"模型拒绝生成"是两回事：后者要改提示词，前者只要等一会儿。
    报成同一种错的话，运营会对着一张本来能生成的图反复改词。
    """


class MediaConfigError(MediaError):
    """4xx —— 请求本身有问题（没有 key、模型名不对、参数非法）。
    重试无用，对每次调用都会同样失败。"""


@dataclass
class ImageResult:
    url: str
    model: str
    prompt: str
    negative_prompt: str = ""


@dataclass
class VideoTask:
    """视频是异步的，创建时只拿得到这个。"""

    task_id: str
    model: str
    prompt: str
    negative_prompt: str = ""
    status: str = "pending"
    url: str = ""
    error: str = ""

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed")


def _raise_for(resp: Any, what: str) -> None:
    """按状态码分类，而不是按响应体里的字眼。

    响应体的措辞会变（不同模型、不同版本都不一样），状态码不会。
    """
    code = resp.status_code
    if code == 200:
        return
    body = (resp.text or "")[:300]
    if code in (401, 403):
        raise MediaConfigError(f"{what} 鉴权失败（{code}）：检查 DASHSCOPE_API_KEY。{body}")
    if code == 429:
        raise MediaUnavailableError(f"{what} 被限流（429），稍后重试。{body}")
    if 400 <= code < 500:
        raise MediaConfigError(f"{what} 请求不合法（{code}）：{body}")
    raise MediaUnavailableError(f"{what} 服务异常（{code}）：{body}")


@dataclass
class DashScopeMediaClient:
    """百炼的图与视频生成。

    只用 httpx，不引官方 SDK：这个项目其余的模型调用都是裸 HTTP
    （见 llm.py），多一个 SDK 就多一套鉴权、重试、错误类型要对齐。
    """

    api_key: str = ""
    base_url: str = ""
    image_model: str = "qwen-image-2.0-pro"
    video_model: str = "wan2.7-t2v-2026-06-12"
    timeout: float = 120.0

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self.base_url = (self.base_url
                         or os.environ.get("DASHSCOPE_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        if not self.api_key:
            raise MediaConfigError(
                "缺少 DASHSCOPE_API_KEY。到 https://bailian.console.aliyun.com/ "
                "申请，填进 deploy/.env")

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json", **extra}

    # ------------------------------------------------------------ 图

    def generate_image(self, prompt: str, *, negative_prompt: str = "",
                       size: str = "1024*1024") -> ImageResult:
        """文生图。**同步**，秒级返回。

        ``watermark=True`` 不是可选项：《人工智能生成合成内容标识办法》要求
        生成内容可识别，显式水印是最直接的一种。库里的 ``ai_generated``
        字段是隐式标识，两者都要有。
        """
        import httpx

        body = {
            "model": self.image_model,
            "input": {"messages": [
                {"role": "user", "content": [{"text": prompt}]}
            ]},
            "parameters": {
                "negative_prompt": negative_prompt,
                "size": size,
                "n": 1,
                # 显式标识。**不做成参数**——能关掉就意味着某天会被关掉
                "watermark": True,
                "prompt_extend": True,
            },
        }
        try:
            resp = httpx.post(self.base_url + IMAGE_PATH, json=body,
                              headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise MediaUnavailableError(f"文生图请求失败：{type(exc).__name__}") from exc

        _raise_for(resp, "文生图")
        data = resp.json()
        url = _dig(data, "output", "choices", 0, "message", "content", 0, "image")
        if not url:
            # 200 但没有图：这仍然是"没生成出来"，不是基础设施故障。
            # 摘一段响应体出来——不然只能看到一句"没有图"，无从查起
            raise MediaError(f"文生图返回 200 但没有图片：{str(data)[:300]}")
        return ImageResult(url=url, model=self.image_model, prompt=prompt,
                           negative_prompt=negative_prompt)

    # ------------------------------------------------------------ 视频

    def create_video_task(self, prompt: str, *, negative_prompt: str = "",
                          resolution: str = "720P", ratio: str = "16:9",
                          duration: int = 5) -> VideoTask:
        """文生视频。**异步**，只创建任务。

        生成要 1–5 分钟。挂在 HTTP 请求上等于超时，所以这里返回 task_id，
        什么时候去取由调用方决定（商家页轮询 / CLI 阻塞等待都行）。
        """
        import httpx

        body = {
            "model": self.video_model,
            "input": {"prompt": prompt, "negative_prompt": negative_prompt},
            "parameters": {
                "resolution": resolution, "ratio": ratio,
                "duration": duration, "watermark": True, "prompt_extend": True,
            },
        }
        try:
            resp = httpx.post(
                self.base_url + VIDEO_PATH, json=body,
                # **这个头不加会直接报"不支持同步调用"**，而报错信息看起来
                # 像是账号权限问题，很容易查错方向
                headers=self._headers(**{"X-DashScope-Async": "enable"}),
                timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise MediaUnavailableError(f"创建视频任务失败：{type(exc).__name__}") from exc

        _raise_for(resp, "文生视频")
        data = resp.json()
        task_id = _dig(data, "output", "task_id")
        if not task_id:
            raise MediaError(f"创建视频任务返回 200 但没有 task_id：{str(data)[:300]}")
        return VideoTask(task_id=task_id, model=self.video_model, prompt=prompt,
                         negative_prompt=negative_prompt,
                         status=_norm_status(_dig(data, "output", "task_status")))

    def poll_video(self, task: VideoTask) -> VideoTask:
        """查一次任务状态。**不阻塞、不循环**——循环由调用方控制。

        任务与视频 URL 的有效期都是 24 小时；超过之后状态变成 UNKNOWN，
        这里归一成 failed 并说明原因，而不是让它永远 pending。
        """
        import httpx

        try:
            resp = httpx.get(self.base_url + TASK_PATH + task.task_id,
                             headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise MediaUnavailableError(f"查询视频任务失败：{type(exc).__name__}") from exc

        _raise_for(resp, "查询视频任务")
        data = resp.json()
        raw = str(_dig(data, "output", "task_status") or "")
        task.status = _norm_status(raw)
        if task.status == "succeeded":
            task.url = _dig(data, "output", "video_url") or ""
            if not task.url:
                task.status = "failed"
                task.error = f"任务成功但没有 video_url：{str(data)[:200]}"
        elif task.status == "failed":
            task.error = (_dig(data, "output", "message")
                          or ("任务已过期（task_id 与视频 URL 的有效期都是 24 小时）"
                              if raw.upper() == "UNKNOWN" else str(data)[:200]))
        return task


#: 百炼的状态是大写的，本项目内部一律小写。
#: ``UNKNOWN`` 归到 failed：任务不存在或已过期，等下去不会变好。
_STATUS = {"PENDING": "pending", "RUNNING": "running", "SUCCEEDED": "succeeded",
           "FAILED": "failed", "CANCELED": "failed", "UNKNOWN": "failed"}


def _norm_status(raw: Any) -> str:
    return _STATUS.get(str(raw or "").upper(), "pending")


def _dig(data: Any, *path: Any) -> Any:
    """按路径取值，中途取不到就返回 None。

    响应结构是外部契约，字段缺失是**要处理的情况**而不是异常——
    直接下标会抛 KeyError/IndexError，那种堆栈完全看不出是响应变了。
    """
    cur = data
    for key in path:
        if isinstance(key, int):
            if not isinstance(cur, list) or len(cur) <= key:
                return None
            cur = cur[key]
        else:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
    return cur


def download(url: str, dest: Any, *, timeout: float = 120.0) -> int:
    """把生成结果落到本地，返回字节数。

    **这一步不是优化，是必须的**：模型返回的 URL 24 小时后失效，
    只存 URL 的话演示第二天就全是裂图，而那时候免费额度可能也用完了，
    重新生成一遍都做不到。
    """
    import httpx
    from pathlib import Path

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream("GET", url, timeout=timeout,
                          follow_redirects=True) as resp:
            _raise_for(resp, "下载素材")
            total = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
                    total += len(chunk)
    except httpx.HTTPError as exc:
        raise MediaUnavailableError(f"下载素材失败：{type(exc).__name__}") from exc
    if total == 0:
        dest.unlink(missing_ok=True)
        raise MediaError("下载到的是空文件")
    return total


@dataclass
class FakeMediaClient:
    """测试与试跑用的替身。**不调 API、不花额度。**"""

    image_model: str = "fake-image"
    video_model: str = "fake-video"
    fail: str = ""
    """注入故障：``image`` / ``video`` / ``poll``。"""
    calls: list[tuple[str, str]] = field(default_factory=list)
    poll_count: int = 0
    succeed_after: int = 2
    """轮询几次之后算成功——用来测"还在跑"那条路径。"""

    def generate_image(self, prompt, *, negative_prompt="", size="1024*1024"):
        self.calls.append(("image", prompt))
        if self.fail == "image":
            raise MediaUnavailableError("注入的故障")
        return ImageResult(url=f"https://example.invalid/{abs(hash(prompt))}.png",
                           model=self.image_model, prompt=prompt,
                           negative_prompt=negative_prompt)

    def create_video_task(self, prompt, *, negative_prompt="", **kw):
        self.calls.append(("video", prompt))
        if self.fail == "video":
            raise MediaUnavailableError("注入的故障")
        return VideoTask(task_id="fake-task-1", model=self.video_model,
                         prompt=prompt, negative_prompt=negative_prompt)

    def poll_video(self, task: VideoTask) -> VideoTask:
        self.calls.append(("poll", task.task_id))
        if self.fail == "poll":
            raise MediaUnavailableError("注入的故障")
        self.poll_count += 1
        if self.poll_count >= self.succeed_after:
            task.status = "succeeded"
            task.url = "https://example.invalid/fake.mp4"
        else:
            task.status = "running"
        return task
