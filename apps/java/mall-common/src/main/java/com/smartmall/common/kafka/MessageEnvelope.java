package com.smartmall.common.kafka;

import com.fasterxml.jackson.annotation.JsonInclude;

import java.time.Instant;
import java.util.UUID;

/**
 * Kafka 消息统一信封。所有 topic 的消息体都是这个形状，payload 才是各自的业务载荷。
 *
 * <p>{@code msgId} 用于消费端幂等去重（Redis SETNX，TTL 24h）；
 * {@code traceId} 用于跨服务串联同一次业务流程；
 * {@code retryCount} 达到 3 次后进入 {@code {topic}.dlq}。
 *
 * <p>与 Python 侧 {@code ai_common.kafka.MessageEnvelope} 保持同一 JSON 形状。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record MessageEnvelope<T>(
        String msgId,
        String topic,
        String traceId,
        String producedBy,
        Instant producedAt,
        int retryCount,
        T payload
) {

    public static <T> MessageEnvelope<T> of(String topic, String producedBy, T payload) {
        return new MessageEnvelope<>(
                UUID.randomUUID().toString(), topic, null, producedBy, Instant.now(), 0, payload);
    }

    public static <T> MessageEnvelope<T> of(String topic, String producedBy, String traceId, T payload) {
        return new MessageEnvelope<>(
                UUID.randomUUID().toString(), topic, traceId, producedBy, Instant.now(), 0, payload);
    }

    /** 重试时保留原 msgId，使消费端的幂等判断仍然成立。 */
    public MessageEnvelope<T> nextRetry() {
        return new MessageEnvelope<>(msgId, topic, traceId, producedBy, producedAt, retryCount + 1, payload);
    }

    public boolean exhausted(int maxRetry) {
        return retryCount >= maxRetry;
    }
}
