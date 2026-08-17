"""运营 Agent：给商品批量生产文案。

**它和"套模板写文案"的唯一区别，是它知道用户实际在问什么。**
运营拍脑袋想的卖点是"高级感"，而客服埋点里躺着的事实是：这件衣服
被问得最多的是"会不会起球"。后者才是用户下单前真正犹豫的地方——
把它写进主图，比任何形容词都管用。这一环是数据飞轮在运营侧的体现：
**客服数据反哺文案生产**（依赖 009 迁移给 agent_trace 补的 product_id）。

产出是**对外发布**的，所以合规这一层比另外三个 Agent 都严：

* 客服回复 —— 一对一、即时，说错是一次失言
* 营销文案 —— 印在详情页上、一对多，说错是可被投诉、可被处罚的
  广告违法，而责任主体是店铺不是模型

因此 :mod:`compliance` 里三类检查一律**只拦截不改写**：把"最好"自动
改成"很好"看着无害，但改完就没人再看一眼了。

**只做文案这一条子链路。** docs/06 里的宣传图（ComfyUI + IP-Adapter）
与宣传视频（Wan2.2 + CosyVoice2）需要 24G 显卡，本机跑不起来，
所以没写——**没有留空壳接口**，那种代码看着像做完了，实际一行都没验证过。

* :mod:`state`      —— 卖点带来源、文案分形态
* :mod:`compliance` —— 极限词 / 功效宣称 / 属性冲突 / 数字出处
* :mod:`prompts`    —— 提炼卖点、一次生成多形态
* :mod:`nodes`      —— ``(state, deps) -> state``
* :mod:`graph`      —— 编排
* :mod:`store`      —— 写待审文案
"""

from .graph import NODE_LABELS, run_copy, safe_run_copy
from .state import CopyBrief, CopyDraft, DemandSignal, MarketingState, SellingPoint
from .store import MySqlCopyStore, StubCopyStore

__all__ = [
    "NODE_LABELS", "CopyBrief", "CopyDraft", "DemandSignal", "MarketingState",
    "MySqlCopyStore", "SellingPoint", "StubCopyStore", "run_copy",
    "safe_run_copy",
]
