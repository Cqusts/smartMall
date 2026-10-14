package com.smartmall.product.order;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 订单号生成器。
 *
 * <p><b>这组测试要钉住的回归：同一秒内大量下单不重号。</b>旧实现是「时间戳 + 6 位随机数」，
 * 同一秒 n 笔订单的碰撞概率约 n²/(2×10⁶)——200 笔时约 2%，所以随机压测里一次可能撞不上，
 * 一个"跑一遍看有没有撞"的测试几乎不会红。这里把时钟<b>冻结在同一秒</b>，
 * 直接数重号个数：旧实现在这个测试上会稳定失败（10 000 个里期望重号约 50 个），
 * 新实现按构造无碰撞，所以是 0。
 *
 * <p>全放进 @Nested，原因见 {@link OrderServiceTest} 类注释。
 */
class OrderNoGeneratorTest {

    /** 固定在 2026-10-14 10:00:00，整个测试期间"同一秒"。 */
    static final Clock FROZEN = Clock.fixed(
            Instant.parse("2026-10-14T02:00:00Z"), ZoneId.of("Asia/Shanghai"));

    @Nested
    @DisplayName("格式")
    class Format {

        @Test
        @DisplayName("20 位纯数字：14 位时间 + 2 位节点 + 4 位序号")
        void twentyDigits() {
            String no = new OrderNoGenerator(7, FROZEN).next();
            assertThat(no).matches("\\d{20}");
            assertThat(no).startsWith("20261014100000");
            assertThat(no.substring(14, 16)).isEqualTo("07");
        }

        @Test
        @DisplayName("格式必须兼容店铺页白名单 \\d{14,24} 与客服抠单号的 \\d{10,24}")
        void compatibleWithPythonSide() {
            String no = new OrderNoGenerator(99, FROZEN).next();
            assertThat(no).matches("\\d{14,24}").matches("\\d{10,24}");
        }

        @Test
        @DisplayName("序号溢出后仍是 4 位，不会撑出 21 位或带负号")
        void sequenceWrapKeepsWidth() {
            OrderNoGenerator g = new OrderNoGenerator(0, FROZEN);
            for (int i = 0; i < 25_000; i++) {
                assertThat(g.next()).hasSize(20).matches("\\d{20}");
            }
        }

        @Test
        @DisplayName("节点号越界直接拒绝启动，而不是悄悄生成畸形订单号")
        void rejectsBadNodeId() {
            assertThatThrownBy(() -> new OrderNoGenerator(100, FROZEN))
                    .isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> new OrderNoGenerator(-1, FROZEN))
                    .isInstanceOf(IllegalArgumentException.class);
        }

        @Test
        @DisplayName("未配置节点号时由主机/进程散列出来，落在 [0,100)")
        void derivedNodeInRange() {
            assertThat(OrderNoGenerator.derivedNodeId()).isBetween(0, 99);
        }
    }

    @Nested
    @DisplayName("唯一性")
    class Uniqueness {

        @Test
        @DisplayName("同一秒、同一节点，9999 个全部不重号（旧实现这里期望重号约 50 个）")
        void sameSecondSameNode() {
            OrderNoGenerator g = new OrderNoGenerator(3, FROZEN);
            Set<String> seen = new HashSet<>();
            for (int i = 0; i < 9_999; i++) {
                assertThat(seen.add(g.next()))
                        .as("第 %d 个订单号重复", i)
                        .isTrue();
            }
        }

        @Test
        @DisplayName("同一秒、不同节点，各 5000 个互不重叠")
        void sameSecondDifferentNodes() {
            OrderNoGenerator a = new OrderNoGenerator(1, FROZEN);
            OrderNoGenerator b = new OrderNoGenerator(2, FROZEN);
            Set<String> seen = new HashSet<>();
            for (int i = 0; i < 5_000; i++) {
                assertThat(seen.add(a.next())).isTrue();
                assertThat(seen.add(b.next())).isTrue();
            }
        }

        @Test
        @DisplayName("64 线程同时取号，同一秒内 9000 个全部不重号")
        void concurrentCallers() throws Exception {
            OrderNoGenerator g = new OrderNoGenerator(5, FROZEN);
            int threads = 64, perThread = 140;               // 64×140 = 8960 < 10⁴
            Set<String> seen = ConcurrentHashMap.newKeySet();
            ExecutorService pool = Executors.newFixedThreadPool(threads);
            CountDownLatch start = new CountDownLatch(1);
            CountDownLatch done = new CountDownLatch(threads);
            for (int t = 0; t < threads; t++) {
                pool.submit(() -> {
                    try {
                        start.await();
                        for (int i = 0; i < perThread; i++) {
                            seen.add(g.next());
                        }
                    } catch (InterruptedException ignored) {
                        Thread.currentThread().interrupt();
                    } finally {
                        done.countDown();
                    }
                });
            }
            start.countDown();
            done.await();
            pool.shutdown();
            // Set 会吞掉重复，所以出现重号时 size 只会偏小——比的就是"应有个数"
            assertThat(seen).hasSize(threads * perThread);
        }
    }
}
