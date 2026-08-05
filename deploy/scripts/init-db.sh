#!/usr/bin/env bash
# 初始化数据库。
#
# MySQL 的建表脚本由容器的 docker-entrypoint-initdb.d 在首次启动时自动执行，
# 本脚本负责：① 等待 MySQL 就绪并校验建表结果；② 应用 ClickHouse DDL
# （ClickHouse 没有等价的 initdb 机制）；③ 幂等重跑时补建缺失的表。
#
# 用法：./deploy/scripts/init-db.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"

[ -f "$DEPLOY_DIR/.env" ] && set -a && . "$DEPLOY_DIR/.env" && set +a

MYSQL_CONTAINER="${MYSQL_CONTAINER:-sm-mysql}"
CH_CONTAINER="${CH_CONTAINER:-sm-clickhouse}"
MYSQL_DATABASE="${MYSQL_DATABASE:-smartmall}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-root}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-smartmall}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-smartmall}"

# 与 deploy/sql/mysql 中的建表脚本对应，用于校验
EXPECTED_TABLES=(
  category product sku product_attr size_chart live_schedule
  asset asset_product_rel asset_clip_meta asset_audit
  ds_source ds_job
  ods_raw_dialogue ods_raw_asset ods_raw_clip ods_raw_doc ods_process_log
  dwd_dialogue_session dwd_dialogue_turn dwd_clip_segment
  knowledge_item sft_sample dataset_version eval_sample eval_result model_version
  kpi_metric kpi_score kpi_golden_set kpi_calibration kpi_appeal
)

mysql_exec() {
  docker exec -i "$MYSQL_CONTAINER" \
    mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4 "$@"
}

echo "==> 等待 MySQL 就绪"
for i in $(seq 1 60); do
  if docker exec "$MYSQL_CONTAINER" mysqladmin ping -uroot -p"$MYSQL_ROOT_PASSWORD" >/dev/null 2>&1; then
    echo "  ✓ MySQL 就绪"
    break
  fi
  [ "$i" -eq 60 ] && { echo "  ✗ MySQL 60 秒内未就绪"; exit 1; }
  sleep 1
done

# 幂等：所有建表脚本都是 CREATE TABLE IF NOT EXISTS，重跑安全。
# 覆盖容器已初始化过、但后来新增了建表脚本的情况。
echo "==> 应用 MySQL 建表脚本"
for f in "$DEPLOY_DIR"/sql/mysql/*.sql; do
  echo "  · $(basename "$f")"
  mysql_exec "$MYSQL_DATABASE" < "$f"
done

echo "==> 校验表结构"
MISSING=()
EXISTING=$(mysql_exec -N -B -e "SELECT table_name FROM information_schema.tables WHERE table_schema='$MYSQL_DATABASE';")
for t in "${EXPECTED_TABLES[@]}"; do
  grep -qx "$t" <<< "$EXISTING" || MISSING+=("$t")
done
if [ ${#MISSING[@]} -gt 0 ]; then
  echo "  ✗ 缺失表：${MISSING[*]}"
  exit 1
fi
echo "  ✓ ${#EXPECTED_TABLES[@]} 张表全部就绪"

echo "==> 应用 ClickHouse DDL"
if docker ps --format '{{.Names}}' | grep -qx "$CH_CONTAINER"; then
  for i in $(seq 1 30); do
    docker exec "$CH_CONTAINER" wget --spider -q http://localhost:8123/ping 2>/dev/null && break
    [ "$i" -eq 30 ] && { echo "  ✗ ClickHouse 30 秒内未就绪"; exit 1; }
    sleep 1
  done
  for f in "$DEPLOY_DIR"/sql/clickhouse/*.sql; do
    echo "  · $(basename "$f")"
    docker exec -i "$CH_CONTAINER" clickhouse-client \
      --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
      --multiquery < "$f"
  done
  echo "  ✓ ClickHouse 就绪"
else
  echo "  ⚠ 未发现 $CH_CONTAINER 容器，跳过（dev 模式下正常）"
fi

echo
echo "✅ 数据库初始化完成"
