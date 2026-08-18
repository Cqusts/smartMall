#!/usr/bin/env bash
# 按顺序应用 deploy/sql/migrations/ 下的迁移，并记录已应用的版本。
#
# **为什么需要一个真的迁移器，而不是 `for f in *.sql; do mysql < $f; done`：**
# 迁移里有 ALTER TABLE ... ADD COLUMN，那不是幂等的 —— 第二次跑直接
# 「Duplicate column name 'request_id'」。而这个脚本注定会被反复执行
# （每次拉完代码都该跑一遍），所以必须知道哪些已经做过。
#
# 做法是最朴素的那种：一张 schema_migrations 表记文件名，跑之前先查。
#
# 另外两个坑都在这里一并处理掉了：
#
# · **字符集**。连接不带 --default-character-set=utf8mb4 时，迁移里的中文会
#   按 latin1 解释，类目名写进去就成了「Tæ¤」。手敲命令时最容易漏这个参数。
#
# · **001 在全新安装上不该执行**。它给老库补 ods_process_log 与
#   knowledge_item.knowledge_type，而 deploy/sql/mysql/*.sql 里已经含这两样，
#   initdb 建完库它们就在了。所以检测到已存在就直接记为已应用，不去跑它。
#
# 用法：
#   ./deploy/scripts/migrate.sh              # 应用所有待执行的迁移
#   ./deploy/scripts/migrate.sh --status     # 只看状态，不改数据库
#   ./deploy/scripts/migrate.sh --baseline   # 把现有迁移全部标记为已应用，但不执行
#
# --baseline 是给「之前手工 mysql < xxx.sql 跑过」的库用的：那些库结构已经对了，
# 只是没有记录，直接跑会撞 Duplicate column。基线一次之后就能正常增量。
#
# 连接方式自动选择：能 docker exec 到 MySQL 容器就走容器，否则用本机 mysql 客户端
# （通过 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD 连）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MIGRATIONS="$ROOT/deploy/sql/migrations"

[ -f "$ROOT/deploy/.env" ] && set -a && . "$ROOT/deploy/.env" && set +a

MYSQL_CONTAINER="${MYSQL_CONTAINER:-smdev-mysql}"
MYSQL_DATABASE="${MYSQL_DATABASE:-smartmall}"
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-root}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-${MYSQL_ROOT_PASSWORD:-root}}"

STATUS_ONLY=0; BASELINE=0
case "${1:-}" in
  --status)   STATUS_ONLY=1 ;;
  --baseline) BASELINE=1 ;;
  "")         ;;
  *) echo "未知参数：$1（可用：--status / --baseline）"; exit 2 ;;
esac

# ---------------------------------------------------------------- 连接
if command -v docker >/dev/null 2>&1 && docker exec "$MYSQL_CONTAINER" true 2>/dev/null; then
  MODE="容器 $MYSQL_CONTAINER"
  sql()      { docker exec -i "$MYSQL_CONTAINER" mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" \
                 --default-character-set=utf8mb4 -N -B -e "$1" "$MYSQL_DATABASE" 2>/dev/null; }
  sql_file() { docker exec -i "$MYSQL_CONTAINER" mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" \
                 --default-character-set=utf8mb4 "$MYSQL_DATABASE" < "$1" 2>&1 \
                 | grep -v "Using a password" || true; }
elif command -v mysql >/dev/null 2>&1; then
  MODE="本机客户端 → $MYSQL_HOST:$MYSQL_PORT"
  sql()      { mysql -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" \
                 --default-character-set=utf8mb4 -N -B -e "$1" "$MYSQL_DATABASE" 2>/dev/null; }
  sql_file() { mysql -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" \
                 --default-character-set=utf8mb4 "$MYSQL_DATABASE" < "$1" 2>&1 \
                 | grep -v "Using a password" || true; }
else
  echo "✗ 既没有可用的 MySQL 容器（$MYSQL_CONTAINER），本机也没有 mysql 客户端"
  echo "  起容器：docker compose -f deploy/docker-compose.dev.yml up -d mysql"
  exit 1
fi

if ! sql "SELECT 1" >/dev/null 2>&1; then
  echo "✗ 连不上数据库（$MODE，库 $MYSQL_DATABASE）"
  exit 1
fi

echo "==> 迁移（$MODE，库 $MYSQL_DATABASE）"

# ---------------------------------------------------------------- 记录表
sql "CREATE TABLE IF NOT EXISTS schema_migrations (
       filename   VARCHAR(190) NOT NULL PRIMARY KEY,
       applied_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
     ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='已应用的迁移'" >/dev/null

# 001 只对「导过初版 30 张表的老库」有意义。全新安装里它要补的东西
# initdb 已经建好了，跑它反而会因为重复定义报错，所以直接记为已应用。
if [ -z "$(sql "SELECT filename FROM schema_migrations WHERE filename LIKE '001_%'")" ]; then
  HAVE=$(sql "SELECT COUNT(*) FROM information_schema.columns
              WHERE table_schema='$MYSQL_DATABASE' AND table_name='knowledge_item'
                AND column_name='knowledge_type'")
  if [ "${HAVE:-0}" -ge 1 ]; then
    F=$(basename "$(ls "$MIGRATIONS"/001_*.sql | head -1)")
    [ "$STATUS_ONLY" = 0 ] && sql "INSERT IGNORE INTO schema_migrations(filename) VALUES('$F')" >/dev/null
    echo "  ⤼ $F（全新安装已内含，标记为已应用）"
  fi
fi

# ---------------------------------------------------------------- 逐个应用
APPLIED=0; SKIPPED=0; FAILED=0
for path in "$MIGRATIONS"/*.sql; do
  f="$(basename "$path")"
  if [ -n "$(sql "SELECT filename FROM schema_migrations WHERE filename='$f'")" ]; then
    SKIPPED=$((SKIPPED + 1)); continue
  fi
  if [ "$STATUS_ONLY" = 1 ]; then
    echo "  待应用  $f"; APPLIED=$((APPLIED + 1)); continue
  fi
  if [ "$BASELINE" = 1 ]; then
    sql "INSERT IGNORE INTO schema_migrations(filename) VALUES('$f')" >/dev/null
    echo "  ⤼ $f（基线：标记为已应用，未执行）"
    APPLIED=$((APPLIED + 1)); continue
  fi

  ERR="$(sql_file "$path")"
  if [ -n "$ERR" ] && echo "$ERR" | grep -qi "^ERROR"; then
    echo "  ✗ $f"
    echo "$ERR" | head -4 | sed 's/^/      /'
    # 「列/表已存在」几乎总是同一个原因：这个库之前是手工跑迁移的，
    # 结构已经对了，只是没有记录。直接把话说到位，省得人去猜
    if echo "$ERR" | grep -qiE "Duplicate column|already exists|Duplicate key name"; then
      echo
      echo "  这个库看起来之前是手工执行迁移的（结构已存在，只是没有记录）。"
      echo "  跑一次基线把现状登记下来，之后就能正常增量："
      echo "      ./deploy/scripts/migrate.sh --baseline"
    fi
    FAILED=1
    break
  fi
  sql "INSERT INTO schema_migrations(filename) VALUES('$f')" >/dev/null
  echo "  ✓ $f"
  APPLIED=$((APPLIED + 1))
done

echo
if [ "$FAILED" = 1 ]; then
  echo "❌ 迁移中断。修好上面那个错误后重跑本脚本，已应用的不会重复执行。"
  exit 1
fi
if [ "$STATUS_ONLY" = 1 ]; then
  echo "共 $APPLIED 个待应用，$SKIPPED 个已应用。"
elif [ "$BASELINE" = 1 ]; then
  echo "✅ 已把 $APPLIED 个迁移标记为已应用（未执行），跳过 $SKIPPED 个原本就有记录的。"
else
  echo "✅ 本次应用 $APPLIED 个，跳过 $SKIPPED 个已应用的。"
fi
