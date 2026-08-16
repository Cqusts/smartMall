package com.smartmall.product.order;

import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.exception.BizException;
import com.smartmall.product.order.dto.CreateOrderRequest;
import com.smartmall.product.order.dto.OrderView;
import com.smartmall.product.order.mapper.MallOrderMapper;
import com.smartmall.product.order.mapper.SkuMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;

import java.time.Duration;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 订单生命周期：支付、超时释放，以及它们之间的竞态。
 *
 * <p>整个生命周期的正确性可以归结成一句话：<b>一笔订单的库存，最多被回补一次，
 * 且只有在它确实没被支付时才回补。</b>支付、手动取消、超时释放三条路都可能
 * 同时发生在同一笔订单上，而它们全部收敛到 {@code status = 'pending_payment'}
 * 这一个条件更新上——数据库替我们裁决谁赢。这个类就是在压这件事。
 */
@SpringBootTest
class OrderLifecycleTest {

    @Autowired
    OrderService orderService;
    @Autowired
    SkuMapper skuMapper;
    @Autowired
    MallOrderMapper orderMapper;
    @Autowired
    JdbcTemplate jdbc;

    static final long USER = 1001L;
    static final long OTHER_USER = 2002L;

    @BeforeEach
    void reset() {
        jdbc.execute("DELETE FROM mall_order");
        jdbc.execute("DELETE FROM sku");
        jdbc.update("INSERT INTO sku (sku_no, product_id, spec, price, origin_price, stock, status)"
                + " VALUES ('S-L', 9001, '{\"尺码\":\"M\"}', 100.00, 150.00, 10, 'on_sale')");
    }

    OrderView place(int qty) {
        return orderService.place(new CreateOrderRequest(
                UUID.randomUUID().toString(), USER, "S-L", qty));
    }

    int stock() {
        return skuMapper.findBySkuNo("S-L").getStock();
    }

    String statusOf(String orderNo) {
        return orderMapper.findByOrderNo(orderNo).getStatus();
    }

    /** 把订单的下单时间往前拨，模拟"它是 N 分钟前下的"。 */
    void ageOrder(String orderNo, Duration by) {
        jdbc.update("UPDATE mall_order SET created_at = ? WHERE order_no = ?",
                java.sql.Timestamp.valueOf(java.time.LocalDateTime.now().minus(by)), orderNo);
    }

    <T> List<Future<T>> stampede(int n, Callable<T> task) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(n);
        CountDownLatch ready = new CountDownLatch(n);
        CountDownLatch go = new CountDownLatch(1);
        try {
            List<Future<T>> futures = new java.util.ArrayList<>(n);
            for (int i = 0; i < n; i++) {
                futures.add(pool.submit(() -> {
                    ready.countDown();
                    go.await();
                    return task.call();
                }));
            }
            assertThat(ready.await(30, TimeUnit.SECONDS)).isTrue();
            go.countDown();
            pool.shutdown();
            assertThat(pool.awaitTermination(120, TimeUnit.SECONDS)).isTrue();
            return futures;
        } finally {
            pool.shutdownNow();
        }
    }

    @Nested
    @DisplayName("支付")
    class Pay {

        @Test
        @DisplayName("支付把订单推到 paid，库存不再变动")
        void marks_paid() {
            OrderView v = place(3);
            assertThat(stock()).isEqualTo(7);

            OrderView paid = orderService.pay(v.orderNo(), USER);

            assertThat(paid.status()).isEqualTo("paid");
            assertThat(paid.idempotentHit()).isFalse();
            assertThat(stock()).as("支付不该再动库存，下单时已经扣过").isEqualTo(7);
        }

        @Test
        @DisplayName("重复支付返回成功而不是报错——支付回调会重试")
        void repeated_payment_is_idempotent() {
            OrderView v = place(1);
            orderService.pay(v.orderNo(), USER);

            OrderView again = orderService.pay(v.orderNo(), USER);

            assertThat(again.status()).isEqualTo("paid");
            assertThat(again.idempotentHit()).isTrue();
            assertThat(stock()).isEqualTo(9);
        }

        @Test
        @DisplayName("已取消的订单不能被支付——库存已经还回去了，收钱就是超卖")
        void cancelled_order_cannot_be_paid() {
            OrderView v = place(2);
            orderService.cancel(v.orderNo(), USER);
            assertThat(stock()).isEqualTo(10);

            assertThatThrownBy(() -> orderService.pay(v.orderNo(), USER))
                    .isInstanceOf(BizException.class)
                    .extracting(e -> ((BizException) e).getErrorCode())
                    .isEqualTo(ErrorCode.ORDER_STATE_ILLEGAL);

            assertThat(statusOf(v.orderNo())).isEqualTo("cancelled");
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("已支付的订单不能再取消——取消会回补库存，那是白送一件货")
        void paid_order_cannot_be_cancelled() {
            OrderView v = place(2);
            orderService.pay(v.orderNo(), USER);

            assertThatThrownBy(() -> orderService.cancel(v.orderNo(), USER))
                    .isInstanceOf(BizException.class);
            assertThat(stock()).isEqualTo(8);
        }

        @Test
        @DisplayName("支付别人的订单被拒，口径与「订单不存在」一致")
        void cross_user_pay_is_rejected() {
            OrderView v = place(1);

            assertThatThrownBy(() -> orderService.pay(v.orderNo(), OTHER_USER))
                    .isInstanceOf(BizException.class)
                    .extracting(e -> ((BizException) e).getErrorCode())
                    .isEqualTo(ErrorCode.ORDER_NOT_FOUND);

            assertThat(statusOf(v.orderNo())).isEqualTo("pending_payment");
        }

        @Test
        @DisplayName("并发支付同一张单：只有一次真正流转，其余都是幂等返回")
        void concurrent_payment_transitions_once() throws Exception {
            OrderView v = place(1);
            AtomicInteger fresh = new AtomicInteger();
            AtomicInteger idempotent = new AtomicInteger();

            List<Future<Void>> fs = stampede(20, () -> {
                OrderView r = orderService.pay(v.orderNo(), USER);
                (r.idempotentHit() ? idempotent : fresh).incrementAndGet();
                return null;
            });
            for (Future<Void> f : fs) {
                f.get();
            }

            assertThat(fresh.get()).as("真正完成流转的只能有一次").isEqualTo(1);
            assertThat(idempotent.get()).isEqualTo(19);
            assertThat(statusOf(v.orderNo())).isEqualTo("paid");
            assertThat(stock()).isEqualTo(9);
        }
    }

    @Nested
    @DisplayName("超时释放")
    class ReleaseExpired {

        @Test
        @DisplayName("超期未支付的单被取消，库存回补")
        void expired_order_is_released() {
            OrderView v = place(4);
            assertThat(stock()).isEqualTo(6);
            ageOrder(v.orderNo(), Duration.ofMinutes(31));

            int released = orderService.releaseExpired(Duration.ofMinutes(30), 100);

            assertThat(released).isEqualTo(1);
            assertThat(statusOf(v.orderNo())).isEqualTo("cancelled");
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("没到时间的单不动——回收窗口不能提前，用户还在付款页上")
        void fresh_order_is_untouched() {
            OrderView v = place(4);
            ageOrder(v.orderNo(), Duration.ofMinutes(5));

            int released = orderService.releaseExpired(Duration.ofMinutes(30), 100);

            assertThat(released).isZero();
            assertThat(statusOf(v.orderNo())).isEqualTo("pending_payment");
            assertThat(stock()).isEqualTo(6);
        }

        @Test
        @DisplayName("已支付的单永远不会被回收，哪怕它很旧")
        void paid_order_is_never_released() {
            OrderView v = place(3);
            orderService.pay(v.orderNo(), USER);
            ageOrder(v.orderNo(), Duration.ofDays(30));

            int released = orderService.releaseExpired(Duration.ofMinutes(30), 100);

            assertThat(released).isZero();
            assertThat(statusOf(v.orderNo())).isEqualTo("paid");
            assertThat(stock()).as("已付款的货不能被还回库存").isEqualTo(7);
        }

        @Test
        @DisplayName("已取消的单不会被再回收一次")
        void cancelled_order_is_not_released_again() {
            OrderView v = place(3);
            orderService.cancel(v.orderNo(), USER);
            ageOrder(v.orderNo(), Duration.ofHours(2));

            assertThat(orderService.releaseExpired(Duration.ofMinutes(30), 100)).isZero();
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("跑两遍不会回补两次——任务每分钟都在跑，幂等是硬要求")
        void running_twice_restores_once() {
            OrderView v = place(5);
            ageOrder(v.orderNo(), Duration.ofHours(1));

            assertThat(orderService.releaseExpired(Duration.ofMinutes(30), 100)).isEqualTo(1);
            assertThat(orderService.releaseExpired(Duration.ofMinutes(30), 100)).isZero();
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("batchSize 给单轮工作量封顶，剩下的留给下一轮")
        void batch_size_caps_one_round() {
            for (int i = 0; i < 5; i++) {
                ageOrder(place(1).orderNo(), Duration.ofHours(1));
            }
            assertThat(stock()).isEqualTo(5);

            assertThat(orderService.releaseExpired(Duration.ofMinutes(30), 2)).isEqualTo(2);
            assertThat(stock()).isEqualTo(7);

            assertThat(orderService.releaseExpired(Duration.ofMinutes(30), 100)).isEqualTo(3);
            assertThat(stock()).isEqualTo(10);
        }

        /**
         * 多实例场景：两个 mall-product 的定时任务会扫到同一批订单。
         * 这里不加分布式锁，靠 markCancelled 的条件更新裁决——所以并发跑
         * 多轮释放，总释放数必须恰好等于订单数，库存恰好回到初始值。
         */
        @Test
        @DisplayName("多个实例同时释放：总共只释放一次，库存不会被加两遍")
        void concurrent_releases_restore_exactly_once() throws Exception {
            for (int i = 0; i < 5; i++) {
                ageOrder(place(1).orderNo(), Duration.ofHours(1));
            }
            assertThat(stock()).isEqualTo(5);

            AtomicInteger total = new AtomicInteger();
            List<Future<Void>> fs = stampede(8, () -> {
                total.addAndGet(orderService.releaseExpired(Duration.ofMinutes(30), 100));
                return null;
            });
            for (Future<Void> f : fs) {
                f.get();
            }

            assertThat(total.get()).as("8 个实例并发，总释放数仍应是 5").isEqualTo(5);
            assertThat(stock()).as("库存恰好回到 10；大于 10 就是被回补了多次").isEqualTo(10);
        }
    }

    @Nested
    @DisplayName("预占到期时刻")
    class ExpiresAt {

        @Test
        @DisplayName("待支付订单带 expiresAt，且等于下单时间 + 配置的 TTL")
        void pending_order_carries_deadline() {
            OrderView v = place(1);

            assertThat(v.expiresAt()).isNotNull();
            assertThat(v.expiresAt())
                    .as("到期时刻必须由配置算出，不能是写死的 30 分钟")
                    .isEqualTo(v.createdAt().plus(orderService.paymentTtl()));
        }

        @Test
        @DisplayName("非待支付状态没有 expiresAt——已支付/已取消谈不上到期")
        void terminal_states_have_no_deadline() {
            OrderView paid = orderService.pay(place(1).orderNo(), USER);
            assertThat(paid.expiresAt()).isNull();

            OrderView cancelled = orderService.cancel(place(1).orderNo(), USER);
            assertThat(cancelled.expiresAt()).isNull();
        }

        /**
         * 前端拿 expiresAt 显示「几点前未支付将自动释放」，回收任务拿同一个
         * TTL 决定几点动手。两者必须是同一个值 —— 各配一份的话，页面说
         * 还有 30 分钟而任务 15 分钟就把单收走了，用户看着倒计时单没了。
         */
        @Test
        @DisplayName("出参的到期时刻与回收任务用的是同一个 TTL")
        void deadline_agrees_with_the_reclaimer() {
            OrderView v = place(1);
            Duration ttl = orderService.paymentTtl();

            // 拨到"刚好还差一秒到期"：不该被回收
            ageOrder(v.orderNo(), ttl.minusSeconds(1));
            assertThat(orderService.releaseExpired()).isZero();
            assertThat(statusOf(v.orderNo())).isEqualTo("pending_payment");

            // 拨过到期点：该被回收
            ageOrder(v.orderNo(), ttl.plusSeconds(1));
            assertThat(orderService.releaseExpired()).isEqualTo(1);
            assertThat(statusOf(v.orderNo())).isEqualTo("cancelled");
        }
    }

    @Nested
    @DisplayName("支付与超时释放的竞态")
    class PayVsRelease {

        /**
         * <b>这是整条链路上最危险的一个时刻。</b>用户在超时那一秒点了支付，
         * 而回收任务同时判定它超期。两者必须恰好成功一个：
         *
         * <ul>
         *   <li>支付赢 → 订单 paid，库存保持已扣减（货是他的）</li>
         *   <li>回收赢 → 订单 cancelled，库存回补，支付**必须报错**</li>
         * </ul>
         *
         * 最坏的结果是两个都"成功"：用户付了钱，而货被还回了库存卖给别人。
         */
        @Test
        @DisplayName("同一瞬间支付与超时回收：恰好一个成功，库存与状态自洽")
        void exactly_one_of_pay_and_release_wins() throws Exception {
            for (int round = 0; round < 15; round++) {
                jdbc.execute("DELETE FROM mall_order");
                jdbc.update("UPDATE sku SET stock = 10 WHERE sku_no = 'S-L'");

                OrderView v = place(1);
                ageOrder(v.orderNo(), Duration.ofHours(1));
                assertThat(stock()).isEqualTo(9);

                AtomicInteger paidOk = new AtomicInteger();
                AtomicInteger releasedCount = new AtomicInteger();

                List<Future<Void>> fs = stampede(2, new Callable<Void>() {
                    final AtomicInteger seat = new AtomicInteger();

                    @Override
                    public Void call() {
                        if (seat.getAndIncrement() == 0) {
                            try {
                                orderService.pay(v.orderNo(), USER);
                                paidOk.incrementAndGet();
                            } catch (BizException ignored) {
                                // 被回收抢先了，正确行为
                            }
                        } else {
                            releasedCount.addAndGet(
                                    orderService.releaseExpired(Duration.ofMinutes(30), 100));
                        }
                        return null;
                    }
                });
                for (Future<Void> f : fs) {
                    f.get();
                }

                String status = statusOf(v.orderNo());
                assertThat(paidOk.get() + releasedCount.get())
                        .as("第 %d 轮：支付与回收必须恰好成功一个（status=%s）", round, status)
                        .isEqualTo(1);

                if (paidOk.get() == 1) {
                    assertThat(status).isEqualTo("paid");
                    assertThat(stock()).as("付了钱，货就是他的，库存不回补").isEqualTo(9);
                } else {
                    assertThat(status).isEqualTo("cancelled");
                    assertThat(stock()).as("回收赢了就必须回补").isEqualTo(10);
                }
            }
        }
    }
}
