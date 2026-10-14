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
import org.springframework.transaction.support.TransactionTemplate;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 下单的新事务形态：咨询性检查 → 舱壁 → 先建单、后扣库存。
 *
 * <p>这几件事在功能上几乎看不出来（用户看到的还是"下单成功/库存不足"），差别在行锁持有多久
 * 与失败时怎么收场——所以要用<b>观察调用顺序与回滚结果</b>的方式去测，而不是只看返回值。
 * SkuMapper 继承了 MyBatis-Plus 的 BaseMapper（几十个方法），手写委托实现不现实，
 * 这里用 JDK 动态代理只拦截 findBySkuNo 与 deductStock，其余原样转发。
 */
@SpringBootTest
class OrderPlacementFlowTest {

    @Autowired
    MallOrderMapper orderMapper;
    @Autowired
    SkuMapper skuMapper;
    @Autowired
    TransactionTemplate tx;
    @Autowired
    OrderNoGenerator orderNos;
    @Autowired
    JdbcTemplate jdbc;

    static final long USER = 1001L;

    @BeforeEach
    void reset() {
        jdbc.execute("DELETE FROM mall_order");
        jdbc.execute("DELETE FROM sku");
        sku("S-A", 10);
        sku("S-B", 10);
        sku("S-EMPTY", 0);
    }

    void sku(String no, int stock) {
        jdbc.update("INSERT INTO sku (sku_no, product_id, spec, price, origin_price, stock, status)"
                + " VALUES (?, 9001, '{\"尺码\":\"M\"}', 100.00, 150.00, ?, 'on_sale')", no, stock);
    }

    int stockOf(String no) {
        return jdbc.queryForObject("SELECT stock FROM sku WHERE sku_no = ?", Integer.class, no);
    }

    int orders() {
        return jdbc.queryForObject("SELECT COUNT(*) FROM mall_order", Integer.class);
    }

    static CreateOrderRequest req(String sku, int qty) {
        return new CreateOrderRequest(UUID.randomUUID().toString(), sku, qty);
    }

    /** 观察点：拦截 findBySkuNo 与 deductStock，其余转发。 */
    static class Probe {
        final AtomicInteger deductCalls = new AtomicInteger();
        final List<String> events = new ArrayList<>();
        /** 第一次 findBySkuNo 返回这个（过期的）视图，之后返回真实的。 */
        volatile Supplier<Sku> staleFirstRead;
        volatile Runnable onDeduct = () -> { };
        private final AtomicBoolean firstRead = new AtomicBoolean(true);

        SkuMapper wrap(SkuMapper real) {
            return (SkuMapper) Proxy.newProxyInstance(
                    SkuMapper.class.getClassLoader(), new Class[]{SkuMapper.class},
                    (proxy, m, args) -> {
                        if (m.getName().equals("findBySkuNo") && staleFirstRead != null
                                && firstRead.getAndSet(false)) {
                            return staleFirstRead.get();
                        }
                        if (m.getName().equals("deductStock")) {
                            deductCalls.incrementAndGet();
                            onDeduct.run();
                        }
                        try {
                            return m.invoke(real, args);
                        } catch (InvocationTargetException e) {
                            throw e.getCause();
                        }
                    });
        }
    }

    OrderService service(SkuMapper sm, PlacementGate gate) {
        return new OrderService(orderMapper, sm, tx, orderNos, gate,
                Duration.ofMinutes(30), 200, Duration.ofSeconds(30));
    }

    static PlacementGate openGate() {
        return new PlacementGate(100, 100, Duration.ofSeconds(1));
    }

    @Nested
    @DisplayName("语句顺序：先建单、后扣库存")
    class Ordering {

        @Test
        @DisplayName("扣库存发生时，订单已经在同一事务里插进去了——行锁只覆盖 UPDATE + COMMIT")
        void orderIsInsertedBeforeStockIsDeducted() {
            Probe probe = new Probe();
            String[] requestId = new String[1];
            probe.onDeduct = () -> {
                // 在扣库存的那一刻，从同一个事务连接里看订单表：此时订单必须已经在了
                int seen = jdbc.queryForObject(
                        "SELECT COUNT(*) FROM mall_order WHERE request_id = ?", Integer.class, requestId[0]);
                probe.events.add("订单可见=" + seen);
            };
            OrderService svc = service(probe.wrap(skuMapper), openGate());

            CreateOrderRequest r = req("S-A", 2);
            requestId[0] = r.requestId();
            svc.place(r, USER);

            // 这条测试钉的是性能设计：把顺序改回"先扣库存"功能上毫无差别，只是热点吞吐少 35%
            // （真库实测 705 → 469 TPS）。没有它，这个回退会悄悄发生而没人知道。
            assertThat(probe.events).containsExactly("订单可见=1");
        }

        @Test
        @DisplayName("过期的咨询检查输了竞态：订单跟着回滚，库存不变，不留半成品")
        void losingTheRaceRollsBackTheInsertedOrder() {
            // 咨询检查读到的是"还有 5 件"，但真实库存是 0（读完之后被别人抢光了）
            jdbc.update("UPDATE sku SET stock = 0 WHERE sku_no = 'S-A'");
            Probe probe = new Probe();
            probe.staleFirstRead = () -> {
                Sku s = skuMapper.findBySkuNo("S-A");
                s.setStock(5);
                return s;
            };
            OrderService svc = service(probe.wrap(skuMapper), openGate());

            assertThatThrownBy(() -> svc.place(req("S-A", 1), USER))
                    .isInstanceOfSatisfying(BizException.class,
                            e -> assertThat(e.getErrorCode()).isEqualTo(ErrorCode.SKU_OUT_OF_STOCK));

            assertThat(probe.deductCalls.get()).as("确实走到了扣库存才失败").isEqualTo(1);
            assertThat(orders()).as("先插进去的订单必须随事务回滚").isZero();
            assertThat(stockOf("S-A")).isZero();
        }
    }

    @Nested
    @DisplayName("咨询性检查")
    class Advisory {

        @Test
        @DisplayName("售罄：直接拒绝，根本不碰库存行（抢购里卖完之后的请求占压倒多数）")
        void soldOutNeverReachesTheStockRow() {
            Probe probe = new Probe();
            OrderService svc = service(probe.wrap(skuMapper), openGate());

            assertThatThrownBy(() -> svc.place(req("S-EMPTY", 1), USER))
                    .isInstanceOfSatisfying(BizException.class,
                            e -> assertThat(e.getErrorCode()).isEqualTo(ErrorCode.SKU_OUT_OF_STOCK));
            assertThat(probe.deductCalls.get()).isZero();
        }

        @Test
        @DisplayName("数量超过库存：同样挡在事务外，并告诉用户还剩几件")
        void quantityAboveStockIsRejectedWithRemaining() {
            Probe probe = new Probe();
            OrderService svc = service(probe.wrap(skuMapper), openGate());

            assertThatThrownBy(() -> svc.place(req("S-A", 11), USER))
                    .hasMessageContaining("仅剩 10 件");
            assertThat(probe.deductCalls.get()).isZero();
            assertThat(stockOf("S-A")).isEqualTo(10);
        }

        @Test
        @DisplayName("乱填的 skuNo：返回不存在，而且不会在舱壁里建信号量（map 以商品目录为界）")
        void unknownSkuDoesNotGrowTheGateMap() {
            PlacementGate gate = openGate();
            OrderService svc = service(skuMapper, gate);

            for (int i = 0; i < 20; i++) {
                String bogus = "BOGUS-" + i;
                assertThatThrownBy(() -> svc.place(req(bogus, 1), USER))
                        .isInstanceOfSatisfying(BizException.class,
                                e -> assertThat(e.getErrorCode()).isEqualTo(ErrorCode.SKU_NOT_FOUND));
            }
            assertThat(gate.trackedSkus()).as("外部不能靠乱填 skuNo 撑大 map").isZero();
        }
    }

    @Nested
    @DisplayName("舱壁接入下单")
    class GateIntegration {

        @Test
        @DisplayName("舱壁满了：快速失败（RATE_LIMITED），不留任何副作用；别的商品照常下单")
        void saturatedGateRejectsWithoutSideEffectsAndOthersProceed() {
            PlacementGate gate = new PlacementGate(1, 8, Duration.ofMillis(30));
            OrderService svc = service(skuMapper, gate);

            try (PlacementGate.Permit hold = gate.enter("S-A")) {          // S-A 的名额被占满
                assertThatThrownBy(() -> svc.place(req("S-A", 1), USER))
                        .isInstanceOfSatisfying(BizException.class,
                                e -> assertThat(e.getErrorCode()).isEqualTo(ErrorCode.RATE_LIMITED));
                assertThat(orders()).isZero();
                assertThat(stockOf("S-A")).isEqualTo(10);

                OrderView ok = svc.place(req("S-B", 1), USER);             // 别的商品不受影响
                assertThat(ok.orderNo()).isNotBlank();
            }
        }

        @Test
        @DisplayName("成功与失败之后许可都归还——否则每笔订单漏一个，几十笔之后整个下单链路锁死")
        void permitsAreReturnedOnSuccessAndOnFailure() {
            PlacementGate gate = new PlacementGate(2, 3, Duration.ofMillis(50));
            OrderService svc = service(skuMapper, gate);

            svc.place(req("S-A", 1), USER);                                // 成功
            assertThatThrownBy(() -> svc.place(req("S-EMPTY", 1), USER));  // 咨询检查失败（没进舱壁）

            Probe probe = new Probe();
            jdbc.update("UPDATE sku SET stock = 0 WHERE sku_no = 'S-B'");
            probe.staleFirstRead = () -> {
                Sku s = skuMapper.findBySkuNo("S-B");
                s.setStock(9);
                return s;
            };
            assertThatThrownBy(() -> service(probe.wrap(skuMapper), gate).place(req("S-B", 1), USER));  // 进了舱壁、事务里失败

            assertThat(gate.availableTotal()).isEqualTo(3);
            assertThat(gate.availableFor("S-A")).isEqualTo(2);
            assertThat(gate.availableFor("S-B")).isEqualTo(2);
        }

        @Test
        @DisplayName("幂等命中走快路径，不进舱壁——重试请求不该去抢名额")
        void idempotentRetryDoesNotConsumeAGatePermit() {
            PlacementGate gate = new PlacementGate(1, 1, Duration.ofMillis(30));
            OrderService svc = service(skuMapper, gate);
            CreateOrderRequest r = req("S-A", 1);
            OrderView first = svc.place(r, USER);

            try (PlacementGate.Permit hold = gate.enter("S-A")) {          // 名额被占满
                OrderView again = svc.place(r, USER);                      // 同一个 request_id
                assertThat(again.orderNo()).isEqualTo(first.orderNo());
                assertThat(again.idempotentHit()).isTrue();
            }
        }
    }
}
