#!/bin/bash
# 起一个被测服务、同时跑独立进程的探针和压力脚本，最后关掉服务。
#
#   LOAD_ARGS="400 6" ./run_case.sh 名字 代码目录 端口 ./load_burst.py -- FAKE_TURN_SECONDS=2 CHAT_WORKERS=32
#
# 对比新旧两版：用 git worktree 拿到旧版代码目录，同一条命令各跑一次：
#   git worktree add --detach /tmp/old_wt <旧提交>
#   ./run_case.sh old /tmp/old_wt/apps/python/ai-agent 8101 ./load_burst.py -- FAKE_TURN_SECONDS=2 CHAT_WORKERS=32
#   ./run_case.sh new $PWD/../../../apps/python/ai-agent 8102 ./load_burst.py -- FAKE_TURN_SECONDS=2 CHAT_WORKERS=32 CHAT_MAX_WAITING=32 CHAT_QUEUE_TIMEOUT=4
HERE="$(cd "$(dirname "$0")" && pwd)"
name=$1; cwd=$2; port=$3; load=$4; shift 5
cd "$cwd"
env PYTHONPATH="$cwd" LOG_LEVEL=WARNING "$@" python3 "$HERE/fake_agent_server.py" "$port" > "/tmp/loadtest_srv_$name.log" 2>&1 &
srv=$!
for i in $(seq 1 60); do curl -sf "http://127.0.0.1:$port/health" >/dev/null && break; sleep 0.5; done
kind=$(curl -s "http://127.0.0.1:$port/info" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("新代码(有 chat_load)" if "chat_load" in d else "旧代码(无 chat_load)")')
echo "=== $name  server pid $srv  $kind    <- 确认这里显示的是你想测的那一版"
python3 "$HERE/probe_proc.py" "http://127.0.0.1:$port" "${PROBE_SECONDS:-30}" > "/tmp/loadtest_probe_$name.json" &
pp=$!
python3 "$HERE/$(basename "$load")" "http://127.0.0.1:$port" $LOAD_ARGS
wait $pp
echo "探针（独立进程）: $(cat "/tmp/loadtest_probe_$name.json")"
kill $srv
wait $srv 2>/dev/null
exit 0
