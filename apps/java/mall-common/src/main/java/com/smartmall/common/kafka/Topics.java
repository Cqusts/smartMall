package com.smartmall.common.kafka;

import java.util.List;
import java.util.Set;

/**
 * Kafka Topic 契约。Java 侧与 Python 侧（{@code ai_common/kafka/topics.py}）必须保持一致。
 *
 * <p>{@link #GPU_EXCLUSIVE} 中的 topic 必须配置为 <b>单分区 + 单消费者</b>：
 * 它们对应独占 GPU 显存的任务（Wan2.2 视频生成 ~22G、QLoRA 训练 ~20G），
 * 并行消费必然 OOM。这里用 Kafka 的分区语义充当分布式锁，无需额外锁机制。
 * 详见 docs/11-agent-cluster.md 与 docs/14-infra.md。
 */
public final class Topics {

    private Topics() {
    }

    /** 图像生成任务 → ai-media */
    public static final String MEDIA_IMAGE_GENERATE = "media.image.generate";
    /** 视频生成任务 → ai-media（GPU 独占） */
    public static final String MEDIA_VIDEO_GENERATE = "media.video.generate";
    /** 口播合成任务 → ai-media */
    public static final String MEDIA_TTS_GENERATE = "media.tts.generate";

    /** 直播录制完成（SRS on_dvr 回调触发）→ ai-clip */
    public static final String LIVE_RECORD_DONE = "live.record.done";
    /** 切片完成 → mall-asset 注册素材 */
    public static final String CLIP_DONE = "clip.done";

    /** 素材审核通过 → mall-dataplat 消费，触发 VLM 打标与回灌 */
    public static final String ASSET_APPROVED = "asset.approved";

    /** 清洗任务 → 数据治理 Agent */
    public static final String DATA_CLEAN_REQUEST = "data.clean.request";
    /** 增量向量化 → ai-rag */
    public static final String KB_INDEX_REQUEST = "kb.index.request";

    /** 对话 Trace 归集 → mall-dataplat */
    public static final String TRACE_COLLECTED = "trace.collected";

    /** 考核评分任务 → ai-agent */
    public static final String KPI_SCORE_REQUEST = "kpi.score.request";

    /** 微调任务 → ai-train（GPU 独占） */
    public static final String TRAIN_JOB_REQUEST = "train.job.request";

    /** 死信队列后缀 */
    public static final String DLQ_SUFFIX = ".dlq";

    /**
     * 独占 GPU 的 topic —— 必须单分区单消费者。
     * {@code deploy/scripts/init-kafka.sh} 依据此清单创建 topic。
     */
    public static final Set<String> GPU_EXCLUSIVE = Set.of(
            MEDIA_VIDEO_GENERATE,
            TRAIN_JOB_REQUEST
    );

    public static final List<String> ALL = List.of(
            MEDIA_IMAGE_GENERATE, MEDIA_VIDEO_GENERATE, MEDIA_TTS_GENERATE,
            LIVE_RECORD_DONE, CLIP_DONE, ASSET_APPROVED,
            DATA_CLEAN_REQUEST, KB_INDEX_REQUEST, TRACE_COLLECTED,
            KPI_SCORE_REQUEST, TRAIN_JOB_REQUEST
    );

    public static String dlqOf(String topic) {
        return topic + DLQ_SUFFIX;
    }

    public static boolean isGpuExclusive(String topic) {
        return GPU_EXCLUSIVE.contains(topic);
    }
}
