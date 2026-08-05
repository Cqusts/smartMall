"""合成对话生成器 —— 冷启动的第二条腿，也是流水线的测试夹具。

两个用途：

1. **冷启动补充**：公开数据集覆盖不到自有商品，按「类目 × 问题类型」矩阵
   批量生成候选 QA，人工校验后入库（见 docs/00-overview.md 第 4 节）。

2. **流水线测试夹具**：生成的对话**故意包含**四道关卡应当清除的噪声——
   PII、订单号、系统话术、无效寒暄、近重复。这样"关卡有没有生效"是
   可断言的，而不是靠肉眼看。

生产环境的合成数据应由 LLM 生成（`gates.gate3_model.LlmClient`），
本模块用模板生成，目的是**确定性**——测试需要可复现的输入。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from ..models import Dialogue, Turn

# 商品清单。与 deploy/sql/mysql/99_seed.sql 的 product 表保持一致——
# 清洗时需要 {product_id: category_id} 映射来填知识条目的类目，
# 对不上的话映射查不到，覆盖度矩阵会显示 0%。
#
# 注意商品 ID 与类目 ID 是两个维度：一个类目下可以有多个商品。
PRODUCTS: dict[str, tuple[int, int]] = {
    # 商品简称: (product_id, category_id)
    "针织衫": (9001, 1024),
    "羽绒服": (9002, 1025),
    "连衣裙": (9003, 1026),
    "单肩包": (9004, 2048),
}

CATEGORIES: dict[str, int] = {name: cat for name, (_, cat) in PRODUCTS.items()}

QUESTION_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "材质": [
        ("这件{cat}是什么面料的", "亲这款是100%羊毛的呢，克重320g，秋冬单穿就够暖了"),
        ("{cat}会起球吗", "不会哦，我们做了抗起球处理，洗五次都不起球的"),
        ("{cat}的面料厚吗", "厚度适中的，320克重，不算特别厚但很保暖"),
    ],
    "尺码": [
        ("我160cm 50kg穿什么码", "亲您这个身高体重建议拍S码哦，我们版型偏宽松"),
        ("{cat}的尺码标准吗", "我们家尺码是标准的，建议按平时穿的码数拍"),
        ("{cat}偏大还是偏小", "这款是宽松版型，偏大一码，介意的话可以拍小一码"),
    ],
    "物流": [
        ("{cat}什么时候发货", "亲，订单3421789023我们48小时内安排发货哦"),
        ("发什么快递", "默认发中通快递，江浙沪一般2-3天到"),
        ("能不能发顺丰", "可以的亲，需要补运费差价8元"),
    ],
    "售后": [
        ("{cat}可以七天无理由退换吗", "支持的亲，签收后7天内不影响二次销售都可以退"),
        ("退货运费谁承担", "质量问题我们承担，非质量问题需要亲承担哦"),
        ("收到货有问题怎么办", "亲拍个照片发我，我们核实后给您处理"),
    ],
    "养护": [
        ("{cat}怎么洗", "建议手洗或者干洗哦，水温不要超过30度"),
        ("能机洗吗", "可以的，建议放洗衣袋里轻柔模式洗"),
    ],
    "活动": [
        ("现在有活动吗", "亲现在双十一预售，定金50抵100呢"),
        ("能便宜点吗", "亲这个已经是活动价啦，我们给您申请个优惠券好吗"),
    ],
}

# 关卡②应当剥离的系统话术
SYSTEM_PHRASES = [
    "正在为您转接人工客服，请稍候",
    "您好，很高兴为您服务，请问有什么可以帮您的呢？",
    "客服小妹正在忙，稍后为您解答",
    "亲，还在吗？在的话回复我一下哦～",
]

# 关卡②应当剥离的无效寒暄
PLEASANTRIES = ["在吗", "你好", "嗯嗯", "好的", "谢谢", "哦", "行"]

# 关卡①应当脱敏的 PII
PII_SNIPPETS = [
    "我手机号13812345678，麻烦发货前联系我",
    "地址是浙江省杭州市西湖区文三路100号",
    "我身份证330106199001011234，需要报关吗",
    "退到我卡上6222021234567890123 可以吗",
]


def _iso(base: datetime, offset_s: int) -> datetime:
    return base + timedelta(seconds=offset_s)


def generate_dialogue(
    seq: int,
    rng: random.Random,
    *,
    category: str | None = None,
    question_type: str | None = None,
    inject_noise: bool = True,
) -> Dialogue:
    """生成一段合成客服对话。

    Args:
        seq: 序号，用于生成 session_no。
        rng: 传入的随机源，保证可复现。
        category / question_type: 指定类目与问题类型；None 则随机。
        inject_noise: 是否注入应被清洗的噪声。测试关卡效果时设 True。
    """
    category = category or rng.choice(list(PRODUCTS))
    question_type = question_type or rng.choice(list(QUESTION_TEMPLATES))
    product_id, _ = PRODUCTS[category]

    started = datetime(2026, 3, 1, 10, 0, 0) + timedelta(minutes=seq)
    turns: list[Turn] = []
    idx = 0

    def add(role: str, content: str, cost: int | None = None) -> None:
        nonlocal idx
        turns.append(
            Turn(
                turn_index=idx,
                role=role,  # type: ignore[arg-type]
                content=content,
                said_at=_iso(started, idx * 30),
                reply_cost_ms=cost,
            )
        )
        idx += 1

    if inject_noise:
        # 系统欢迎语 + 用户寒暄：关卡②应当剥离
        add("system", rng.choice(SYSTEM_PHRASES))
        add("user", rng.choice(PLEASANTRIES))
        add("agent", "在的亲～", 4200)

    # 真正有知识价值的问答，可能有 1-3 组
    n_qa = rng.randint(1, 3)
    used: set[str] = set()
    for _ in range(n_qa):
        qtype = question_type if len(used) == 0 else rng.choice(list(QUESTION_TEMPLATES))
        q, a = rng.choice(QUESTION_TEMPLATES[qtype])
        if q in used:
            continue
        used.add(q)
        add("user", q.format(cat=category))
        add("agent", a, rng.randint(3000, 60000))

    if inject_noise and rng.random() < 0.4:
        # PII：关卡①应当脱敏
        add("user", rng.choice(PII_SNIPPETS))
        add("agent", "好的亲，已经备注啦", 5000)

    if inject_noise and rng.random() < 0.5:
        add("user", rng.choice(PLEASANTRIES))

    return Dialogue(
        session_no=f"SYN{seq:08d}",
        source_type="synthetic",
        channel="web",
        agent_id=rng.choice([101, 102, 103, 104]),
        agent_type="human",
        product_ids=[product_id],
        turns=turns,
        started_at=started,
        ended_at=_iso(started, idx * 30),
        transferred_human=rng.random() < 0.1,
        order_created=rng.random() < 0.25,
    )


def generate_batch(
    count: int,
    *,
    seed: int = 42,
    inject_noise: bool = True,
    duplicate_rate: float = 0.1,
) -> list[Dialogue]:
    """生成一批对话。

    Args:
        duplicate_rate: 近重复比例。关卡①的 MinHash 去重应当把它们清掉，
            这让"去重有没有生效"成为可断言的事实。
    """
    rng = random.Random(seed)
    dialogues = [
        generate_dialogue(i, rng, inject_noise=inject_noise) for i in range(count)
    ]

    n_dup = int(count * duplicate_rate)
    for i in range(n_dup):
        src = dialogues[rng.randrange(len(dialogues))]
        dup = src.model_copy(deep=True)
        dup.session_no = f"SYNDUP{i:06d}"
        # 近重复而非完全重复：加一句无关紧要的话，考验 MinHash 而不只是精确哈希
        dup.turns.append(
            Turn(turn_index=len(dup.turns), role="user", content="好的谢谢")
        )
        dialogues.append(dup)

    rng.shuffle(dialogues)
    return dialogues


def coverage_matrix_spec() -> dict[str, list[str]]:
    """返回合成器覆盖的「类目 × 问题类型」矩阵，供覆盖度分析对照。"""
    return {cat: list(QUESTION_TEMPLATES) for cat in PRODUCTS}


def product_category_map() -> dict[int, int]:
    """``{product_id: category_id}``，与 99_seed.sql 的 product 表一致。

    离线测试时可直接用它，不必连库。
    """
    return {pid: cat for pid, cat in PRODUCTS.values()}
