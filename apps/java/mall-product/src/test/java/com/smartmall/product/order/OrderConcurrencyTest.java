package com.smartmall.product.order;

import com.smartmall.common.exception.BizException;
import com.smartmall.product.order.dto.CreateOrderRequest;
import com.smartmall.product.order.mapper.MallOrderMapper;
import com.smartmall.product.order.mapper.SkuMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;

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

/**
 * 并发下单：库存守恒与不超卖。
 *
 * <p><b>这个测试存在的理由：超卖在低并发下永远复现不出来。</b>手工点十次
 * 「立即购买」不会暴露先查后扣的竞态，只有几十个线程同时撞同一行才会。
 * 所以这条链路的正确性不能靠人工验收，只能靠这里。
 *
 * <p><b>关于 H2 与 InnoDB 的差异，需要说清楚：</b>这些测试跑在 H2 的 MySQL
 * 兼容模式上，H2 的行锁实现与 InnoDB 不是一回事。H2 通过不代表 InnoDB 通过。
 * 因此同一个场景另外对真实 MySQL 8.0 复核过一遍（50 并发抢 5 件，成功 5 单、
 * 终态库存 0、无负库存），复核脚本见 {@code deploy/scripts/verify-order-concurrency.sh}。
 * 这里保留 H2 版本，是因为它能在任何机器上无条件执行——要起容器才能跑的
 * 测试，在没装 Docker 的机器上会被静默跳过，而被跳过的测试等于不存在。
 */
@SpringBootTest
class OrderConcurrencyTest {

    @Autowired
    OrderService orderService;
    @Autowired
    SkuMapper skuMapper;
    @Autowired
    MallOrderMapper orderMapper;
    @Autowired
    JdbcTemplate jdbc;

    static final int THREADS = 50;

    @BeforeEach
    void reset() {
        jdbc.execute("DELETE FROM mall_order");
        jdbc.execute("DELETE FROM sku");
    }

    void seed(String skuNo, int stock) {
        jdbc.update("INSERT INTO sku (sku_no, product_id, spec, price, origin_price, stock, status)"
                + " VALUES (?, 9001, '{}', 100.00, 150.00, ?, 'on_sale')", skuNo, stock);
    }

    /** 并发跑 n 个任务，全部就位后一起放行——不这样做线程会前后脚跑，撞不上。 */
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
            assertThat(ready.await(30, TimeUnit.SECONDS)).as("线程未能全部就位").isTrue();
            go.countDown();
            pool.shutdown();
            assertThat(pool.awaitTermination(120, TimeUnit.SECONDS)).as("并发任务超时").isTrue();
            return futures;
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    @DisplayName("50 人抢 5 件：恰好 5 单成功，库存归零，不出现负库存")
    void no_oversell_under_stampede() throws Exception {
        seed("S-HOT", 5);
        AtomicInteger ok = new AtomicInteger();
        AtomicInteger soldOut = new AtomicInteger();

        List<Future<Void>> futures = stampede(THREADS, () -> {
            try {
                orderService.place(new CreateOrderRequest(
                        UUID.randomUUID().toString(), 1001L, "S-HOT", 1));
                ok.incrementAndGet();
            } catch (BizException e) {
                soldOut.incrementAndGet();
            }
            return null;
        });
        for (Future<Void> f : futures) {
            f.get();
        }

        assertThat(ok.get()).as("成功下单数必须恰好等于库存").isEqualTo(5);
        assertThat(soldOut.get()).isEqualTo(THREADS - 5);
        assertThat(skuMapper.findBySkuNo("S-HOT").getStock())
                .as("库存必须归零，且绝不能为负").isZero();
        assertThat(orderMapper.selectCount(null)).isEqualTo(5);
    }

    @Test
    @DisplayName("并发下单数量不一时，库存依然守恒：扣掉的总数 = 成交订单的总件数")
    void stock_is_conserved_with_mixed_quantities() throws Exception {
        seed("S-MIX", 40);
        AtomicInteger soldUnits = new AtomicInteger();

        List<Future<Void>> futures = stampede(THREADS, () -> {
            int qty = 1 + (int) (Thread.currentThread().threadId() % 4);   // 1~4 件
            try {
                orderService.place(new CreateOrderRequest(
                        UUID.randomUUID().toString(), 1001L, "S-MIX", qty));
                soldUnits.addAndGet(qty);
            } catch (BizException ignored) {
                // 库存不足，正常结果
            }
            return null;
        });
        for (Future<Void> f : futures) {
            f.get();
        }

        int remaining = skuMapper.findBySkuNo("S-MIX").getStock();
        assertThat(remaining).as("库存不能为负").isGreaterThanOrEqualTo(0);
        assertThat(soldUnits.get() + remaining)
                .as("售出件数 + 剩余库存必须等于初始库存").isEqualTo(40);

        Integer orderedUnits = jdbc.queryForObject(
                "SELECT COALESCE(SUM(quantity), 0) FROM mall_order", Integer.class);
        assertThat(orderedUnits)
                .as("订单表记的件数要与实际扣减一致").isEqualTo(soldUnits.get());
    }

    /**
     * 幂等的慢路径：同一个 requestId 被并发提交，快路径（先查一次）全部落空，
     * 于是多个请求同时走到 INSERT，靠唯一索引分胜负。
     *
     * <p>这条测试盯的是那个容易写错的点——输的请求必须**整笔回滚**。
     * 如果 catch 写在事务内部，它扣掉的库存不会吐回来，最终库存就会少几件，
     * 而订单只有一笔。断言里的 stock == 9 就是在守这个。
     */
    @Test
    @DisplayName("同一 requestId 并发提交：只出一单，且落败请求扣的库存要吐回来")
    void concurrent_same_request_id_creates_exactly_one_order() throws Exception {
        seed("S-IDEM", 10);
        String sharedRequestId = UUID.randomUUID().toString();
        AtomicInteger returned = new AtomicInteger();

        List<Future<String>> futures = stampede(20, () -> {
            String no = orderService.place(new CreateOrderRequest(
                    sharedRequestId, 1001L, "S-IDEM", 1)).orderNo();
            returned.incrementAndGet();
            return no;
        });

        java.util.Set<String> orderNos = new java.util.HashSet<>();
        for (Future<String> f : futures) {
            orderNos.add(f.get());
        }

        assertThat(returned.get()).as("所有请求都该拿到结果，没有一个报错").isEqualTo(20);
        assertThat(orderNos).as("20 个请求必须拿到同一个订单号").hasSize(1);
        assertThat(orderMapper.selectCount(null)).isEqualTo(1);
        assertThat(skuMapper.findBySkuNo("S-IDEM").getStock())
                .as("只应扣 1 件；少于 9 说明落败请求扣的库存没回滚").isEqualTo(9);
    }

    @Test
    @DisplayName("并发取消同一张单：只回补一次库存")
    void concurrent_cancel_restores_stock_once() throws Exception {
        seed("S-CAN", 10);
        String orderNo = orderService.place(new CreateOrderRequest(
                UUID.randomUUID().toString(), 1001L, "S-CAN", 3)).orderNo();
        assertThat(skuMapper.findBySkuNo("S-CAN").getStock()).isEqualTo(7);

        AtomicInteger succeeded = new AtomicInteger();
        List<Future<Void>> futures = stampede(20, () -> {
            try {
                orderService.cancel(orderNo, 1001L);
                succeeded.incrementAndGet();
            } catch (BizException ignored) {
                // 状态已流转，正常结果
            }
            return null;
        });
        for (Future<Void> f : futures) {
            f.get();
        }

        assertThat(succeeded.get()).as("只能有一次取消成功").isEqualTo(1);
        assertThat(skuMapper.findBySkuNo("S-CAN").getStock())
                .as("回补恰好一次；大于 10 就是凭空刷出了库存").isEqualTo(10);
    }
}
