"""桩掉 LLM 的 ai-agent：每轮对话睡 FAKE_TURN_SECONDS 秒，其余（准入控制、限流、会话、线程池）全是真的。

用法（在要测的代码目录里）：
    cd apps/python/ai-agent
    PYTHONPATH=$PWD FAKE_TURN_SECONDS=2 CHAT_WORKERS=32 python <本文件> 8101

**PYTHONPATH 必须显式设**：Python 把"脚本所在目录"而不是 cwd 放进 sys.path[0]，而 ai-agent
通常是 `pip install -e` 装的，不设的话 `import app` 会解析到可编辑安装指向的那份代码——
用 git worktree 对比新旧两版时，两边实际跑的是同一份代码（这个坑踩过，"旧版"一度跑的是新代码）。
"""
import os
import sys
import threading
import time

import uvicorn

import app.routers.chat as chat_mod

DELAY = float(os.environ.get("FAKE_TURN_SECONDS", "2"))
calls = {"started": 0, "finished": 0, "probe": 0}
lock = threading.Lock()


def fake(message, agent_state, deps):
    with lock:
        calls["started"] += 1
    time.sleep(DELAY)
    with lock:
        calls["finished"] += 1
        # 探针（probe_proc.py）也会发聊天请求，单独记账：否则它们会被算成"客户端早已放弃的白跑调用"
        if agent_state.session.session_id.startswith("probe"):
            calls["probe"] += 1
    agent_state.answer = "ok"
    return agent_state


chat_mod.safe_run_turn = fake
chat_mod.get_deps = lambda: object()
from app.main import app  # noqa: E402


@app.get("/_stub/calls")
async def _calls():
    """服务端实际跑了多少轮——和客户端看到的成功数对比，差值就是白跑的 LLM 调用。"""
    return calls


uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
