"""商品图生知识：VLM 看图 → 结构化描述 → knowledge_item。

这是方案里「多模态素材 → 数据中台 → RAG」那条入口的第一次落地。整条体系
收敛到 ``knowledge_item`` 一个抽象，图片这条路也不例外——VLM 的描述转成
文本进同一套索引，命中后把 ``asset_ids`` 对应的图挂回答案上。
不做端到端多模态检索（双塔召回不稳、单卡预算下调不动），这是有意的取舍。

**这一层最大的风险是 VLM 会把看不出来的东西也说出来。**

一张图判断不了羊毛还是腈纶、判断不了克重、判断不了含绒量——但你问它，
它会给一个答案，而且语气和真知道一样。这和寒暄分支编造公司注册地是同一种
病，只是更隐蔽：编出来的"100%羊毛"混在一堆正确的颜色款式描述里，
人工审的时候很难逐条核。

所以这里的分工是死的：

* **VLM 只报"我看见了什么"** —— 颜色、版型、领型、袖长、图案、可见细节。
  提示词里明确列出禁止字段，输出后再用规则复查一遍（模型不总听话）。
* **"是什么"来自数据库** —— 材质、克重、成分从 ``product_attr`` 取，
  那是运营填的权威值，不是猜的。

两路合起来才是一条知识。而且这个结构自带一次交叉验证：VLM 说的颜色
必须能在 SKU 里找到，对不上就送人工——那通常意味着图配错了商品，
而配错图这件事没人会主动发现。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .gates.gate3_model import (
    LlmConfigError,
    LlmError,
    LlmUnavailableError,
    parse_json_lenient,
)
from .models import (
    BizType,
    GateStats,
    KnowledgeItem,
    KnowledgeType,
    Modality,
    ReviewStatus,
)

# ---------------------------------------------------------------- 客户端


@runtime_checkable
class VisionClient(Protocol):
    """看图模型调用协议。测试用 :class:`FakeVisionClient`。"""

    def describe(
        self, *, model: str, system: str, user: str, image: bytes, mime: str
    ) -> dict[str, Any]:
        ...


_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".png": "image/png", ".webp": "image/webp"}


def read_image(path: str | Path) -> tuple[bytes, str]:
    p = Path(path)
    mime = _MIME.get(p.suffix.lower())
    if mime is None:
        raise ValueError(f"不支持的图片格式：{p.suffix}（支持 {sorted(_MIME)}）")
    return p.read_bytes(), mime


@dataclass
class DashScopeVisionClient:
    """DashScope 的 OpenAI 兼容多模态接口。

    图片用 base64 data URL 内联，不传 URL：本地图片没有公网地址，
    而为了跑个打标临时起一个图床是本末倒置。代价是请求体大一些，
    600×600 的 JPEG 约 50KB，base64 后 70KB 左右，可以接受。
    """

    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode"
    timeout: float = 90.0
    """比文本调用长：看图本来就慢，60 秒在图稍大时会卡在超时上。"""

    MODEL_ALIASES = {"vision": "qwen-vl-max", "vision-light": "qwen-vl-plus"}

    def __post_init__(self) -> None:
        import os

        self.api_key = (self.api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        self.base_url = os.environ.get("DASHSCOPE_BASE_URL", self.base_url).rstrip("/")
        if not self.api_key:
            raise LlmError(
                "缺少 DASHSCOPE_API_KEY。\n"
                "  · 到 https://bailian.console.aliyun.com/ 申请，填进 deploy/.env\n"
                "  · 或用 --fake-vlm 跑通链路（不调真实模型、不产生费用）"
            )
        if not self.api_key.isascii():
            # h11 会在写 header 时抛 LocalProtocolError，报错完全看不出是 key 的问题
            raise LlmError("DASHSCOPE_API_KEY 含非 ASCII 字符，多半是复制时带进了全角字符")

    @property
    def chat_url(self) -> str:
        if re.search(r"/v\d+$", self.base_url):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def describe(
        self, *, model: str, system: str, user: str, image: bytes, mime: str
    ) -> dict[str, Any]:
        import httpx

        data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        body = {
            "model": self.MODEL_ALIASES.get(model, model),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": user},
                ]},
            ],
            "temperature": 0,
        }
        try:
            resp = httpx.post(
                self.chat_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body, timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise LlmUnavailableError(
                f"无法连接模型服务 {self.base_url}：{type(exc).__name__}: {exc}"
            ) from exc

        # 与文本侧同一套判断：5xx/429 可重试，4xx 是请求本身有问题
        if resp.status_code >= 500 or resp.status_code == 429:
            raise LlmUnavailableError(f"模型服务返回 {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise LlmConfigError(f"模型调用失败 HTTP {resp.status_code}: {resp.text[:400]}")

        return parse_json_lenient(resp.json()["choices"][0]["message"]["content"])


@dataclass
class FakeVisionClient:
    """确定性替身。真实 VLM 的输出不可预测，用它做断言等于测随机数。"""

    payload: dict[str, Any] | None = None
    raise_on: str | None = None
    calls: list[str] = field(default_factory=list)

    def describe(self, *, model, system, user, image, mime):
        self.calls.append(model)
        if self.raise_on and self.raise_on in user:
            raise LlmUnavailableError("注入的故障")
        return self.payload if self.payload is not None else {
            "colors": ["米白"],
            "pattern": "纯色",
            "silhouette": "宽松直筒",
            "neckline": "圆领",
            "sleeve": "长袖",
            "details": ["罗纹袖口", "落肩"],
            "scenes": ["通勤", "日常"],
            "description": "米白色圆领长袖针织衫，版型宽松，袖口与下摆有罗纹收边。",
            "confidence": 0.85,
        }


# ---------------------------------------------------------------- 提示词

ASSET_SYSTEM = """你是电商商品图的标注员。只输出 json，不要解释。

**你的职责边界是"看见"，不是"知道"。** 照片能告诉你颜色、版型、图案、
可见的细节；照片不能告诉你面料成分、克重、含绒量、尺寸厘米数、洗涤方式、
产地、价格。后面这些店铺自己有权威数据，不需要你猜，猜错了会变成客服
对用户说的话。"""

#: 成分词。**照片看不出纤维是什么**——针织的手感可能是羊毛也可能是腈纶，
#: 而两者差价好几倍。这些词的权威值在 product_attr 里。
#:
#: 注意这里划的线是**成分**而不是"任何与布料有关的词"：针织、牛仔、灯芯绒、
#: 蕾丝、绗缝这类是**织法与质感**，肉眼分得出来，属于"看见"的范畴，不拦。
_FIBER = (
    "材质", "面料", "成分", "纤维", "羊毛", "羊绒", "棉质", "纯棉", "涤纶",
    "腈纶", "锦纶", "氨纶", "真丝", "丝绸", "亚麻", "棉麻", "羽绒", "鸭绒",
    "鹅绒", "皮革", "牛皮", "羊皮", "莱赛尔", "醋酸", "粘纤", "聚酯",
)

#: 参数词。克重、含绒量、密度都要称要测，看不出来。
_SPEC = ("克重", "含绒量", "充绒量", "密度", "支数", "定重")

#: 保养词。"建议手洗"是厂商标注，不是照片里的信息。
#: 不拦"水洗"——"复古水洗夹克"里那是做旧工艺，看得见。
_CARE = ("手洗", "机洗", "干洗", "洗涤", "水温", "漂白", "熨烫", "烘干", "缩水")

#: 其他猜不出来的
_MISC = ("产地", "价格", "售价", "正品", "进口", "厘米", "cm")

#: 禁止 VLM 输出的内容。**光在提示词里写不够**——模型不总听话，
#: 输出后还要用规则复查一遍（见 :func:`unobservable_claims`）。
FORBIDDEN_TOPICS = _FIBER + _SPEC + _CARE + _MISC

ASSET_USER = """标注这张商品图。

已知信息（**不要重复这些，也不要推翻它们**）：
商品名：{name}
类目：{category}

**只描述这一件商品本身。** 图里可能还有模特身上的其他衣服、背景、道具、
搭配单品——那些都不是要标注的对象。实测里模特黑色夹克内搭一件红色卫衣，
模型把"红色"报成了商品颜色。

只描述你在图里**真的看见**的东西：

- colors：**这件商品本身**的主要颜色，用中文常见叫法（米白/藏青/焦糖…），
  最多 3 个。内搭、背景、模特的其他单品一律不算
- pattern：图案，如 纯色 / 条纹 / 波点 / 印花 / 格纹
- silhouette：版型轮廓，如 宽松直筒 / 修身 / A 字 / 束脚
- neckline：领型（没有就填空字符串）
- sleeve：袖长（没有就填空字符串）
- details：看得见的细节，如 罗纹袖口 / 双开口袋 / 金属拉链 / 流苏下摆
- scenes：这件东西看起来适合什么场合，最多 3 个
- description：一段 40-80 字的自然描述，供客服直接引用
- confidence：0-1，你对以上判断的把握

**绝对不要**出现：面料成分、克重、含绒量、洗涤方式、产地、价格、
任何厘米数。这些猜不出来，而店铺有权威数据。看不出来的字段就留空，
留空是正确答案，编一个不是。

输出 json：
{{"colors": ["米白"], "pattern": "纯色", "silhouette": "宽松直筒",
  "neckline": "圆领", "sleeve": "长袖", "details": ["罗纹袖口"],
  "scenes": ["通勤"], "description": "……", "confidence": 0.85}}"""


# ---------------------------------------------------------------- 事实与复查


@dataclass
class ProductFacts:
    """商品的权威事实，来自数据库而不是模型。

    VLM 负责"看见"，这里负责"知道"。两者合起来才是一条知识——
    也只有合起来，才能交叉验证出"图配错了商品"这种没人会主动发现的错。
    """

    product_id: int
    name: str
    category: str = ""
    category_id: int | None = None
    sku_colors: list[str] = field(default_factory=list)
    attrs: dict[str, str] = field(default_factory=dict)
    image_path: str = ""


def unobservable_claims(desc: dict[str, Any], exempt: str = "") -> list[str]:
    """揪出 VLM 说了它看不出来的东西。

    提示词里已经禁止过一次，这里再用规则复查——**提示词是请求，不是约束**。
    实测里模型很愿意顺口补一句"采用优质羊毛面料"，而这句话一旦进了知识库，
    客服就会当事实说给用户听。

    ``exempt`` 是商品名。**品类名里带成分词不算越界**——首轮实测三条误报
    全出在这儿：「轻薄保暖白鸭绒羽绒服」被判"羽绒"越界、「牛皮小方砖单肩包」
    被判"牛皮"越界。模型只是在称呼这件商品，而这个叫法是店铺自己挂出来的。
    **复述店铺已经公开的事实，不是模型在编。**

    只查自由文本字段：结构化字段是枚举值，编不出材质来。
    """
    hits: list[str] = []
    text = " ".join([
        str(desc.get("description") or ""),
        " ".join(str(x) for x in (desc.get("details") or [])),
    ])
    for word in FORBIDDEN_TOPICS:
        if word in text and word not in exempt:
            hits.append(word)
    # 数字 + 单位：克重、尺寸的另一种写法，正则比关键词更稳
    if re.search(r"\d+\s*(?:g|克|cm|厘米|mm)", text, re.I):
        hits.append("含具体数值")
    return hits


#: 颜色归并表。"藏青"和"蓝"、"米白"和"白"不该算作不符——
#: 首轮实测里 9009（SKU 写"蓝"，模型说"藏青"）就是这么误报的。
_COLOR_BASE = {
    "蓝": ("藏青", "深蓝", "宝蓝", "天蓝", "海军蓝", "牛仔蓝", "湖蓝"),
    "白": ("米白", "象牙白", "奶白", "米色", "本白", "乳白"),
    "红": ("酒红", "正红", "大红", "砖红", "枣红", "玫红"),
    "绿": ("墨绿", "军绿", "草绿", "橄榄绿", "青绿"),
    "灰": ("浅灰", "深灰", "银灰", "烟灰"),
    "棕": ("咖啡", "焦糖", "驼色", "卡其", "棕色"),
    "粉": ("藕粉", "浅粉", "豆沙粉"),
    "黑": ("炭黑", "墨黑"),
}

#: 出现在"颜色"规格位上的**花色词**。真实电商的"颜色分类"经常放的不是颜色
#: （淘宝上一半的连衣裙"颜色"是"碎花""波点"），拿它去比对具体颜色必然对不上。
#: 首轮实测里 9003（波点）与 9012（撞色）的五条误报全出在这儿。
_PATTERN_WORDS = ("波点", "撞色", "碎花", "条纹", "格纹", "印花", "拼色",
                  "花色", "图案", "迷彩", "豹纹")


def _base_color(c: str) -> str:
    """归到基础色。认不出来就原样返回。"""
    c = c.strip().rstrip("色")
    for base, variants in _COLOR_BASE.items():
        if c == base or any(v.rstrip("色") == c for v in variants):
            return base
    return c


def color_conflicts(desc: dict[str, Any], facts: ProductFacts) -> list[str]:
    """VLM 看到的颜色和 SKU 对不上的部分。

    对不上通常不是模型眼神差，是**图配错了商品**——而配错图没人会主动发现：
    页面上看着挺好，只有客服答"这款有藏青色"而图里是红的时候才暴露，
    那时已经在用户面前了。

    比对前要过两道归一，否则报出来的全是噪音（首轮实测 7 条误报里 5 条
    出在这两处）：

    1. **花色词不参与比对。** SKU 的"颜色"位上写着"波点""撞色"时，
       它压根不是一个颜色，拿任何颜色去比都不符。
    2. **同色系归并。** SKU 写"蓝"、模型说"藏青"，是同一个。
    """
    real = [c for c in facts.sku_colors
            if not any(p in c for p in _PATTERN_WORDS)]
    if not real:
        # 整个商品的"颜色"位都是花色词，这一项无从判断——
        # 报不出来比报一堆假警报好
        return []

    bases = {_base_color(s) for s in real}
    out = []
    for c in (str(x).strip() for x in (desc.get("colors") or [])):
        if c and _base_color(c) not in bases:
            out.append(c)
    return out


# ---------------------------------------------------------------- 打标


@dataclass
class TagResult:
    facts: ProductFacts
    desc: dict[str, Any] = field(default_factory=dict)
    item: KnowledgeItem | None = None
    flags: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.item is not None


def describe_asset(
    client: VisionClient, facts: ProductFacts, *, model: str = "vision"
) -> TagResult:
    """给一件商品的主图打标，产出一条 knowledge_item。"""
    result = TagResult(facts=facts)
    try:
        image, mime = read_image(facts.image_path)
    except (OSError, ValueError) as exc:
        result.error = f"读图失败：{exc}"
        return result

    try:
        desc = client.describe(
            model=model,
            system=ASSET_SYSTEM,
            user=ASSET_USER.format(name=facts.name, category=facts.category or "未知"),
            image=image, mime=mime,
        )
    except LlmError as exc:
        # 与关卡③同一套：调用失败**不是**"这张图没知识"，不能算进产出
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.desc = desc
    result.flags = [f"越界描述:{w}"
                    for w in unobservable_claims(desc, exempt=facts.name)]
    result.flags += [f"颜色与SKU不符:{c}" for c in color_conflicts(desc, facts)]
    result.item = to_knowledge_item(desc, facts, flags=result.flags)
    return result


def _clean(desc: dict[str, Any], exempt: str = "") -> dict[str, Any]:
    """把越界内容从描述里剔掉，而不是整条丢弃。

    整条丢掉的话，一句多余的"优质面料"就废掉了一整条本来正确的颜色版型
    描述；留着又会被客服当事实播出去。剔掉那一句是唯一说得通的处置。
    """
    out = dict(desc)
    text = str(out.get("description") or "")
    if text:
        kept = [
            s for s in re.split(r"(?<=[。；;])", text)
            if s.strip() and not unobservable_claims({"description": s}, exempt)
        ]
        out["description"] = "".join(kept)
    out["details"] = [
        d for d in (out.get("details") or [])
        if not unobservable_claims({"details": [d]}, exempt)
    ]
    return out


def to_knowledge_item(
    desc: dict[str, Any], facts: ProductFacts, *, flags: list[str] | None = None
) -> KnowledgeItem:
    """VLM 描述 + 数据库权威属性 → 一条 knowledge_item。

    **审核状态取决于有没有被交叉验证过**，不是一律 pending 也不是一律通过：

    * 颜色对得上 SKU、没有越界描述 → ``approved``。它已经被权威数据印证过
      一次，再压着等人工只会让知识库永远是空的。
    * 有任何一条不对 → ``pending``，进人工队列。

    这个区分是有意义的：全 pending 等于这条链路白做，全 approved 等于把
    模型的话直接当事实——而"能不能被别的数据印证"恰好是一条可判定的界线。
    """
    flags = flags or []
    d = _clean(desc, exempt=facts.name)

    lines = [f"商品：{facts.name}"]
    if facts.category:
        lines.append(f"类目：{facts.category}")

    seen = [
        ("颜色", "、".join(str(c) for c in (d.get("colors") or []))),
        ("图案", d.get("pattern")),
        ("版型", d.get("silhouette")),
        ("领型", d.get("neckline")),
        ("袖长", d.get("sleeve")),
        ("细节", "、".join(str(x) for x in (d.get("details") or []))),
        ("适合场合", "、".join(str(x) for x in (d.get("scenes") or []))),
    ]
    lines += [f"{k}：{v}" for k, v in seen if v]

    # 权威属性来自 product_attr / sku，不是模型看出来的。合进同一条知识里，
    # 客服才不需要为"颜色"和"材质"分别检索两次
    if facts.attrs or facts.sku_colors:
        lines.append("—— 以下为商品档案登记值 ——")
        if facts.sku_colors:
            # **可选颜色必须写进来。** 实测漏掉它的代价："这款有藏青色吗"
            # 词汇覆盖率只有 0.057——藏青确实在 SKU 里，却一个字都没进知识库，
            # 于是被判成"知识库里根本没有"而转人工。
            # 主图只拍得到一个颜色，另外几个色只有档案里有。
            lines.append(f"可选颜色：{'、'.join(facts.sku_colors)}")
        lines += [f"{k}：{v}" for k, v in facts.attrs.items()]

    if d.get("description"):
        lines.append(f"整体：{d['description']}")

    conf = float(d.get("confidence") or 0.7)
    return KnowledgeItem(
        # SPEC 而不是 QA：这条不是问答，是商品规格描述。分错了
        # 覆盖度矩阵会把它算进错误的格子，"哪里缺知识"就看不准了
        biz_type=BizType.SPEC,
        modality=Modality.IMAGE,
        title=f"{facts.name} 外观与规格",
        content="\n".join(lines),
        summary=str(d.get("description") or "")[:200] or None,
        product_ids=[facts.product_id],
        category_id=facts.category_id,
        tags=[str(t) for t in (d.get("scenes") or [])],
        source="vlm_asset",
        source_ref=f"product:{facts.product_id}:{Path(facts.image_path).name}",
        quality_score=round(min(conf, 0.95), 3),
        confidence=round(conf, 3),
        # 越界或颜色对不上就送人工；两样都干净说明已被 SKU 印证过一次
        review_status=ReviewStatus.PENDING if flags else ReviewStatus.APPROVED,
        knowledge_type=KnowledgeType.SPEC,
    )


def tag_assets(
    client: VisionClient, facts: list[ProductFacts], *, model: str = "vision",
    progress: Any = None,
) -> tuple[list[TagResult], GateStats]:
    """批量打标。返回逐条结果与漏斗统计。"""
    stats = GateStats(gate="⑥商品图打标", input_count=len(facts),
                      input_unit="张图", output_unit="条目")
    results: list[TagResult] = []

    for i, f in enumerate(facts, 1):
        r = describe_asset(client, f, model=model)
        results.append(r)
        if not r.ok:
            stats.dropped["模型调用或读图失败"] = \
                stats.dropped.get("模型调用或读图失败", 0) + 1
        elif r.flags:
            stats.modified["送人工复核"] = stats.modified.get("送人工复核", 0) + 1
        if progress:
            progress(i, len(facts))

    stats.output_count = sum(1 for r in results if r.ok)
    return results, stats
