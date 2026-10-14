# 并发与容量压测脚本

提交说明和 `docs/17-capacity-and-concurrency.md` 里的数字都是用这些脚本量出来的。放在仓库里，
是为了让你能自己复现、也能在改动之后再量一遍。

## 对话入口：过载保护与限流（不需要 LLM、不需要 MySQL）

`fake_agent_server.py` 把一轮对话桩成"睡 N 秒"，其余（准入控制、限流、会话、线程池、
真 uvicorn）都是真的。`run_case.sh` 起服务、跑压力脚本、同时用**独立进程**的探针量服务端。

```bash
cd deploy/scripts/loadtest

# 洪峰：400 个请求同时来，客户端 6 秒超时，32 个工作线程，每轮 2 秒
export LOAD_ARGS="400 6"
./run_case.sh new ../../../apps/python/ai-agent 8102 ./load_burst.py -- \
    FAKE_TURN_SECONDS=2 CHAT_WORKERS=32 CHAT_RATE_BACKEND=off CHAT_MAX_WAITING=32 CHAT_QUEUE_TIMEOUT=4

# 对比旧版：用 git worktree 拿到旧提交的代码目录
git worktree add --detach /tmp/old_wt 41cb4f7
./run_case.sh old /tmp/old_wt/apps/python/ai-agent 8101 ./load_burst.py -- \
    FAKE_TURN_SECONDS=2 CHAT_WORKERS=32
git worktree remove --force /tmp/old_wt

# 滥用：一个不停换 session_id 的滥用者 + 8 个正常用户（限流计数放 Redis）
redis-server --port 6399 --save "" --appendonly no &
export LOAD_ARGS="20" PROBE_SECONDS=22
./run_case.sh new ../../../apps/python/ai-agent 8103 ./load_mixed.py -- \
    FAKE_TURN_SECONDS=1 CHAT_WORKERS=16 CHAT_MAX_WAITING=16 CHAT_QUEUE_TIMEOUT=3 \
    CHAT_RATE_BACKEND=redis REDIS_PORT=6399 CHAT_TRUST_PROXY_HOPS=1
```

**量的时候踩过的坑（都写进了脚本的注释，这里再强调一遍）：**

- 压力生成器、探针必须是**不同进程**。单个 Python 进程发几百个并发请求，自己就是瓶颈，
  共用事件循环的探针量到的是生成器的卡顿，不是服务端的。
- 对比新旧两版时，**先确认两边跑的真是两份不同的代码**：`run_case.sh` 会打印"新代码/旧代码"。
  Python 把脚本所在目录而不是 cwd 放进 `sys.path[0]`，可编辑安装会让 `import app` 悄悄解析到同一份。
- 别用 `pkill -f <模式>` 清理：模式出现在你自己那条命令行里时会把自己的 shell 一起杀掉。

## 数据库：agent_trace 容量与归档（需要真 MySQL）

`db_trace_bench.py` 只动临时库 `smartmall_bench`，需要管理员账号
（`MYSQL_ADMIN_USER` / `MYSQL_ADMIN_PASSWORD`，约定同 `deploy/scripts/migrate.py`）。

```bash
python db_trace_bench.py fill 1000000   # 真实 DDL（003+009+015），灌 100 万行
python db_trace_bench.py probe          # 单行插入吞吐 + product_demand 耗时
python db_trace_bench.py verify         # 归档验证：线上写入和点赞同时进行，校验不丢不重
```

要看 buffer pool 的差别，让 MySQL 带 `--innodb-flush-method=O_DIRECT` 启动（否则 OS page cache
会兜底，看不出差别），再 `SET GLOBAL innodb_buffer_pool_size = ...` 在线切换对比 128MB 与 1GB。
