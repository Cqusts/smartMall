"""从商品属性生成商品图与宣传视频。

**图也会撒谎，而且比文字更难被发现。** 一张画着蓬松羊毛质感的图，配一件
涤纶的衣服，用户收到货的落差和文案写"精选羊毛"是一样的——但文案里的
"羊毛"两个字能被规则查出来，图里的质感查不出来。

所以这条链路的做法是：**把关口前移到提示词**。提示词是文本，能过和文案
同一套属性冲突检查；提示词里不出现属性表没有的材质，生成的图就不会朝
那个方向画。这不能保证图一定对（模型仍可能自己发挥），所以生成的素材
一律 pending，人工过一眼才能用。

链路：``读取商品 → 拼提示词 → 提示词合规 → 调模型 → 下载落地 → 写入待审``

**下载那一步不是优化。** 模型返回的 URL 24 小时后失效，只存 URL 的话
演示第二天就是一片裂图，而那时候免费额度可能也用完了，重新生成都做不到。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..nodes import Deps, run_node
from ..state import TraceRecord
from . import compliance, media

#: 用途 → 画面描述。**不写「高级感」「大片质感」这种词**：它们对模型是噪音，
#: 对商家是幻觉——生成出来不像的时候，没人说得清是哪个词没起作用。
USAGE_SCENES = {
    "white": "纯白背景电商主图，商品居中平铺，柔和顶光，无阴影干扰，"
             "商品占画面七成",
    "scene": "生活场景图，自然光，室内木质背景虚化，"
             "商品作为画面主体清晰可见",
    "detail": "特写细节图，展示面料纹理与走线，微距，自然光",
}

#: 负向提示词。前三条是电商图的通病，后两条是合规红线：
#: 画面里出现文字容易变成"广告语"，而那部分不受属性表约束、也过不了广告法检查
NEGATIVE = ("模糊, 低分辨率, 变形, 多余的手指, 扭曲的文字, 水印文字, "
            "商品标签, 价格标签, 促销文字, 人脸特写")


@dataclass
class MediaBrief:
    """一次素材生成的输入。"""

    product_id: int
    kind: str = "image"
    """image | video"""
    usage: str = "white"
    """图的用途，见 USAGE_SCENES。视频忽略这个字段。"""
    duration: int = 5

    def __post_init__(self) -> None:
        # 视频没有"用途"这一说，而默认值 white 会一路落进库里，
        # 于是列表页把一条视频显示成「白底主图」——实测就是这样
        if self.kind == "video":
            self.usage = ""


@dataclass
class MediaState:
    brief: MediaBrief
    product: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    flags: list[str] = field(default_factory=list)

    image: media.ImageResult | None = None
    task: media.VideoTask | None = None
    local_path: str = ""
    asset_id: int | None = None

    outcome: str = ""
    """generated 图已生成并落库 / queued 视频任务已排队 /
    prompt_only 只拼了提示词没调模型 / needs_human 合规没过 / skipped 这轮没跑成

    ``prompt_only`` 与 ``needs_human`` 分开是有意的：前者什么都没错，
    只是没接模型；混成一个的话，一次正常的试跑会在页面上显示成"要人处理"。
    """
    trace: TraceRecord = field(default_factory=TraceRecord)

    @property
    def attrs(self) -> dict[str, str]:
        return dict(self.product.get("attrs") or {})

    @property
    def product_name(self) -> str:
        return str(self.product.get("name") or "")


# ---------------------------------------------------------------- 节点


def load(state: MediaState, deps: Deps) -> MediaState:
    """读商品。读不到就什么都不做——凭商品名想象一件商品正是虚假宣传的定义。"""
    state.trace.intent = "marketing_media"
    state.trace.product_id = state.brief.product_id

    if deps.tools is None:
        state.outcome = "skipped"
        state.flags.append("没有商品数据通道")
        return state
    try:
        detail = deps.tools.get_product_detail(state.brief.product_id)
    except Exception as exc:  # noqa: BLE001
        # 查不到 ≠ 这个商品没有属性
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.outcome = "skipped"
        state.flags.append("商品数据查不到，本轮跳过")
        return state

    if not detail:
        state.outcome = "skipped"
        state.flags.append(f"商品 #{state.brief.product_id} 不存在")
        return state
    state.product = detail
    if not state.attrs:
        state.outcome = "needs_human"
        state.flags.append("商品属性表是空的，没有可依据的事实")
    return state


def build_prompt(state: MediaState, deps: Deps) -> MediaState:
    """拼提示词。**只用属性表里的事实。**

    不调模型来写提示词，是刻意的：让模型润色一遍，它会顺手加上"高级羊绒
    质感""匠心工艺"这类词——而那正是要防的编造，而且加完之后连是谁加的
    都说不清。这里用模板拼，每一个词都能追到属性表的某一行。
    """
    p = state.product
    bits = [state.product_name or "商品"]
    cat = p.get("category")
    if cat:
        bits.append(str(cat))

    # 只取能影响画面的属性。克重、产地这类画不出来，塞进去只是噪音
    visual_keys = ("材质", "面料", "颜色", "工艺", "版型", "厚度")
    for k, v in state.attrs.items():
        if any(vk in k for vk in visual_keys):
            bits.append(f"{k}{v}")

    scene = USAGE_SCENES.get(state.brief.usage, USAGE_SCENES["white"])
    if state.brief.kind == "video":
        state.prompt = (f"{'，'.join(bits)}。镜头缓慢环绕展示商品细节，"
                        f"自然光，画面干净，无文字")
    else:
        state.prompt = f"{'，'.join(bits)}。{scene}"
    return state


def check_prompt(state: MediaState, deps: Deps) -> MediaState:
    """提示词合规。**把关口前移到这里，因为生成后的图查不了。**

    文案里写"羊绒"能被规则揪出来；图里画出羊绒质感，规则一点办法都没有。
    所以在送进模型之前，先用与文案同一套属性冲突检查过一遍——
    提示词里不出现属性表没有的材质，生成的图就不会朝那个方向画。

    **这不保证图一定对**（模型仍可能自己发挥），所以素材一律 pending。
    """
    result = compliance.check(state.prompt, attrs=state.attrs,
                              product_name=state.product_name)
    state.flags.extend(result.flags)
    state.trace.postcheck_flags = list(result.flags)
    if not result.ok:
        state.outcome = "needs_human"
    return state


def generate(state: MediaState, deps: Deps) -> MediaState:
    """调模型。图是同步的，视频只创建任务。"""
    if deps.media is None:
        # **不是 needs_human。** 没接模型这件事本身没有任何"要人处理"的
        # 问题——提示词已经出来了，而它正是这条链路上唯一查得了的东西
        state.outcome = "prompt_only"
        return state

    t0 = time.time()
    try:
        if state.brief.kind == "video":
            state.task = deps.media.create_video_task(
                state.prompt, negative_prompt=NEGATIVE,
                duration=state.brief.duration)
        else:
            state.image = deps.media.generate_image(
                state.prompt, negative_prompt=NEGATIVE)
    except media.MediaError as exc:
        # 生成失败 ≠ 这个商品生成不出来。限流和超时下次就好了，
        # 标成"要人处理"等于把一个临时故障转成永久的人工任务
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.outcome = "skipped"
        state.flags.append(f"生成失败：{exc}")
        return state

    state.trace.tools_called.append({
        "name": "generate_" + state.brief.kind,
        "latency_ms": int((time.time() - t0) * 1000), "hit": True,
    })
    return state


#: 文件名白名单式生成：只保留字母数字与横线，其余一律换掉。
#: 商品名是用户可控的（商家自己填），直接拼进路径就是任意文件写入
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _filename(product_id: Any, kind: str, usage: str) -> str:
    """``9002-white-1755, 随机六位.png``

    **末尾那六位随机不是装饰**：只用秒级时间戳的话，同一秒内给同一个商品
    生成两张同用途的图会撞名，后一个文件把前一个覆盖掉——而库里两条记录
    都指向它，看起来像模型生成了两张一样的图。
    """
    ext = "mp4" if kind == "video" else "png"
    tag = _SAFE.sub("", usage) or _SAFE.sub("", kind) or "asset"
    pid = _SAFE.sub("", str(product_id)) or "0"
    return f"{pid}-{tag}-{int(time.time())}-{os.urandom(3).hex()}.{ext}"


#: 页面引用素材时的前缀。与 routers/media.py 的路由对齐——
#: 这里改了那边不改，页面上就是一片裂图
URL_PREFIX = "generated/"


def _fetch(url: str, asset_dir: Any, name: str) -> str:
    """下载并返回页面用的相对路径。失败抛 MediaError。"""
    path = Path(asset_dir) / name
    media.download(url, path)
    return URL_PREFIX + path.name


def store(state: MediaState, deps: Deps) -> MediaState:
    """下载落地 + 写入待审。

    视频还在跑的时候只落一条任务记录，等轮询到成功再补文件——
    否则商家点完按钮要盯着屏幕等五分钟。
    """
    if deps.asset_store is None:
        # 这一条**确实要人处理**：额度已经花掉了，而结果没有任何地方
        # 记着它，24 小时后连补下载都做不到
        state.outcome = "needs_human"
        state.flags.append("未接素材库，生成结果没有落库（24 小时后失效）")
        return state

    url, task_id, status = "", None, "succeeded"
    if state.brief.kind == "video":
        task_id = state.task.task_id if state.task else None
        status = state.task.status if state.task else "failed"
        url = state.task.url if state.task else ""
    elif state.image:
        url = state.image.url

    # 只有拿到 URL 才下载。视频刚创建时没有 URL，那是正常状态
    if url and deps.asset_dir:
        try:
            state.local_path = _fetch(
                url, deps.asset_dir,
                _filename(state.brief.product_id, state.brief.kind,
                          state.brief.usage))
        except media.MediaError as exc:
            # 下载失败要留痕但不丢记录：URL 还在，24 小时内可以补下载
            state.trace.error = f"{type(exc).__name__}: {exc}"
            state.flags.append(f"下载失败，24 小时内可补：{exc}")
    elif url:
        # 没配落地目录不是"下载失败"，是这条链路装配得不完整。
        # 不说出来的话，库里会静静躺着一批明天就打不开的 URL
        state.flags.append("未配置素材目录，只存了 24 小时后会失效的 URL")

    try:
        state.asset_id = deps.asset_store.stage_asset(
            product_id=state.brief.product_id, kind=state.brief.kind,
            usage_tag=state.brief.usage, local_path=state.local_path,
            source_url=url, prompt=state.prompt, negative_prompt=NEGATIVE,
            task_id=task_id, task_status=status,
            model=(state.task.model if state.task
                   else state.image.model if state.image else ""),
            error=state.trace.error or "",
        )
    except Exception as exc:  # noqa: BLE001
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.outcome = "skipped"
        state.flags.append(f"落库失败：{type(exc).__name__}")
        return state

    state.outcome = "queued" if status in ("pending", "running") else "generated"
    return state


# ---------------------------------------------------------------- 编排

NODE_LABELS = {
    "load": "读取商品",
    "prompt": "拼提示词",
    "check": "提示词合规",
    "generate": "调用生成模型",
    "store": "下载并写入待审",
}

_TERMINAL = ("needs_human", "skipped", "prompt_only")


def step_detail(name: str, state: MediaState) -> dict[str, Any]:
    if name == "load":
        return {"商品": state.product_name or "-", "属性条数": len(state.attrs)}
    if name == "prompt":
        return {"提示词": state.prompt[:48]}
    if name == "check":
        return {"合规": "、".join(state.flags) if state.flags else "通过"}
    if name == "generate":
        if state.task:
            return {"任务": state.task.task_id, "状态": state.task.status}
        return {"图": "已生成" if state.image else "无"}
    if name == "store":
        return {"素材": f"#{state.asset_id}" if state.asset_id else "未落库",
                "本地文件": state.local_path or "（还没有）"}
    return {}


def run_media(brief: MediaBrief, deps: Deps) -> MediaState:
    state = MediaState(brief=brief)

    def step(name, fn):
        nonlocal state
        state = run_node(name, fn, state, deps,
                         labels=NODE_LABELS, detail=step_detail)
        return state.outcome in _TERMINAL

    if step("load", load):
        return state
    step("prompt", build_prompt)
    if step("check", check_prompt):
        return state
    if step("generate", generate):
        return state
    step("store", store)
    return state


def safe_run_media(brief: MediaBrief, deps: Deps) -> MediaState:
    try:
        return run_media(brief, deps)
    except Exception as exc:  # noqa: BLE001
        state = MediaState(brief=brief, outcome="skipped")
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.flags.append(f"处理异常：{type(exc).__name__}")
        return state


# ---------------------------------------------------------------- 视频轮询


@dataclass
class PollReport:
    """一轮轮询的结果。"""

    checked: int = 0
    succeeded: int = 0
    failed: int = 0
    running: int = 0
    unknown: int = 0
    """查不到状态的（限流、超时、服务异常）。**单独一档**，见 poll_pending。"""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"checked": self.checked, "succeeded": self.succeeded,
                "failed": self.failed, "running": self.running,
                "unknown": self.unknown, "notes": list(self.notes)}


def poll_pending(deps: Deps, *, limit: int = 20) -> PollReport:
    """把还没跑完的视频任务查一遍，成了就下载落地。

    分成两步（创建 / 轮询）而不是一个函数里等到底，是因为生成要 1–5 分钟：
    挂在 HTTP 请求上就是超时，挂在页面上就是商家盯着转圈五分钟。

    **查不到状态不等于任务失败。** 限流、超时、服务异常都会让这次查询
    拿不到结果，而把它记成 failed 就是永久性地判了这条任务死刑——
    实际上下一次查可能就成了。所以这类只计数、不写库，任务留在
    pending/running 里等下一轮。
    """
    report = PollReport()
    if deps.media is None or deps.asset_store is None:
        report.notes.append("未接生成模型或素材库，没什么可轮询的")
        return report

    for row in deps.asset_store.unfinished(limit):
        if str(row.get("kind")) != "video":
            continue
        report.checked += 1
        task = media.VideoTask(
            task_id=str(row.get("task_id") or ""),
            model=str(row.get("model") or ""),
            prompt=str(row.get("prompt") or ""),
            status=str(row.get("task_status") or "pending"),
        )
        try:
            task = deps.media.poll_video(task)
        except media.MediaError as exc:
            report.unknown += 1
            report.notes.append(f"#{row.get('id')} 查询失败（不改状态）：{exc}")
            continue

        if task.status != "succeeded":
            deps.asset_store.finish(row["id"], task_status=task.status,
                                    error=task.error)
            if task.status == "failed":
                report.failed += 1
            else:
                report.running += 1
            continue

        local_path, err = "", ""
        if deps.asset_dir and task.url:
            try:
                local_path = _fetch(
                    task.url, deps.asset_dir,
                    _filename(row.get("product_id"), "video",
                              str(row.get("usage_tag") or "")))
            except media.MediaError as exc:
                err = f"下载失败（源 URL 24 小时内有效）：{exc}"
        elif task.url:
            err = "未配置素材目录，只存了 24 小时后会失效的 URL"

        # 下载失败也照记 succeeded：**任务确实成功了**，失败的是下载这一步。
        # 记成 failed 会让人去改提示词，而该做的是重下一次
        deps.asset_store.finish(row["id"], task_status="succeeded",
                                local_path=local_path, source_url=task.url,
                                error=err)
        report.succeeded += 1
        if err:
            report.notes.append(f"#{row.get('id')} {err}")
    return report
