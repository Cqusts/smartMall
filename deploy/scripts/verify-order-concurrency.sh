#!/usr/bin/env bash
# 对**真实 MySQL** 复核下单链路的防超卖。
#
# 为什么单元测试之外还要这一条：OrderConcurrencyTest 跑在 H2 上，H2 的行锁
# 实现与 InnoDB 不是一回事。防超卖的全部保证压在
#
#     UPDATE sku SET stock = stock - ? WHERE sku_no = ? AND stock >= ?
#
# 这条语句的原子性上，而它到底原子不原子，取决于具体存储引擎在持锁状态下
# 求值谓词的方式。H2 通过不等于 InnoDB 通过，所以真库上要再验一遍。
#
# 前置：mall-product 已启动（默认 8081），MySQL 可连，SKU 种子数据已导入。
#
# 用法：
#   ./deploy/scripts/verify-order-concurrency.sh
#   SKU_NO=S9002-WHITE-M STOCK=8 CONCURRENCY=80 ./deploy/scripts/verify-order-concurrency.sh

set -euo pipefail

ORDER_BASE="${ORDER_BASE:-http://127.0.0.1:8081}"
MYSQL_CONTAINER="${MYSQL_CONTAINER:-smdev-mysql}"
MYSQL_DATABASE="${MYSQL_DATABASE:-smartmall}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-root}"

SKU_NO="${SKU_NO:-S9001-BEIGE-M}"
STOCK="${STOCK:-5}"
CONCURRENCY="${CONCURRENCY:-50}"
USER_ID="${USER_ID:-10086}"

q() {
  docker exec "$MYSQL_CONTAINER" mysql -uroot -p"$MYSQL_ROOT_PASSWORD" \
    --default-character-set=utf8mb4 -N -e "$1" "$MYSQL_DATABASE" 2>/dev/null
}

echo "==> 复核防超卖：$CONCURRENCY 个并发请求抢 $STOCK 件（$SKU_NO）"

if ! curl -sf "$ORDER_BASE/health" >/dev/null; then
  echo "  ✗ 订单服务不可用：$ORDER_BASE"
  echo "    先启动：cd apps/java && mvn -pl mall-product spring-boot:run"
  exit 1
fi

# 清掉上一轮的痕迹，把库存摆到已知值
q "DELETE FROM mall_order WHERE sku_no = '$SKU_NO' AND request_id LIKE 'verify-%'"
q "UPDATE sku SET stock = $STOCK, status = 'on_sale' WHERE sku_no = '$SKU_NO'"
echo "  初始库存：$(q "SELECT stock FROM sku WHERE sku_no = '$SKU_NO'")"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 全部后台起，靠 wait 一起收——curl 进程本身的启动开销会摊掉一部分并发度，
# 但足以让请求落在同一个几十毫秒的窗口里，撞上同一行
for i in $(seq 1 "$CONCURRENCY"); do
  (
    curl -s -X POST "$ORDER_BASE/api/product/orders" \
      -H 'Content-Type: application/json' \
      -d "{\"requestId\":\"verify-$$-$i\",\"userId\":$USER_ID,\"skuNo\":\"$SKU_NO\",\"quantity\":1}" \
      > "$TMP/$i.json" 2>/dev/null
  ) &
done
wait

OK=$(grep -l '"code":0' "$TMP"/*.json 2>/dev/null | wc -l | tr -d ' ')
REJECTED=$(grep -l '库存不足' "$TMP"/*.json 2>/dev/null | wc -l | tr -d ' ')
FINAL_STOCK=$(q "SELECT stock FROM sku WHERE sku_no = '$SKU_NO'")
ORDERS=$(q "SELECT COUNT(*) FROM mall_order WHERE sku_no = '$SKU_NO' AND request_id LIKE 'verify-$$-%'")
UNITS=$(q "SELECT COALESCE(SUM(quantity),0) FROM mall_order WHERE sku_no = '$SKU_NO' AND request_id LIKE 'verify-$$-%'")

echo
echo "  成功下单      $OK"
echo "  库存不足被拒  $REJECTED"
echo "  订单表记录数  $ORDERS（$UNITS 件）"
echo "  终态库存      $FINAL_STOCK"
echo

FAIL=0
check() {
  if [ "$2" = "$3" ]; then
    echo "  ✓ $1"
  else
    echo "  ✗ $1（期望 $3，实际 $2）"
    FAIL=1
  fi
}

check "成功数恰好等于库存"       "$OK"          "$STOCK"
check "订单表记录数与成功数一致" "$ORDERS"      "$OK"
check "售出件数与初始库存一致"   "$UNITS"       "$STOCK"
check "终态库存归零"             "$FINAL_STOCK" "0"
check "全部请求都有结果"         "$((OK + REJECTED))" "$CONCURRENCY"

if [ "$FINAL_STOCK" -lt 0 ]; then
  echo "  ✗✗ 出现负库存 —— 超卖了"
  FAIL=1
fi

echo
if [ "$FAIL" = 0 ]; then
  echo "✅ 未超卖，库存守恒"
else
  echo "❌ 复核失败"
fi
exit "$FAIL"
