"""跨 Agent 任务：让四个 Agent 互相派活。

在这个包之前，四个 Agent 是各跑各的——准确的说法是「四个共用底座的
Agent」，不是「Agent 集群」。这里把那条链闭上：

    客服答不上来 → 派「补写知识」给知识运维
                 → 补完了、且属于某个商品
                 → 派「更新文案」给运营

真正需要判断力的只有 :mod:`.dispatch`（什么情况下**不**派），
其余是管道。
"""

from .state import Agent, AgentTask, Kind, RunReport, Status, dedupe_key
from .store import MySqlTaskStore, StubTaskStore, TaskStore

__all__ = [
    "Agent", "AgentTask", "Kind", "RunReport", "Status", "dedupe_key",
    "MySqlTaskStore", "StubTaskStore", "TaskStore",
]
