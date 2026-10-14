"""跨进程测试用的 ai-agent：真 uvicorn + 真 Redis 会话后端，LLM 桩成"回声"。

回声桩会往会话里追加两条历史（用户、助手），这样能从 /chat/{id}/history 看出
上下文有没有跨实例/跨重启保留下来。用法：python _session_server.py <port>
"""
import sys
import time

import uvicorn

import app.routers.chat as chat_mod

DELAY = 0.0


def fake(message, state, deps):
    state.session.append("user", message)
    time.sleep(DELAY)
    state.answer = f"echo:{message}"
    state.session.append("assistant", state.answer)
    return state


chat_mod.safe_run_turn = fake
chat_mod.get_deps = lambda: object()

# WebSocket 路径：生成器。**历史是在第一个事件产出之后才追加的**——写回必须包住整个生成器，
# 而不是只包到第一个事件
import app.routers.ws as ws_mod  # noqa: E402


def fake_stream(message, state, deps):
    state.session.append("user", message)
    yield {"type": "delta", "text": "ec"}
    time.sleep(DELAY)
    state.answer = f"echo:{message}"
    state.session.append("assistant", state.answer)
    yield {"type": "done", "answer": state.answer, "session_id": state.session.session_id}


def fake_shopping_stream(message, state, deps):
    seen = state.need.category                 # 这一轮开始时已经记得的品类
    if "外套" in message:
        state.need.category = "外套"
    state.asked += 1
    state.session.append("user", message)
    yield {"type": "done", "answer": "ok", "seen_category": seen, "asked": state.asked,
           "session_id": state.session.session_id}


ws_mod.stream_turn = fake_stream
ws_mod.get_deps = lambda: object()
import app.agent.streaming as streaming_mod  # noqa: E402

streaming_mod.stream_shopping_turn = fake_shopping_stream

from app.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
