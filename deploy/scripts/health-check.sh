#!/usr/bin/env bash
# 全栈健康检查。M0 的验收依据：11 个应用服务 /health 全绿。
#
# 用法：
#   ./deploy/scripts/health-check.sh          # 存活检查（快）
#   ./deploy/scripts/health-check.sh --ready   # 就绪检查（含依赖探测，慢但完整）

set -uo pipefail

READY_MODE=0
[ "${1:-}" = "--ready" ] && READY_MODE=1

HOST="${SMARTMALL_HOST:-localhost}"

# 名称|端口|路径
JAVA_SERVICES=(
  "mall-gateway|8080|/health"
  "mall-product|8081|/health"
  "mall-asset|8082|/health"
  "mall-dataplat|8083|/health"
  "mall-kpi|8084|/health"
)
PYTHON_SERVICES=(
  "ai-rag|9001"
  "ai-agent|9002"
  "ai-media|9003"
  "ai-clip|9004"
  "ai-train|9005"
)
INFRA=(
  "MySQL|3306"
  "Redis|6379"
  "Kafka|29092"
  "ClickHouse-HTTP|8123"
  "Milvus|19530"
  "SeaweedFS-S3|8333"
)
PLATFORMS=(
  "Langfuse|3000|/api/public/health"
  "Label Studio|8090|/health"
  "Airflow|8091|/api/v2/version"
  "SRS|1985|/api/v1/versions"
)

PASS=0; FAIL=0
green() { printf '\033[32m%s\033[0m' "$1"; }
red()   { printf '\033[31m%s\033[0m' "$1"; }
dim()   { printf '\033[2m%s\033[0m' "$1"; }

check_http() {
  local name=$1 url=$2 expect=${3:-200}
  local code
  # curl 连接失败时已经会输出 000，不要再用 `|| echo 000` 追加，否则显示成 000000
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null) || true
  code=${code:-000}
  if [ "$code" = "$expect" ]; then
    printf '  %s %-20s %s\n' "$(green ✓)" "$name" "$(dim "$url")"; PASS=$((PASS+1))
  else
    printf '  %s %-20s %s\n' "$(red ✗)" "$name" "$(dim "$url → HTTP $code")"; FAIL=$((FAIL+1))
  fi
}

check_tcp() {
  local name=$1 port=$2
  if (exec 3<>"/dev/tcp/$HOST/$port") 2>/dev/null; then
    exec 3<&- 3>&-
    printf '  %s %-20s %s\n' "$(green ✓)" "$name" "$(dim "$HOST:$port")"; PASS=$((PASS+1))
  else
    printf '  %s %-20s %s\n' "$(red ✗)" "$name" "$(dim "$HOST:$port 不可达")"; FAIL=$((FAIL+1))
  fi
}

echo
echo "── 中间件 ─────────────────────────────────────"
for e in "${INFRA[@]}"; do IFS='|' read -r n p <<< "$e"; check_tcp "$n" "$p"; done

echo
echo "── Java 业务服务 ──────────────────────────────"
for e in "${JAVA_SERVICES[@]}"; do
  IFS='|' read -r n p path <<< "$e"
  check_http "$n" "http://$HOST:$p$path"
done

echo
echo "── Python AI 服务 ─────────────────────────────"
# LiteLLM 的探针路径与其他服务不同（官方镜像自带，未认证可访问）
if [ $READY_MODE -eq 1 ]; then
  check_http "ai-gateway (ready)" "http://$HOST:9000/health/readiness"
else
  check_http "ai-gateway" "http://$HOST:9000/health/liveliness"
fi
for e in "${PYTHON_SERVICES[@]}"; do
  IFS='|' read -r n p <<< "$e"
  if [ $READY_MODE -eq 1 ]; then
    # /ready 在依赖未就绪时返回 503，这是设计行为而非故障
    check_http "$n (ready)" "http://$HOST:$p/ready"
  else
    check_http "$n" "http://$HOST:$p/health"
  fi
done

echo
echo "── 支撑平台 ───────────────────────────────────"
for e in "${PLATFORMS[@]}"; do
  IFS='|' read -r n p path <<< "$e"
  check_http "$n" "http://$HOST:$p$path"
done

echo
echo "───────────────────────────────────────────────"
if [ $FAIL -eq 0 ]; then
  echo "✅ 全部通过（$PASS 项）"
  exit 0
else
  echo "❌ $FAIL 项失败，$PASS 项通过"
  exit 1
fi
