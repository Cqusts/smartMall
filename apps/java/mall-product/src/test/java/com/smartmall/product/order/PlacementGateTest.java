package com.smartmall.product.order;

import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.exception.BizException;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 下单舱壁。
 *
 * <p>它要防的是"一个爆款拖垮整个店"：热点 SKU 的下单在同一行库存上串行，几百个请求各占一条
 * 数据库连接等行锁，连接池（10 条）被占满，别的商品的下单也拿不到连接。
 * 真库实测路人买别的商品 p50 从十几毫秒变成 1.5 秒、最长 10.1 秒。
 *
 * <p>全放进 @Nested，原因见 {@link OrderServiceTest} 类注释。
 */
class PlacementGateTest {

    static PlacementGate gate(int perSku, int total, long waitMs) {
        return new PlacementGate(perSku, total, Duration.ofMillis(waitMs));
    }

    @Nested
    @DisplayName("每个 SKU 的并发上限")
    class PerSku {

        @Test
        @DisplayName("同一 SKU 上同时在里面的人数永远不超过上限（50 线程抢，上限 3）")
        void neverExceedsLimitForOneSku() throws Exception {
            PlacementGate g = gate(3, 100, 5_000);          // 等得够久，让所有人最终都进得来
            AtomicInteger inside = new AtomicInteger(), peak = new AtomicInteger(), done = new AtomicInteger();
            ExecutorService pool = Executors.newFixedThreadPool(50);
            CountDownLatch start = new CountDownLatch(1);
            for (int i = 0; i < 50; i++) {
                pool.submit(() -> {
                    try {
                        start.await();
                        try (PlacementGate.Permit p = g.enter("HOT")) {
                            int now = inside.incrementAndGet();
                            peak.accumulateAndGet(now, Math::max);
                            Thread.sleep(5);
                            inside.decrementAndGet();
                            done.incrementAndGet();
                        }
                    } catch (Exception ignored) {
                    }
                });
            }
            start.countDown();
            pool.shutdown();
            assertThat(pool.awaitTermination(30, TimeUnit.SECONDS)).isTrue();

            assertThat(done.get()).isEqualTo(50);
            assertThat(peak.get()).as("同时在里面的峰值").isLessThanOrEqualTo(3).isGreaterThan(1);
        }

        @Test
        @DisplayName("不同 SKU 互不影响：一个爆款占满自己的名额，别的商品照常进")
        void differentSkusAreIndependent() {
            PlacementGate g = gate(1, 100, 50);
            try (PlacementGate.Permit hot = g.enter("HOT")) {
                assertThatThrownBy(() -> g.enter("HOT")).isInstanceOf(BizException.class);
                try (PlacementGate.Permit other = g.enter("OTHER")) {
                    assertThat(other).isNotNull();          // 这才是舱壁的意义
                }
            }
        }
    }

    @Nested
    @DisplayName("全局并发上限")
    class Total {

        @Test
        @DisplayName("几个爆款同时来，各自没超限，合计超过全局上限时也要拒绝——否则照样占满连接池")
        void globalCapAppliesAcrossSkus() {
            PlacementGate g = gate(10, 2, 50);
            try (PlacementGate.Permit a = g.enter("A"); PlacementGate.Permit b = g.enter("B")) {
                assertThatThrownBy(() -> g.enter("C")).isInstanceOf(BizException.class);
            }
        }

        @Test
        @DisplayName("拿到每-SKU 许可却没拿到全局许可时，必须把前者还回去（否则每拒绝一次就永久漏一个）")
        void doesNotLeakPerSkuPermitWhenGlobalIsFull() {
            PlacementGate g = gate(1, 1, 30);
            try (PlacementGate.Permit a = g.enter("A")) {
                for (int i = 0; i < 5; i++) {
                    assertThatThrownBy(() -> g.enter("B")).isInstanceOf(BizException.class);
                }
                assertThat(g.availableFor("B")).as("B 的许可没有被拒绝吞掉").isEqualTo(1);
            }
            try (PlacementGate.Permit b = g.enter("B")) {
                assertThat(b).as("全局有空位了，B 必须进得来").isNotNull();
            }
        }
    }

    @Nested
    @DisplayName("获取顺序")
    class AcquireOrder {

        @Test
        @DisplayName("先拿每-SKU、后拿全局：在热点上排队的请求不攥着全局许可，别的商品照常进")
        void queuedHotRequestsDoNotHoldGlobalPermits() throws Exception {
            // 全局只有 2 个许可。热点 HOT 占了 1 个，另一个请求在 HOT 上排队。
            // 若排队的请求先拿了全局许可再等每-SKU 许可，第 2 个全局许可就被它攥住了，
            // OTHER 拿不到——一个爆款的排队者把别的商品也饿死，正是舱壁要避免的事。
            PlacementGate g = gate(1, 2, 1_500);
            ExecutorService pool = Executors.newSingleThreadExecutor();
            try (PlacementGate.Permit hot = g.enter("HOT")) {
                CountDownLatch waiting = new CountDownLatch(1);
                pool.submit(() -> {
                    waiting.countDown();
                    try (PlacementGate.Permit p = g.enter("HOT")) {
                        return null;
                    } catch (BizException ignored) {
                        return null;
                    }
                });
                waiting.await();
                Thread.sleep(150);                                  // 确保它已经卡在 HOT 的许可上

                long t0 = System.nanoTime();
                try (PlacementGate.Permit other = g.enter("OTHER")) {
                    long ms = (System.nanoTime() - t0) / 1_000_000;
                    assertThat(ms).as("OTHER 不该被 HOT 的排队者拖住").isLessThan(300);
                }
            }
            pool.shutdownNow();
        }
    }

    @Nested
    @DisplayName("等待与拒绝")
    class WaitAndReject {

        @Test
        @DisplayName("拿不到许可：等满 max-wait 后抛 RATE_LIMITED，而不是立刻拒绝也不是无限等")
        void rejectsAfterWaitingRoughlyMaxWait() {
            PlacementGate g = gate(1, 10, 150);
            try (PlacementGate.Permit hold = g.enter("X")) {
                long t0 = System.nanoTime();
                assertThatThrownBy(() -> g.enter("X"))
                        .isInstanceOfSatisfying(BizException.class,
                                e -> assertThat(e.getErrorCode()).isEqualTo(ErrorCode.RATE_LIMITED));
                long ms = (System.nanoTime() - t0) / 1_000_000;
                assertThat(ms).isBetween(120L, 1000L);
            }
        }

        @Test
        @DisplayName("等待期间许可被释放：成功进入——max-wait 的用途是抹平突发，不是一概拒绝")
        void succeedsIfAPermitFreesUpWithinTheWait() throws Exception {
            PlacementGate g = gate(1, 10, 2_000);
            PlacementGate.Permit hold = g.enter("X");
            ExecutorService pool = Executors.newSingleThreadExecutor();
            var f = pool.submit(() -> {
                try (PlacementGate.Permit p = g.enter("X")) {
                    return true;
                }
            });
            Thread.sleep(100);
            hold.close();
            assertThat(f.get(5, TimeUnit.SECONDS)).isTrue();
            pool.shutdown();
        }

        @Test
        @DisplayName("被拒绝的次数被计数，运维能看出「是不是在限流」")
        void countsRejections() {
            PlacementGate g = gate(1, 10, 10);
            try (PlacementGate.Permit hold = g.enter("X")) {
                for (int i = 0; i < 4; i++) {
                    try {
                        g.enter("X");
                    } catch (BizException ignored) {
                    }
                }
            }
            assertThat(g.rejected()).isEqualTo(4);
        }

        @Test
        @DisplayName("线程在等待中被中断：当作拒绝，并保留中断标志")
        void interruptIsRejectedAndPreserved() throws Exception {
            PlacementGate g = gate(1, 10, 5_000);
            PlacementGate.Permit hold = g.enter("X");
            boolean[] flag = new boolean[1];
            Thread t = new Thread(() -> {
                try {
                    g.enter("X");
                } catch (BizException e) {
                    flag[0] = Thread.currentThread().isInterrupted();
                }
            });
            t.start();
            Thread.sleep(100);
            t.interrupt();
            t.join(5_000);
            assertThat(flag[0]).as("中断标志必须保留，上层才有机会响应").isTrue();
            hold.close();
        }
    }

    @Nested
    @DisplayName("许可的归还")
    class Release {

        @Test
        @DisplayName("close 幂等：重复关闭不会多还一个许可（多还等于悄悄放宽了上限）")
        void closeIsIdempotent() {
            PlacementGate g = gate(2, 2, 10);
            PlacementGate.Permit p = g.enter("X");
            p.close();
            p.close();
            p.close();
            assertThat(g.availableTotal()).isEqualTo(2);
            assertThat(g.availableFor("X")).isEqualTo(2);
        }

        @Test
        @DisplayName("事务体抛异常时也归还（try-with-resources）")
        void releasedWhenBodyThrows() {
            PlacementGate g = gate(1, 1, 10);
            assertThatThrownBy(() -> {
                try (PlacementGate.Permit p = g.enter("X")) {
                    throw new IllegalStateException("扣库存失败");
                }
            }).isInstanceOf(IllegalStateException.class);
            assertThat(g.availableTotal()).isEqualTo(1);
            assertThat(g.availableFor("X")).isEqualTo(1);
        }

        @Test
        @DisplayName("大量进出之后许可数回到初值——没有泄漏")
        void noLeakAfterManyCycles() throws Exception {
            PlacementGate g = gate(2, 4, 50);
            ExecutorService pool = Executors.newFixedThreadPool(16);
            List<java.util.concurrent.Future<?>> fs = new ArrayList<>();
            for (int i = 0; i < 400; i++) {
                String sku = "S" + (i % 3);
                fs.add(pool.submit(() -> {
                    try (PlacementGate.Permit p = g.enter(sku)) {
                        Thread.sleep(1);
                    } catch (BizException | InterruptedException ignored) {
                    }
                }));
            }
            for (var f : fs) {
                f.get(30, TimeUnit.SECONDS);
            }
            pool.shutdown();
            assertThat(g.availableTotal()).isEqualTo(4);
            for (int i = 0; i < 3; i++) {
                assertThat(g.availableFor("S" + i)).isEqualTo(2);
            }
        }
    }

    @Nested
    @DisplayName("配置校验")
    class Config {

        @Test
        @DisplayName("上限配成 0 或负数直接拒绝启动——0 会让所有下单都被拒绝")
        void rejectsNonPositiveLimits() {
            assertThatThrownBy(() -> gate(0, 8, 100)).isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> gate(4, 0, 100)).isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> gate(-1, 8, 100)).isInstanceOf(IllegalArgumentException.class);
        }
    }
}
