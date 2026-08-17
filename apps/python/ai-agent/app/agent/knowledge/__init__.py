"""知识运维 Agent：把「答不上来的问题」变成「待审的知识」。

数据飞轮的后半圈原先卡在人身上：`handover list` 把盲点列出来，然后
每一条都要人从头查资料、从头写。这个 Agent 接的就是这一段——找依据、
起草、自查，把人的工作从"写"降成"审"。

**它的核心风险和另外两个 Agent 是同一条线，但后果最重。**

    客服编答案     → 误导一个用户一次
    导购编商品     → 用户点进去发现不存在
    知识运维编知识 → 写进库里，之后每一次相关检索都会命中它、
                     引用它，而且格外权威——它来自知识库

所以这条链上有两道锁：

1. :mod:`.grounding` —— 草稿里每个数字都要有出处，且要过广告法那道关。
   提示词里已经要求过一遍，这一步是去核对它有没有照做（**提示词是请求，
   不是约束**）
2. ``review_status`` 恒为 pending —— 机器不许给自己盖章。人工审核是
   这条链路的出口，不是可选的加固

分层与另外两个 Agent 一致，共用 :class:`~app.agent.nodes.Deps`、检索层、
工具层与出口检查：

* :mod:`state`     —— 盲点、依据、单个盲点的处理状态
* :mod:`cluster`   —— 近似问法聚成一个盲点（阈值是量出来的，见模块注释）
* :mod:`grounding` —— 依据核查
* :mod:`prompts`   —— 起草提示词（「材料不足」是预期内的正常输出）
* :mod:`nodes`     —— ``(state, deps) -> state``
* :mod:`graph`     —— 编排。**每个节点都有权终止这条链**
* :mod:`store`     —— 读盲点、写待审草稿
"""

from .graph import NODE_LABELS, run_batch, run_spot, safe_run_spot
from .state import BlindSpot, Evidence, OpsReport, SpotState
from .store import MySqlKnowledgeStore, StubKnowledgeStore

__all__ = [
    "NODE_LABELS", "BlindSpot", "Evidence", "MySqlKnowledgeStore", "OpsReport",
    "SpotState", "StubKnowledgeStore", "run_batch", "run_spot", "safe_run_spot",
]
