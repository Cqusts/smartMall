"""滥用：一个滥用者（独立进程，不停发消息、不停换 session_id 来绕过按会话的限流）
和 8 个正常用户（另一个独立进程，每人每 4 秒一句）同时打同一个服务。
看正常用户的成功率和延迟。两边都带 X-Forwarded-For（滥用者 6.6.6.6，正常用户各自一个）——
所以被测服务要设 CHAT_TRUST_PROXY_HOPS=1，否则所有请求在服务端看来都来自 127.0.0.1、共用一个 IP 桶。

用法：python load_mixed.py http://127.0.0.1:8101 20
"""
import asyncio, json, subprocess, sys, time
import httpx

if sys.argv[1].startswith("http"):          # 编排模式：run_case.sh 传来的是 base_url D
    role, base, D = "main", sys.argv[1], float(sys.argv[2])
else:
    role, base, D = sys.argv[1], sys.argv[2], float(sys.argv[3])

async def abuser():
    stats = {"200": 0, "429": 0, "503": 0, "other": 0}
    end = time.perf_counter() + D
    async with httpx.AsyncClient(base_url=base, limits=httpx.Limits(max_connections=400)) as c:
        async def loop(i):
            k = 0
            while time.perf_counter() < end:
                k += 1
                try:
                    r = await c.post("/chat", json={"message": "spam", "session_id": f"abuse{i}-{k}"},
                                     headers={"x-forwarded-for": "6.6.6.6"}, timeout=10)
                    stats[str(r.status_code) if str(r.status_code) in stats else "other"] += 1
                except Exception:
                    stats["other"] += 1
        await asyncio.gather(*[loop(i) for i in range(150)])
    print(json.dumps({"abuser": stats}))

async def polite():
    res = []
    end = time.perf_counter() + D
    async with httpx.AsyncClient(base_url=base) as c:
        async def user(u):
            await asyncio.sleep(u * 0.37)
            while time.perf_counter() < end:
                t = time.perf_counter()
                try:
                    r = await c.post("/chat", json={"message": "请问有货吗", "session_id": f"polite{u}", "user_id": 1000 + u},
                                     headers={"x-forwarded-for": f"203.0.113.{10 + u}"}, timeout=10)
                    res.append((r.status_code, time.perf_counter() - t))
                except httpx.TimeoutException:
                    res.append(("timeout", time.perf_counter() - t))
                await asyncio.sleep(4)
        await asyncio.gather(*[user(u) for u in range(8)])
    ok = sorted(l for s, l in res if s == 200)
    from collections import Counter
    print(json.dumps({"polite": {"requests": len(res), "status": dict(Counter(str(s) for s, _ in res)),
        "ok_p50_s": round(ok[len(ok)//2], 2) if ok else None, "ok_max_s": round(ok[-1], 2) if ok else None}}))

if role == "abuser": asyncio.run(abuser())
elif role == "polite": asyncio.run(polite())
else:
    procs = [subprocess.Popen([sys.executable, __file__, r, base, str(D)], stdout=subprocess.PIPE, text=True)
             for r in ("abuser", "polite")]
    for p in procs: print(p.communicate()[0].strip())
