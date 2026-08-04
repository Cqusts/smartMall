"""Kafka Topic 契约。

与 Java 侧 ``com.smartmall.common.kafka.Topics`` 必须保持一致——
``deploy/scripts/check-contracts.sh`` 会对比两边的常量，不一致则 CI 失败。

``GPU_EXCLUSIVE`` 中的 topic 必须配置为 **单分区 + 单消费者**：
它们对应独占 GPU 显存的任务（Wan2.2 视频生成 ~22G、QLoRA 训练 ~20G），
并行消费必然 OOM。这里用 Kafka 的分区语义充当分布式锁，无需额外锁机制。
详见 docs/11-agent-cluster.md 与 docs/14-infra.md。
"""

from __future__ import annotations

from typing import Final

# ---- 素材生成 ----
MEDIA_IMAGE_GENERATE: Final = "media.image.generate"
MEDIA_VIDEO_GENERATE: Final = "media.video.generate"  # GPU 独占
MEDIA_TTS_GENERATE: Final = "media.tts.generate"

# ---- 直播切片 ----
LIVE_RECORD_DONE: Final = "live.record.done"
CLIP_DONE: Final = "clip.done"

# ---- 素材与数据中台 ----
ASSET_APPROVED: Final = "asset.approved"
DATA_CLEAN_REQUEST: Final = "data.clean.request"
KB_INDEX_REQUEST: Final = "kb.index.request"
TRACE_COLLECTED: Final = "trace.collected"

# ---- 考核与训练 ----
KPI_SCORE_REQUEST: Final = "kpi.score.request"
TRAIN_JOB_REQUEST: Final = "train.job.request"  # GPU 独占

DLQ_SUFFIX: Final = ".dlq"

GPU_EXCLUSIVE: Final[frozenset[str]] = frozenset(
    {MEDIA_VIDEO_GENERATE, TRAIN_JOB_REQUEST}
)

ALL: Final[tuple[str, ...]] = (
    MEDIA_IMAGE_GENERATE,
    MEDIA_VIDEO_GENERATE,
    MEDIA_TTS_GENERATE,
    LIVE_RECORD_DONE,
    CLIP_DONE,
    ASSET_APPROVED,
    DATA_CLEAN_REQUEST,
    KB_INDEX_REQUEST,
    TRACE_COLLECTED,
    KPI_SCORE_REQUEST,
    TRAIN_JOB_REQUEST,
)


def dlq_of(topic: str) -> str:
    return topic + DLQ_SUFFIX


def is_gpu_exclusive(topic: str) -> bool:
    return topic in GPU_EXCLUSIVE
