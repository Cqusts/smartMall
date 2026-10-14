"""洪峰：t=0 突发 N 个聊天请求（客户端超时 T 秒），t=T+1 来一个"旁观用户"，看洪峰过后能不能恢复。

用法：python load_burst.py http://127.0.0.1:8101 400 6
输出里最该看的三个数：
  wasted_calls    服务端跑了、但客户端早已放弃的 LLM 调用数（过载保护要把它降到 0）
  bystander       洪峰之后才来的用户等了多久
  server_idle_after_s  服务端多久后才空闲下来
"""
import asyncio, json, sys, time
import httpx

base, N, TIMEOUT = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])

async def one(c, i, t0, out):
    t = time.perf_counter()
    try:
        r = await c.post("/chat", json={"message": "hi", "session_id": f"burst{i}"}, timeout=TIMEOUT)
        out.append((r.status_code, time.perf_counter() - t))
    except httpx.TimeoutException:
        out.append(("timeout", time.perf_counter() - t))
    except Exception as e:
        out.append((type(e).__name__, time.perf_counter() - t))

async def bystander(c, delay, out):
    await asyncio.sleep(delay)
    t = time.perf_counter()
    try:
        r = await c.post("/chat", json={"message": "你好，我是洪峰之后才来的用户", "session_id": "bystander"}, timeout=30)
        out.append((r.status_code, time.perf_counter() - t))
    except Exception as e:
        out.append((type(e).__name__, time.perf_counter() - t))

async def main():
    limits = httpx.Limits(max_connections=1000, max_keepalive_connections=1000)
    async with httpx.AsyncClient(base_url=base, limits=limits) as c, httpx.AsyncClient(base_url=base) as c2:
        out, by, hl, stop = [], [], [], asyncio.Event()
        has_guard = 'chat_load' in (await c2.get('/info')).json()
        t0 = time.perf_counter()
        by_task = asyncio.ensure_future(bystander(c2, TIMEOUT + 1, by))
        await asyncio.gather(*[one(c, i, t0, out) for i in range(N)])
        await by_task
        # 等服务端把残留的活干完，量它"还忙多久"
        busy_until = None
        for _ in range(120):
            s = (await c2.get("/info")).json().get("chat_load")
            calls = (await c2.get("/_stub/calls")).json()
            if calls["started"] == calls["finished"]:
                busy_until = time.perf_counter() - t0
                break
            await asyncio.sleep(0.5)
        calls = (await c2.get("/_stub/calls")).json()
    from collections import Counter
    st = Counter(str(s) for s, _ in out)
    ok_lat = sorted(l for s, l in out if s == 200)
    rej_lat = sorted(l for s, l in out if s == 503)
    print(json.dumps({
        "burst_status": dict(st),
        "ok_p50_s": round(ok_lat[len(ok_lat)//2], 2) if ok_lat else None,
        "reject_p50_s": round(rej_lat[len(rej_lat)//2], 3) if rej_lat else None,
        "reject_max_s": round(rej_lat[-1], 3) if rej_lat else None,
        "server_llm_calls": calls["finished"],
        "useful_calls": st.get("200", 0),
        "wasted_calls": calls["finished"] - st.get("200", 0) - (1 if by and by[0][0] == 200 else 0),
        "bystander": [(str(s), round(l, 2)) for s, l in by],
        "has_guard": has_guard,
        "server_idle_after_s": round(busy_until, 1) if busy_until else None,
    }, ensure_ascii=False))
asyncio.run(main())
