package com.smartmall.product.order;

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
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

import java.time.Clock;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 订单号撞了以后的处置：整笔重试，库存不漏、订单不重。
 *
 * <p><b>这条兜底为什么要单独测。</b>生成器按构造无碰撞，正常情况下它永远不会触发——
 * 一段永远跑不到的代码，最容易在重构里被悄悄改坏，而坏的时候没有任何信号。
 * 所以用一个"先吐出已存在的订单号"的生成器强行把它逼出来，对真实事务验证三件事：
 * 撞一次能成功、库存只扣一次、撞满上限要明确失败而且库存如数吐回。
 *
 * <p>不用 @SpyBean：换了 Bean 定义会让 Spring 另起一个上下文，而测试库是共享的
 * 内存 H2，重复执行建表脚本会互相踩。直接用真实的 Mapper 与事务模板手工
 * 装配一个 OrderService，生成器换成测试替身即可。
 */
@SpringBootTest
class OrderNoCollisionTest {

    @Autowired
    MallOrderMapper orderMapper;
    @Autowired
    SkuMapper skuMapper;
    @Autowired
    TransactionTemplate tx;
    @Autowired
    JdbcTemplate jdbc;

    static final long USER = 1001L;
    static final String TAKEN = "20260101000000000001";

    /** 按剧本吐订单号的生成器：剧本用完后回落到真实生成器。 */
    static class ScriptedGenerator extends OrderNoGenerator {
        final Deque<String> script = new ArrayDeque<>();
        final AtomicInteger calls = new AtomicInteger();

        ScriptedGenerator(String... first) {
            super(9, Clock.systemDefaultZone());
            for (String s : first) {
                script.add(s);
            }
        }

        @Override
        public String next() {
            calls.incrementAndGet();
            return script.isEmpty() ? super.next() : script.poll();
        }
    }

    OrderService serviceWith(OrderNoGenerator gen) {
        return new OrderService(orderMapper, skuMapper, tx, gen, new PlacementGate(100, 100, Duration.ofSeconds(1)),
                Duration.ofMinutes(30), 200, Duration.ofSeconds(30));
    }

    static CreateOrderRequest req() {
        return new CreateOrderRequest(UUID.randomUUID().toString(), "S-A", 1);
    }

    int stock() {
        return skuMapper.findBySkuNo("S-A").getStock();
    }

    int orderCount() {
        return jdbc.queryForObject("SELECT COUNT(*) FROM mall_order", Integer.class);
    }

    @BeforeEach
    void reset() {
        jdbc.execute("DELETE FROM mall_order");
        jdbc.execute("DELETE FROM sku");
        jdbc.update("INSERT INTO sku (sku_no, product_id, spec, price, origin_price, stock, status)"
                + " VALUES ('S-A', 9001, '{\"尺码\":\"M\"}', 299.00, 399.00, 10, 'on_sale')");
        // 一笔已经占着订单号的存量订单
        jdbc.update("INSERT INTO mall_order (order_no, request_id, user_id, product_id, sku_no, spec,"
                + " quantity, amount, status) VALUES (?, 'seed-req', 7, 9001, 'S-A', '', 1, 299.00,"
                + " 'pending_payment')", TAKEN);
    }

    @Nested
    @DisplayName("订单号碰撞")
    class Collision {

        @Test
        @DisplayName("撞一次：整笔重试后下单成功，库存只扣一次，没有多出订单")
        void retriesOnceAndSucceeds() {
            ScriptedGenerator gen = new ScriptedGenerator(TAKEN);      // 第一次撞，第二次走真实生成器
            OrderView v = serviceWith(gen).place(req(), USER);

            assertThat(v.orderNo()).isNotEqualTo(TAKEN).matches("\\d{20}");
            assertThat(gen.calls.get()).as("生成器被调用次数").isEqualTo(2);
            // 库存只扣一次：第一次尝试的事务整个回滚，扣掉的那件已经吐回
            assertThat(stock()).isEqualTo(9);
            // 存量那笔 + 新下的这笔；撞号的那次没留下任何痕迹
            assertThat(orderCount()).isEqualTo(2);
        }

        @Test
        @DisplayName("连撞两次：第三次成功")
        void retriesTwice() {
            ScriptedGenerator gen = new ScriptedGenerator(TAKEN, TAKEN);
            OrderView v = serviceWith(gen).place(req(), USER);

            assertThat(v.orderNo()).isNotEqualTo(TAKEN);
            assertThat(gen.calls.get()).isEqualTo(3);
            assertThat(stock()).isEqualTo(9);
            assertThat(orderCount()).isEqualTo(2);
        }

        @Test
        @DisplayName("一直撞：到上限后明确失败，库存如数吐回，不留半成品订单")
        void givesUpAfterLimit() {
            ScriptedGenerator gen = new ScriptedGenerator(TAKEN, TAKEN, TAKEN, TAKEN, TAKEN);

            assertThatThrownBy(() -> serviceWith(gen).place(req(), USER))
                    .isInstanceOf(DuplicateKeyException.class);

            assertThat(gen.calls.get()).as("最多尝试 3 次，不无限重试").isEqualTo(3);
            assertThat(stock()).as("三次尝试扣掉的库存必须全部回滚").isEqualTo(10);
            assertThat(orderCount()).as("只剩存量那一笔").isEqualTo(1);
        }

        @Test
        @DisplayName("撞号重试时，同一个 request_id 之后再提交仍是幂等命中")
        void idempotencyStillHoldsAfterRetry() {
            ScriptedGenerator gen = new ScriptedGenerator(TAKEN);
            OrderService svc = serviceWith(gen);
            CreateOrderRequest r = req();

            OrderView first = svc.place(r, USER);
            OrderView again = svc.place(r, USER);

            assertThat(again.orderNo()).isEqualTo(first.orderNo());
            assertThat(again.idempotentHit()).isTrue();
            assertThat(stock()).isEqualTo(9);
        }
    }

    @Nested
    @DisplayName("冲突类型判别")
    class Classification {

        @Test
        @DisplayName("只有 uk_order_no 冲突才重试，别的唯一键冲突不重试")
        void onlyOrderNoCollisionIsRetried() {
            assertThat(OrderService.isOrderNoCollision(new DuplicateKeyException(
                    "Duplicate entry '2026' for key 'mall_order.uk_order_no'"))).isTrue();
            assertThat(OrderService.isOrderNoCollision(new DuplicateKeyException(
                    "Unique index violation: \"UK_ORDER_NO_INDEX_2 ON PUBLIC.MALL_ORDER\""))).isTrue();
            assertThat(OrderService.isOrderNoCollision(new DuplicateKeyException(
                    "Duplicate entry 'x' for key 'mall_order.uk_some_future_key'"))).isFalse();
        }

        @Test
        @DisplayName("冲突信息藏在嵌套的 cause 里也能认出来")
        void looksThroughCauseChain() {
            Throwable inner = new RuntimeException("Duplicate entry 'x' for key 'mall_order.uk_order_no'");
            assertThat(OrderService.isOrderNoCollision(
                    new DuplicateKeyException("### Error updating database", inner))).isTrue();
        }
    }
}
