package com.smartmall.product.order;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
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

import java.math.BigDecimal;
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
 * 履约与退款：发货 → 送达 → 确认收货，以及退款的申请 / 同意 / 驳回。
 *
 * <p>整份订单代码只有一条真正要命的不变式：<b>一笔订单的库存至多被回补一次。</b>
 * 到这一版为止有三条路径会回补——手动取消、超时回收、同意退款。三条路各自
 * 正确不够，它们两两之间也不能重叠，所以 {@link StockRestoredAtMostOnce}
 * 专门跨路径压这件事。
 */
@SpringBootTest
class OrderFulfillmentTest {

    @Autowired
    OrderService orderService;
    @Autowired
    SkuMapper skuMapper;
    @Autowired
    MallOrderMapper orderMapper;
    @Autowired
    JdbcTemplate jdbc;
    @Autowired
    ObjectMapper json;

    static final long USER = 1001L;
    static final long OTHER_USER = 2002L;

    @BeforeEach
    void reset() {
        jdbc.execute("DELETE FROM mall_order");
        jdbc.execute("DELETE FROM sku");
        jdbc.update("INSERT INTO sku (sku_no, product_id, spec, price, origin_price, stock, status)"
                + " VALUES ('S-F', 9001, '{\"尺码\":\"M\"}', 100.00, 150.00, 10, 'on_sale')");
    }

    OrderView place(int qty) {
        return orderService.place(new CreateOrderRequest(
                UUID.randomUUID().toString(), USER, "S-F", qty));
    }

    /** 下单并付款，返回订单号。多数履约测试从这里起步。 */
    String paid(int qty) {
        String no = place(qty).orderNo();
        orderService.pay(no, USER);
        return no;
    }

    int stock() {
        return skuMapper.findBySkuNo("S-F").getStock();
    }

    String statusOf(String orderNo) {
        return orderMapper.findByOrderNo(orderNo).getStatus();
    }

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
    @DisplayName("发货与收货")
    class Fulfillment {

        @Test
        @DisplayName("发货写入运单号与第一条物流轨迹")
        void ship_records_express_and_first_track() throws Exception {
            String no = paid(1);

            OrderView v = orderService.ship(no, "中通", "78123456789");

            assertThat(v.status()).isEqualTo("shipped");
            assertThat(v.expressCompany()).isEqualTo("中通");
            assertThat(v.expressNo()).isEqualTo("78123456789");

            JsonNode tracks = json.readTree(v.tracks());
            assertThat(tracks).hasSize(1);
            assertThat(tracks.get(0).get("desc").asText()).contains("中通");
            assertThat(tracks.get(0).get("ts").asText())
                    .as("时间格式要与 004 种子数据一致，客服照着念")
                    .matches("\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}");
        }

        @Test
        @DisplayName("送达追加第二条轨迹，不覆盖第一条")
        void deliver_appends_track() throws Exception {
            String no = paid(1);
            orderService.ship(no, "顺丰", "SF1001");

            OrderView v = orderService.deliver(no);

            assertThat(v.status()).isEqualTo("delivered");
            JsonNode tracks = json.readTree(v.tracks());
            assertThat(tracks).hasSize(2);
            assertThat(tracks.get(0).get("desc").asText()).contains("出库");
            assertThat(tracks.get(1).get("desc").asText()).contains("送达");
        }

        @Test
        @DisplayName("未支付的单不能发货")
        void unpaid_cannot_ship() {
            String no = place(1).orderNo();

            assertThatThrownBy(() -> orderService.ship(no, "中通", "X1"))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("不可发货");
            assertThat(statusOf(no)).isEqualTo("pending_payment");
        }

        @Test
        @DisplayName("不能重复发货——第二次会覆盖运单号，用户查到的就是错的")
        void cannot_ship_twice() {
            String no = paid(1);
            orderService.ship(no, "中通", "FIRST-1");

            assertThatThrownBy(() -> orderService.ship(no, "顺丰", "SECOND-2"))
                    .isInstanceOf(BizException.class);
            assertThat(orderMapper.findByOrderNo(no).getExpressNo()).isEqualTo("FIRST-1");
        }

        @Test
        @DisplayName("确认收货可以从 shipped 直接跳，不必等物流回调")
        void confirm_directly_from_shipped() {
            String no = paid(1);
            orderService.ship(no, "中通", "X1");

            OrderView v = orderService.confirmReceipt(no, USER);

            assertThat(v.status()).isEqualTo("completed");
        }

        @Test
        @DisplayName("确认收货也可以从 delivered 走")
        void confirm_from_delivered() {
            String no = paid(1);
            orderService.ship(no, "中通", "X1");
            orderService.deliver(no);

            assertThat(orderService.confirmReceipt(no, USER).status()).isEqualTo("completed");
        }

        @Test
        @DisplayName("没发货就确认收货会被拒")
        void cannot_confirm_before_shipping() {
            String no = paid(1);

            assertThatThrownBy(() -> orderService.confirmReceipt(no, USER))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("不可确认收货");
        }

        @Test
        @DisplayName("确认别人的收货被拒，口径与「订单不存在」一致")
        void cross_user_confirm_rejected() {
            String no = paid(1);
            orderService.ship(no, "中通", "X1");

            assertThatThrownBy(() -> orderService.confirmReceipt(no, OTHER_USER))
                    .isInstanceOf(BizException.class)
                    .extracting(e -> ((BizException) e).getErrorCode())
                    .isEqualTo(ErrorCode.ORDER_NOT_FOUND);
            assertThat(statusOf(no)).isEqualTo("shipped");
        }

        @Test
        @DisplayName("并发确认收货只成功一次")
        void concurrent_confirm_transitions_once() throws Exception {
            String no = paid(1);
            orderService.ship(no, "中通", "X1");
            AtomicInteger ok = new AtomicInteger();

            List<Future<Void>> fs = stampede(16, () -> {
                try {
                    orderService.confirmReceipt(no, USER);
                    ok.incrementAndGet();
                } catch (BizException ignored) {
                }
                return null;
            });
            for (Future<Void> f : fs) {
                f.get();
            }

            assertThat(ok.get()).isEqualTo(1);
            assertThat(statusOf(no)).isEqualTo("completed");
        }

        @Test
        @DisplayName("已发货的单不能被超时回收——它早就付过款了")
        void shipped_order_is_never_reclaimed() {
            String no = paid(2);
            orderService.ship(no, "中通", "X1");
            ageOrder(no, Duration.ofDays(30));

            assertThat(orderService.releaseExpired(Duration.ofMinutes(30), 100)).isZero();
            assertThat(stock()).isEqualTo(8);
        }
    }

    @Nested
    @DisplayName("退款")
    class Refund {

        @Test
        @DisplayName("申请退款只挂起等审核，不动钱也不动库存")
        void apply_does_not_move_money_or_stock() {
            String no = paid(3);
            assertThat(stock()).isEqualTo(7);

            OrderView v = orderService.applyRefund(no, USER, "尺码不合适");

            assertThat(v.status()).isEqualTo("refunding");
            assertThat(v.refundReason()).isEqualTo("尺码不合适");
            assertThat(stock()).as("审核通过前不能回补库存").isEqualTo(7);
        }

        @Test
        @DisplayName("同意退款才回补库存")
        void approve_restores_stock() {
            String no = paid(3);
            orderService.applyRefund(no, USER, "不想要了");

            OrderView v = orderService.approveRefund(no);

            assertThat(v.status()).isEqualTo("refunded");
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("驳回退款回到申请前的状态，已发货的仍然是已发货")
        void reject_restores_previous_status() {
            String no = paid(2);
            orderService.ship(no, "中通", "X1");
            orderService.applyRefund(no, USER, "不想要了");
            assertThat(statusOf(no)).isEqualTo("refunding");

            OrderView v = orderService.rejectRefund(no, "已出库，请走退货流程");

            assertThat(v.status())
                    .as("回到 shipped 而不是 paid——发没发货是客服照着答的事实")
                    .isEqualTo("shipped");
            assertThat(v.refundRejectReason()).isEqualTo("已出库，请走退货流程");
            assertThat(stock()).as("驳回不动库存").isEqualTo(8);
        }

        @Test
        @DisplayName("驳回之后可以再次申请")
        void can_reapply_after_rejection() {
            String no = paid(1);
            orderService.applyRefund(no, USER, "第一次");
            orderService.rejectRefund(no, "理由不充分");

            OrderView again = orderService.applyRefund(no, USER, "第二次，附上照片");

            assertThat(again.status()).isEqualTo("refunding");
            assertThat(again.refundReason()).isEqualTo("第二次，附上照片");
        }

        @Test
        @DisplayName("未支付的单不能申请退款，提示直接取消")
        void unpaid_cannot_refund() {
            String no = place(1).orderNo();

            assertThatThrownBy(() -> orderService.applyRefund(no, USER, "算了"))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("直接取消");
        }

        @Test
        @DisplayName("已取消的单不能申请退款——它从来没付过钱")
        void cancelled_cannot_refund() {
            String no = place(1).orderNo();
            orderService.cancel(no, USER);

            assertThatThrownBy(() -> orderService.applyRefund(no, USER, "退钱"))
                    .isInstanceOf(BizException.class);
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("重复申请被拒，不会重置审核状态")
        void duplicate_application_rejected() {
            String no = paid(1);
            orderService.applyRefund(no, USER, "原因一");

            assertThatThrownBy(() -> orderService.applyRefund(no, USER, "原因二"))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("已在退款审核中");
            assertThat(orderMapper.findByOrderNo(no).getRefundReason()).isEqualTo("原因一");
        }

        @Test
        @DisplayName("重复同意退款幂等——审核按钮会被点第二次")
        void approve_is_idempotent() {
            String no = paid(2);
            orderService.applyRefund(no, USER, "不想要了");
            orderService.approveRefund(no);

            OrderView again = orderService.approveRefund(no);

            assertThat(again.status()).isEqualTo("refunded");
            assertThat(again.idempotentHit()).isTrue();
            assertThat(stock()).as("绝不能回补两次").isEqualTo(10);
        }

        @Test
        @DisplayName("没有待审申请时同意/驳回都被拒")
        void cannot_review_without_application() {
            String no = paid(1);

            assertThatThrownBy(() -> orderService.approveRefund(no))
                    .hasMessageContaining("没有待审的退款申请");
            assertThatThrownBy(() -> orderService.rejectRefund(no, "无"))
                    .hasMessageContaining("没有待审的退款申请");
            assertThat(stock()).isEqualTo(9);
        }

        @Test
        @DisplayName("申请别人的订单退款被拒，口径与「订单不存在」一致")
        void cross_user_refund_rejected() {
            String no = paid(1);

            assertThatThrownBy(() -> orderService.applyRefund(no, OTHER_USER, "退钱"))
                    .isInstanceOf(BizException.class)
                    .extracting(e -> ((BizException) e).getErrorCode())
                    .isEqualTo(ErrorCode.ORDER_NOT_FOUND);
            assertThat(statusOf(no)).isEqualTo("paid");
        }

        @Test
        @DisplayName("退款金额由服务端取订单实付，不接受客户端指定")
        void refund_amount_comes_from_the_order() {
            String no = paid(3);
            orderService.applyRefund(no, USER, "全退");

            assertThat(orderMapper.findByOrderNo(no).getRefundAmount())
                    .isEqualByComparingTo(new BigDecimal("300.00"));
        }

        @Test
        @DisplayName("并发同意退款：只回补一次库存")
        void concurrent_approve_restores_once() throws Exception {
            String no = paid(4);
            orderService.applyRefund(no, USER, "不想要了");
            assertThat(stock()).isEqualTo(6);

            List<Future<Void>> fs = stampede(16, () -> {
                try {
                    orderService.approveRefund(no);
                } catch (BizException ignored) {
                }
                return null;
            });
            for (Future<Void> f : fs) {
                f.get();
            }

            assertThat(stock()).as("回补恰好一次；大于 10 就是凭空刷出了库存").isEqualTo(10);
        }
    }

    /**
     * 三条会回补库存的路径两两之间不能重叠。
     *
     * <p>各自正确不代表合起来正确：如果一笔单既能走取消又能走退款，
     * 库存就会被加回去两次。这里逐条堵死。
     */
    @Nested
    @DisplayName("库存至多回补一次（跨路径）")
    class StockRestoredAtMostOnce {

        @Test
        @DisplayName("取消过的单进不了退款流程")
        void cancel_then_refund_is_impossible() {
            String no = place(3).orderNo();
            orderService.cancel(no, USER);
            assertThat(stock()).isEqualTo(10);

            // 付款这条路也堵着（已有测试），退款这条路同样堵着
            assertThatThrownBy(() -> orderService.pay(no, USER))
                    .isInstanceOf(BizException.class);
            assertThatThrownBy(() -> orderService.applyRefund(no, USER, "退"))
                    .isInstanceOf(BizException.class);
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("退款完成的单不能再被取消")
        void refunded_then_cancel_is_impossible() {
            String no = paid(3);
            orderService.applyRefund(no, USER, "退");
            orderService.approveRefund(no);
            assertThat(stock()).isEqualTo(10);

            assertThatThrownBy(() -> orderService.cancel(no, USER))
                    .isInstanceOf(BizException.class);
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("退款完成的单不会被超时回收")
        void refunded_order_is_never_reclaimed() {
            String no = paid(3);
            orderService.applyRefund(no, USER, "退");
            orderService.approveRefund(no);
            ageOrder(no, Duration.ofDays(30));

            assertThat(orderService.releaseExpired(Duration.ofMinutes(30), 100)).isZero();
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("退款审核中的单不会被超时回收——它已经付过款了")
        void refunding_order_is_never_reclaimed() {
            String no = paid(3);
            orderService.applyRefund(no, USER, "退");
            ageOrder(no, Duration.ofDays(30));

            assertThat(orderService.releaseExpired(Duration.ofMinutes(30), 100)).isZero();
            assertThat(stock()).as("还没审完，不能提前回补").isEqualTo(7);
        }

        /**
         * 全生命周期跑一遍，断言库存在每一步都精确等于应有的值。
         * 单看每一步都对，连起来错位的情况这条能抓到。
         */
        @Test
        @DisplayName("走完整条链路，库存每一步都对得上")
        void full_lifecycle_keeps_stock_exact() {
            assertThat(stock()).isEqualTo(10);

            String no = place(2).orderNo();
            assertThat(stock()).as("下单预占").isEqualTo(8);

            orderService.pay(no, USER);
            assertThat(stock()).as("支付不动库存").isEqualTo(8);

            orderService.ship(no, "中通", "X1");
            assertThat(stock()).as("发货不动库存").isEqualTo(8);

            orderService.deliver(no);
            orderService.confirmReceipt(no, USER);
            assertThat(stock()).as("收货不动库存").isEqualTo(8);

            orderService.applyRefund(no, USER, "七天无理由");
            assertThat(stock()).as("申请退款不动库存").isEqualTo(8);

            orderService.approveRefund(no);
            assertThat(stock()).as("同意退款才回补").isEqualTo(10);

            // 终态之后任何操作都不该再改库存
            assertThatThrownBy(() -> orderService.cancel(no, USER))
                    .isInstanceOf(BizException.class);
            orderService.approveRefund(no);   // 幂等
            assertThat(orderService.releaseExpired(Duration.ofSeconds(0), 100)).isZero();
            assertThat(stock()).isEqualTo(10);
        }
    }
}
