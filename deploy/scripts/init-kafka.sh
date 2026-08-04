#!/usr/bin/env bash
# 创建 Kafka topic。
#
# 分区数不是随便定的——它是显存约束的一部分：
# media.video.generate 与 train.job.request 必须单分区，用 Kafka 的分区语义
# 充当分布式锁，强制 GPU 独占任务串行执行，否则必然 OOM。
# 详见 docs/11-agent-cluster.md 与 docs/14-infra.md。
#
# 用法：./deploy/scripts/init-kafka.sh [broker]

set -euo pipefail

BROKER="${1:-localhost:29092}"
KAFKA_CONTAINER="${KAFKA_CONTAINER:-sm-kafka}"
RETENTION_MS=$((7 * 24 * 3600 * 1000))

# topic:分区数:说明
TOPICS=(
  "media.image.generate:3:图像生成任务"
  "media.video.generate:1:视频生成（GPU 独占，必须单分区）"
  "media.tts.generate:3:口播合成任务"
  "live.record.done:2:直播录制完成"
  "clip.done:3:切片完成"
  "asset.approved:3:素材审核通过"
  "data.clean.request:3:清洗任务"
  "kb.index.request:3:增量向量化"
  "trace.collected:6:对话 Trace 归集"
  "kpi.score.request:3:考核评分任务"
  "train.job.request:1:微调任务（GPU 独占，必须单分区）"
)

kafka_topics() {
  docker exec -i "$KAFKA_CONTAINER" /opt/kafka/bin/kafka-topics.sh "$@"
}

echo "==> 在 $BROKER 上创建 topic"

for entry in "${TOPICS[@]}"; do
  IFS=':' read -r topic partitions desc <<< "$entry"

  kafka_topics --bootstrap-server "$BROKER" \
    --create --if-not-exists \
    --topic "$topic" \
    --partitions "$partitions" \
    --replication-factor 1 \
    --config retention.ms=$RETENTION_MS >/dev/null

  # 每个业务 topic 配一个死信队列，重试 3 次后进入，由运营后台人工重投
  kafka_topics --bootstrap-server "$BROKER" \
    --create --if-not-exists \
    --topic "${topic}.dlq" \
    --partitions 1 \
    --replication-factor 1 \
    --config retention.ms=$((30 * 24 * 3600 * 1000)) >/dev/null

  printf '  ✓ %-28s 分区=%s  %s\n' "$topic" "$partitions" "$desc"
done

echo
echo "==> 校验 GPU 独占 topic 的分区数"
FAILED=0
for topic in media.video.generate train.job.request; do
  count=$(kafka_topics --bootstrap-server "$BROKER" --describe --topic "$topic" \
          | grep -c "Partition:" || true)
  if [ "$count" -ne 1 ]; then
    echo "  ✗ $topic 分区数为 $count，必须为 1，否则并发消费会 OOM"
    FAILED=1
  else
    echo "  ✓ $topic 单分区"
  fi
done

exit $FAILED
