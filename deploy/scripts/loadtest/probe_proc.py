"""独立进程里的旁观者：量服务端对 /health 的响应时间，并在洪峰中途单独发一批聊天请求。

**必须是独立进程**：和压力生成器共用事件循环的话，量到的是生成器自己的卡顿，不是服务端的。
用法：python probe_proc.py http://127.0.0.1:8101 30
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict

base, seconds = sys.argv[1], float(sys.argv[2])
lat, chat = [], []


def chat_probe(i):
    body = json.dumps({"message": "hi", "session_id": f"probe{i}"}).encode()
    req = urllib.request.Request(base + "/chat", data=body, headers={"content-type": "application/json"})
    t = time.perf_counter()
    try:
        st = urllib.request.urlopen(req, timeout=15).status
    except urllib.error.HTTPError as e:
        st = e.code
    except Exception as e:  # noqa: BLE001
        st = type(e).__name__
    chat.append((st, time.perf_counter() - t))


def chat_probes():
    time.sleep(1.5)                       # 洪峰已经把名额和等待区占满
    ths = []
    for i in range(20):
        th = threading.Thread(target=chat_probe, args=(i,))
        th.start()
        ths.append(th)
        time.sleep(0.1)
    for th in ths:
        th.join()


threading.Thread(target=chat_probes, daemon=True).start()
end = time.perf_counter() + seconds
while time.perf_counter() < end:
    t = time.perf_counter()
    try:
        urllib.request.urlopen(base + "/health", timeout=10).read()
        lat.append(time.perf_counter() - t)
    except Exception:  # noqa: BLE001
        lat.append(99.0)
    time.sleep(0.1)
lat.sort()
by = defaultdict(list)
for st, x in chat:
    by[str(st)].append(x)
summary = {k: {"n": len(v), "p50_ms": round(sorted(v)[len(v) // 2] * 1000), "max_ms": round(max(v) * 1000)}
           for k, v in by.items()}
print(json.dumps({"mid_burst_chat_probe": summary, "health_p50_ms": round(lat[len(lat) // 2] * 1000),
                  "health_p99_ms": round(lat[int(len(lat) * .99) - 1] * 1000),
                  "health_max_ms": round(lat[-1] * 1000), "health_failed": sum(1 for x in lat if x >= 99)}))
