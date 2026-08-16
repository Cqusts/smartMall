package com.smartmall.product.order;

import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.exception.BizException;
import com.smartmall.product.order.dto.CreateOrderRequest;
import com.smartmall.product.order.dto.OrderView;
import com.smartmall.product.order.entity.Sku;
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
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 下单链路的功能测试。并发超卖单独在 {@link OrderConcurrencyTest}。
 *
 * <p><b>约定：所有 @Test 都写在 @Nested 类里，不直接挂在外层类上。</b>
 * 原因是报告可读性——Surefire 3.2.5 在外层类含 @Nested 时，外层自己那一行
 * 会显示 {@code Tests run: 0}，而外层测试的计数被并进某个 @Nested 的那一行。
 * 测试**确实执行了**（拿一个必然失败的方法验证过：构建会 FAILURE），只是
 * 数字对不上号，排查失败时容易找错类。全放进 @Nested 就没有这个歧义。
 */
@SpringBootTest
class OrderServiceTest {

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
                + " VALUES ('S-A', 9001, '{\"尺码\":\"M\"}', 299.00, 399.00, 10, 'on_sale')");
        jdbc.update("INSERT INTO sku (sku_no, product_id, spec, price, origin_price, stock, status)"
                + " VALUES ('S-EMPTY', 9001, '{\"尺码\":\"L\"}', 299.00, 399.00, 0, 'on_sale')");
        jdbc.update("INSERT INTO sku (sku_no, product_id, spec, price, origin_price, stock, status)"
                + " VALUES ('S-OFF', 9001, '{\"尺码\":\"S\"}', 299.00, 399.00, 5, 'sold_out')");
    }

    static CreateOrderRequest req(String skuNo, int qty) {
        return new CreateOrderRequest(UUID.randomUUID().toString(), USER, skuNo, qty);
    }

    int stockOf(String skuNo) {
        return skuMapper.findBySkuNo(skuNo).getStock();
    }

    @Nested
    @DisplayName("下单")
    class Place {

        @Test
        @DisplayName("成功下单会扣掉对应数量的库存，且订单落到待支付")
        void deducts_stock_and_creates_pending_order() {
            OrderView v = orderService.place(req("S-A", 3));

            assertThat(v.orderNo()).isNotBlank();
            assertThat(v.status()).isEqualTo("pending_payment");
            assertThat(v.quantity()).isEqualTo(3);
            assertThat(v.idempotentHit()).isFalse();
            assertThat(stockOf("S-A")).isEqualTo(7);
        }

        @Test
        @DisplayName("金额由服务端按 SKU 价格算，客户端无从干预")
        void amount_is_computed_server_side() {
            // 请求体里根本没有 price 字段——这条测试真正锁住的是那个「缺席」：
            // 哪天有人为了图方便给 CreateOrderRequest 加上 price，这里会挂
            OrderView v = orderService.place(req("S-A", 3));
            assertThat(v.amount()).isEqualByComparingTo(new BigDecimal("897.00"));
        }

        @Test
        @DisplayName("规格是下单那一刻的快照，改了 SKU 也不会改写历史订单")
        void spec_is_snapshotted() {
            OrderView v = orderService.place(req("S-A", 1));
            jdbc.update("UPDATE sku SET spec = '{\"尺码\":\"XXL\"}' WHERE sku_no = 'S-A'");

            assertThat(orderMapper.findByOrderNo(v.orderNo()).getSpec()).contains("M");
        }

        @Test
        @DisplayName("库存不足时下单失败，且一件都不扣")
        void insufficient_stock_is_rejected_without_side_effect() {
            assertThatThrownBy(() -> orderService.place(req("S-A", 11)))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("库存不足")
                    .extracting(e -> ((BizException) e).getErrorCode())
                    .isEqualTo(ErrorCode.SKU_OUT_OF_STOCK);

            assertThat(stockOf("S-A")).isEqualTo(10);
            assertThat(orderMapper.selectCount(null)).isZero();
        }

        @Test
        @DisplayName("零库存与已下架报的是不同的话，前端才能给出不同的引导")
        void out_of_stock_and_off_shelf_are_distinguishable() {
            assertThatThrownBy(() -> orderService.place(req("S-EMPTY", 1)))
                    .hasMessageContaining("仅剩 0 件");
            assertThatThrownBy(() -> orderService.place(req("S-OFF", 1)))
                    .hasMessageContaining("已下架");
        }

        @Test
        @DisplayName("不存在的 SKU 报 SKU_NOT_FOUND，而不是笼统的库存不足")
        void unknown_sku() {
            assertThatThrownBy(() -> orderService.place(req("S-NOPE", 1)))
                    .isInstanceOf(BizException.class)
                    .extracting(e -> ((BizException) e).getErrorCode())
                    .isEqualTo(ErrorCode.SKU_NOT_FOUND);
        }

        @Test
        @DisplayName("下单失败时事务整体回滚——不留下扣了库存却没有订单的残局")
        void failed_placement_leaves_no_partial_state() {
            // 逻辑删除的 SKU：deductStock 的 deleted=0 条件会让它扣减失败
            jdbc.update("UPDATE sku SET deleted = 1 WHERE sku_no = 'S-A'");

            assertThatThrownBy(() -> orderService.place(req("S-A", 1)))
                    .isInstanceOf(BizException.class);

            Integer raw = jdbc.queryForObject(
                    "SELECT stock FROM sku WHERE sku_no = 'S-A'", Integer.class);
            assertThat(raw).isEqualTo(10);
            assertThat(orderMapper.selectCount(null)).isZero();
        }
    }

    @Nested
    @DisplayName("幂等")
    class Idempotency {

        @Test
        @DisplayName("同一个 requestId 提交两次只产生一笔订单，只扣一次库存")
        void same_request_id_yields_one_order() {
            CreateOrderRequest r = req("S-A", 2);

            OrderView first = orderService.place(r);
            OrderView second = orderService.place(r);

            assertThat(second.orderNo()).isEqualTo(first.orderNo());
            assertThat(first.idempotentHit()).isFalse();
            assertThat(second.idempotentHit()).isTrue();
            assertThat(orderMapper.selectCount(null)).isEqualTo(1);
            // 关键：扣了 2 不是 4
            assertThat(stockOf("S-A")).isEqualTo(8);
        }

        @Test
        @DisplayName("不同 requestId 是两笔独立订单——幂等不能误伤真实的连续购买")
        void different_request_ids_are_distinct_orders() {
            OrderView a = orderService.place(req("S-A", 1));
            OrderView b = orderService.place(req("S-A", 1));

            assertThat(b.orderNo()).isNotEqualTo(a.orderNo());
            assertThat(stockOf("S-A")).isEqualTo(8);
        }
    }

    @Nested
    @DisplayName("取消")
    class Cancel {

        @Test
        @DisplayName("取消订单会把库存原样还回去")
        void restores_stock() {
            OrderView v = orderService.place(req("S-A", 4));
            assertThat(stockOf("S-A")).isEqualTo(6);

            OrderView cancelled = orderService.cancel(v.orderNo(), USER);

            assertThat(cancelled.status()).isEqualTo("cancelled");
            assertThat(stockOf("S-A")).isEqualTo(10);
        }

        @Test
        @DisplayName("重复取消不会重复回补——否则能凭空刷出库存")
        void second_cancel_does_not_restore_again() {
            OrderView v = orderService.place(req("S-A", 4));
            orderService.cancel(v.orderNo(), USER);

            assertThatThrownBy(() -> orderService.cancel(v.orderNo(), USER))
                    .isInstanceOf(BizException.class)
                    .extracting(e -> ((BizException) e).getErrorCode())
                    .isEqualTo(ErrorCode.ORDER_STATE_ILLEGAL);

            assertThat(stockOf("S-A")).isEqualTo(10);
        }

        @Test
        @DisplayName("已支付的订单不能走取消这条路（退款是另一条链路）")
        void paid_order_cannot_be_cancelled() {
            OrderView v = orderService.place(req("S-A", 1));
            jdbc.update("UPDATE mall_order SET status = 'paid' WHERE order_no = ?", v.orderNo());

            assertThatThrownBy(() -> orderService.cancel(v.orderNo(), USER))
                    .hasMessageContaining("不可取消");
            assertThat(stockOf("S-A")).isEqualTo(9);
        }
    }

    @Nested
    @DisplayName("越权")
    class Authorization {

        /**
         * 与客服工具层 {@code tools.py::get_order_status} 同一个口径：
         * 「别人的订单」与「不存在的订单」必须返回完全相同的错误。
         * 区分开就等于给攻击者一个存在性预言机，枚举单号即可确认哪些是真的。
         */
        @Test
        @DisplayName("查别人的订单与查不存在的订单，返回完全相同的错误")
        void cross_user_read_is_indistinguishable_from_not_found() {
            OrderView v = orderService.place(req("S-A", 1));

            BizException cross = catchBiz(() -> orderService.get(v.orderNo(), OTHER_USER));
            BizException missing = catchBiz(() -> orderService.get("NO-SUCH-ORDER", OTHER_USER));

            assertThat(cross.getErrorCode()).isEqualTo(ErrorCode.ORDER_NOT_FOUND);
            assertThat(cross.getErrorCode()).isEqualTo(missing.getErrorCode());
            assertThat(cross.getMessage()).isEqualTo(missing.getMessage());
        }

        @Test
        @DisplayName("取消别人的订单会被拒，且不会回补库存")
        void cross_user_cancel_is_rejected() {
            OrderView v = orderService.place(req("S-A", 3));

            assertThatThrownBy(() -> orderService.cancel(v.orderNo(), OTHER_USER))
                    .isInstanceOf(BizException.class)
                    .extracting(e -> ((BizException) e).getErrorCode())
                    .isEqualTo(ErrorCode.ORDER_NOT_FOUND);

            assertThat(stockOf("S-A")).isEqualTo(7);
            assertThat(orderMapper.findByOrderNo(v.orderNo()).getStatus())
                    .isEqualTo("pending_payment");
        }

        @Test
        @DisplayName("订单出参不含 user_id——它是鉴权依据，不该回给调用方")
        void order_view_does_not_leak_user_id() {
            OrderView v = orderService.place(req("S-A", 1));
            assertThat(OrderView.class.getRecordComponents())
                    .extracting(java.lang.reflect.RecordComponent::getName)
                    .doesNotContain("userId");
            assertThat(v.orderNo()).isNotBlank();
        }
    }

    @Nested
    @DisplayName("映射")
    class Mapping {

        @Test
        @DisplayName("SKU 实体字段与表列对得上（冒烟）")
        void sku_mapper_smoke() {
            Sku sku = skuMapper.findBySkuNo("S-A");
            assertThat(sku.getProductId()).isEqualTo(9001L);
            assertThat(sku.getPrice()).isEqualByComparingTo(new BigDecimal("299.00"));
            assertThat(sku.getStock()).isEqualTo(10);
        }

        @Test
        @DisplayName("订单实体没有 deleted 字段——它不该被逻辑删除")
        void order_has_no_logic_delete_field() {
            // 全局配了 logic-delete-field: deleted，实体上一旦有这个字段，
            // 所有查询都会自动加 deleted=0。订单被"删掉"意味着客服查不到、
            // 对账对不上——取消是状态流转，不是删除
            assertThat(java.util.Arrays.stream(
                    com.smartmall.product.order.entity.MallOrder.class.getDeclaredFields())
                    .map(java.lang.reflect.Field::getName))
                    .doesNotContain("deleted");
        }
    }

    static BizException catchBiz(Runnable r) {
        try {
            r.run();
            throw new AssertionError("预期抛 BizException，但没有抛");
        } catch (BizException e) {
            return e;
        }
    }
}
