#!/usr/bin/env bash
# 对**真实 MySQL** 跑一遍订单状态机。
#
# 为什么单元测试之外还要这一条：H2 与 MySQL 在 UPDATE 的 SET 子句上语义不同。
# MySQL 从左到右求值，后面的赋值看得见前面刚写入的新值；H2 用标准 SQL 语义，
# 整行读旧值。于是这种写法
#
#     SET status = 'refunding', status_before_refund = status
#
# 在 H2 上把旧状态存进 status_before_refund（对），在 MySQL 上存的却是
# 'refunding' 自己（错）——驳回时"还原"成 refunding，订单永远卡在审核中。
#
# **这个 bug 真实发生过，而 73 个单元测试全绿。**H2 抓不到它，因为 H2 恰好
# 是宽容的那一侧。所以状态机要在真库上再走一遍。
#
# 前置：mall-product 已启动（默认 8081），MySQL 可连，SKU 种子数据已导入。
#
# 用法：
#   ./deploy/scripts/verify-order-lifecycle.sh
#   SKU_NO=S9002-WHITE-M ./deploy/scripts/verify-order-lifecycle.sh

set -euo pipefail

ORDER_BASE="${ORDER_BASE:-http://127.0.0.1:8081}"
API="$ORDER_BASE/api/product"
MYSQL_CONTAINER="${MYSQL_CONTAINER:-smdev-mysql}"
MYSQL_DATABASE="${MYSQL_DATABASE:-smartmall}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-root}"

SKU_NO="${SKU_NO:-S9003-FLORAL-S}"
USER_ID="${USER_ID:-10086}"
QTY=2
STOCK0=20

q() {
  docker exec "$MYSQL_CONTAINER" mysql -uroot -p"$MYSQL_ROOT_PASSWORD" \
    --default-character-set=utf8mb4 -N -e "$1" "$MYSQL_DATABASE" 2>/dev/null
}

FAIL=0
NO=""

check() {
  local label="$1" gotStatus gotStock
  gotStatus="$(q "SELECT status FROM mall_order WHERE order_no='$NO'")"
  gotStock="$(q "SELECT stock FROM sku WHERE sku_no='$SKU_NO'")"
  if [ "$gotStatus" = "$2" ] && [ "$gotStock" = "$3" ]; then
    printf "  ✓ %-22s 状态=%-16s 库存=%s\n" "$label" "$gotStatus" "$gotStock"
  else
    printf "  ✗ %-22s 状态=%-16s 库存=%-4s（期望 %s / %s）\n" \
      "$label" "$gotStatus" "$gotStock" "$2" "$3"
    FAIL=1
  fi
}

echo "==> 订单状态机全链路复核（$SKU_NO）"

if ! curl -sf "$ORDER_BASE/health" >/dev/null; then
  echo "  ✗ 订单服务不可用：$ORDER_BASE"
  # 必须先 install：-pl 只把 mall-product 放进 reactor，mall-common 不在里面，
  # 本地仓库里也没有，于是依赖解析直接失败。加 -am 也不行 —— 那会把 parent
  # 拉进 reactor，spring-boot:run 在 parent 上跑会报 "Unable to find a suitable
  # main class"。所以是两步，不是一步
  echo "    先启动：make run-product"
  echo "    或手动：cd apps/java && mvn -pl mall-product -am install -DskipTests \\"
  echo "                        && mvn -pl mall-product spring-boot:run"
  exit 1
fi

q "UPDATE sku SET stock = $STOCK0, status = 'on_sale' WHERE sku_no = '$SKU_NO'"

NO=$(curl -s -X POST "$API/orders" -H 'Content-Type: application/json' \
  -d "{\"requestId\":\"lc-$$-$(date +%s)\",\"userId\":$USER_ID,\"skuNo\":\"$SKU_NO\",\"quantity\":$QTY}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['orderNo'])")
echo "  订单号 $NO"
echo

check "下单（预占）"        pending_payment $((STOCK0 - QTY))

curl -s -X POST "$API/orders/$NO/pay?userId=$USER_ID" >/dev/null
check "支付"                paid            $((STOCK0 - QTY))

curl -s -X POST "$API/admin/orders/$NO/ship" -H 'Content-Type: application/json' \
  -d '{"company":"顺丰","expressNo":"SF7788123"}' >/dev/null
check "发货"                shipped         $((STOCK0 - QTY))

curl -s -X POST "$API/admin/orders/$NO/deliver" >/dev/null
check "送达"                delivered       $((STOCK0 - QTY))

curl -s -X POST "$API/orders/$NO/confirm?userId=$USER_ID" >/dev/null
check "确认收货"            completed       $((STOCK0 - QTY))

curl -s -X POST "$API/orders/$NO/refund?userId=$USER_ID" -H 'Content-Type: application/json' \
  -d '{"reason":"七天无理由"}' >/dev/null
check "申请退款（未回补）"  refunding       $((STOCK0 - QTY))

# 这一步就是 H2 抓不到的那个：驳回必须回到 completed，不能还是 refunding
curl -s -X POST "$API/admin/orders/$NO/refund/reject" -H 'Content-Type: application/json' \
  -d '{"reason":"已拆封"}' >/dev/null
check "驳回（回到申请前）"  completed       $((STOCK0 - QTY))

curl -s -X POST "$API/orders/$NO/refund?userId=$USER_ID" -H 'Content-Type: application/json' \
  -d '{"reason":"重新申请"}' >/dev/null
check "再次申请"            refunding       $((STOCK0 - QTY))

curl -s -X POST "$API/admin/orders/$NO/refund/approve" >/dev/null
check "同意退款（回补）"    refunded        "$STOCK0"

curl -s -X POST "$API/admin/orders/$NO/refund/approve" >/dev/null
check "重复同意（幂等）"    refunded        "$STOCK0"

curl -s -X POST "$API/orders/$NO/cancel?userId=$USER_ID" >/dev/null
check "已退款后取消（拒）"  refunded        "$STOCK0"

curl -s -X POST "$API/orders/$NO/pay?userId=$USER_ID" >/dev/null
check "已退款后支付（拒）"  refunded        "$STOCK0"

echo
# 物流轨迹的形状要与 004 种子一致——客服工具层直接把它读出来讲给用户听
TRACKS=$(q "SELECT tracks FROM mall_order WHERE order_no='$NO'")
if echo "$TRACKS" | grep -q '"ts"' && echo "$TRACKS" | grep -q '"desc"'; then
  echo "  ✓ 物流轨迹形状正确（含 ts/desc），客服可直接读"
else
  echo "  ✗ 物流轨迹形状不对：$TRACKS"
  FAIL=1
fi

# 时区一致性。created_at 由 Java 写（LocalDateTime.now()），shipped_at 由
# SQL 的 NOW() 写 —— 两个时钟不一致时，同一行里会出现「15:25 下单、23:25 发货」，
# 而客服正是照着这些字段回答"我的货什么时候发的"，于是它会讲出一段
# 根本没发生过的延迟。单元测试抓不到：H2 里两者都走 JVM 时钟，永远一致。
GAP=$(q "SELECT ABS(TIMESTAMPDIFF(MINUTE, created_at, shipped_at)) FROM mall_order WHERE order_no='$NO'")
if [ -n "$GAP" ] && [ "$GAP" -le 5 ]; then
  echo "  ✓ Java 与 MySQL 时钟一致（created_at 与 shipped_at 相差 ${GAP} 分钟）"
else
  echo "  ✗ 时区不一致：created_at 与 shipped_at 相差 ${GAP} 分钟"
  echo "    JVM 与 MySQL 时区不同。应用启动时会调 AppTimeZone.apply() 钉死时区，"
  echo "    检查 smartmall.timezone / TZ 是否与 MySQL 容器的 TZ 一致"
  FAIL=1
fi

echo
if [ "$FAIL" = 0 ]; then
  echo "✅ 状态机与库存全部符合预期"
else
  echo "❌ 复核失败"
fi
exit "$FAIL"
