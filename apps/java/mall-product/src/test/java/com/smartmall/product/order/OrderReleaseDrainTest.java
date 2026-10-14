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
import org.springframework.jdbc.core.JdbcTemplate;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 超时回收：积压要能翻完，一笔失败不能挡住后面的。
 *
 * <p>对应两个问题，都是读代码时发现、再用测试复现的：
 *
 * <ol>
 *   <li><b>吞吐。</b>原先每轮只处理一批（200 单），一分钟一轮，上限 200 单/分钟。
 *       1 万个放弃支付的订单要 50 分钟才收得完，这段时间里该 SKU 的库存一直被占着，
 *       页面显示售罄而实际一件没卖出去。</li>
 *   <li><b>毒单。</b>某一笔订单回收时稳定地抛异常，原先会中断整批，而下一轮又从同一个队头
 *       开始——它后面的所有订单<b>永远</b>回收不了。实测复现：毒单后面的健康订单
 *       一直停在 pending_payment。</li>
 * </ol>
 */
@SpringBootTest
class OrderReleaseDrainTest {

    @Autowired
    OrderService orderService;
    @Autowired
    SkuMapper skuMapper;
    @Autowired
    MallOrderMapper orderMapper;
    @Autowired
    JdbcTemplate jdbc;

    static final long USER = 1001L;
    static final Duration TTL = Duration.ofMinutes(30);

    @BeforeEach
    void reset() {
        jdbc.execute("DELETE FROM mall_order");
        jdbc.execute("DELETE FROM sku");
        sku("S-A", 1000);
        sku("S-B", 1000);
    }

    void sku(String no, int stock) {
        jdbc.update("INSERT INTO sku (sku_no, product_id, spec, price, origin_price, stock, status)"
                + " VALUES (?, 9001, '{}', 10.00, 10.00, ?, 'on_sale')", no, stock);
    }

    OrderView place(String skuNo, int qty) {
        return orderService.place(new CreateOrderRequest(UUID.randomUUID().toString(), skuNo, qty), USER);
    }

    List<OrderView> placeMany(String skuNo, int n) {
        List<OrderView> out = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            out.add(place(skuNo, 1));
        }
        return out;
    }

    void ageAll() {
        jdbc.update("UPDATE mall_order SET created_at = DATEADD('HOUR', -2, CURRENT_TIMESTAMP)");
    }

    String statusOf(OrderView v) {
        return orderMapper.findByOrderNo(v.orderNo()).getStatus();
    }

    long count(String status) {
        return jdbc.queryForObject("SELECT COUNT(*) FROM mall_order WHERE status = ?", Long.class, status);
    }

    int stock(String sku) {
        return skuMapper.findBySkuNo(sku).getStock();
    }

    @Nested
    @DisplayName("积压翻页")
    class Backlog {

        @Test
        @DisplayName("积压超过一批：一次触发就全部收完，库存全部回补")
        void drainsBacklogLargerThanOneBatch() {
            placeMany("S-A", 25);
            ageAll();
            assertThat(stock("S-A")).isEqualTo(975);

            int released = orderService.releaseAllExpired(TTL, 10, Duration.ofSeconds(30));

            assertThat(released).isEqualTo(25);                     // 10 + 10 + 5，三批
            assertThat(count("pending_payment")).isZero();
            assertThat(stock("S-A")).isEqualTo(1000);
        }

        @Test
        @DisplayName("旧行为对照：单批方法一次只能收一批")
        void singleBatchMethodStillProcessesOneBatch() {
            placeMany("S-A", 25);
            ageAll();

            assertThat(orderService.releaseExpired(TTL, 10)).isEqualTo(10);
            assertThat(count("pending_payment")).isEqualTo(15);
        }

        @Test
        @DisplayName("时间预算用完就停：第一批一定跑，之后每批前检查，剩下的留给下一轮")
        void respectsTimeBudget() {
            placeMany("S-A", 25);
            ageAll();

            int released = orderService.releaseAllExpired(TTL, 10, Duration.ZERO);

            assertThat(released).as("预算为 0：只跑第一批").isEqualTo(10);
            assertThat(count("pending_payment")).isEqualTo(15);

            // 下一轮接着收，已回收的不会再出现
            assertThat(orderService.releaseAllExpired(TTL, 10, Duration.ofSeconds(30))).isEqualTo(15);
            assertThat(count("pending_payment")).isZero();
        }

        @Test
        @DisplayName("恰好一批整：要再查一次确认队列真的空了，而不是误以为还有")
        void exactMultipleOfBatchTerminates() {
            placeMany("S-A", 20);
            ageAll();

            assertThat(orderService.releaseAllExpired(TTL, 10, Duration.ofSeconds(30))).isEqualTo(20);
            assertThat(count("pending_payment")).isZero();
        }

        @Test
        @DisplayName("没有积压：一批都不满，立刻返回 0")
        void emptyQueue() {
            assertThat(orderService.releaseAllExpired(TTL, 10, Duration.ofSeconds(30))).isZero();
        }

        @Test
        @DisplayName("批量上限配成 0 直接报错——否则翻页循环永远不满足终止条件")
        void nonPositiveBatchLimitIsRejected() {
            assertThatThrownBy(() -> orderService.releaseAllExpired(TTL, 0, Duration.ofSeconds(1)))
                    .isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> orderService.releaseExpiredBatch(TTL, -3, OrderService.Cursor.START))
                    .isInstanceOf(IllegalArgumentException.class);
        }

        @Test
        @DisplayName("只收超期的：没到时间的单留着，跨批也一样")
        void freshOrdersSurviveTheDrain() {
            placeMany("S-A", 15);
            ageAll();
            OrderView fresh = place("S-A", 1);                      // 刚下的，没超期

            assertThat(orderService.releaseAllExpired(TTL, 10, Duration.ofSeconds(30))).isEqualTo(15);
            assertThat(statusOf(fresh)).isEqualTo("pending_payment");
        }
    }

    @Nested
    @DisplayName("毒单不挡队")
    class PoisonOrder {

        /**
         * 造一笔回收时必然抛异常的订单：下单之后把它那个 SKU 的库存改成 INT 最大值，
         * 回补时 stock + qty 溢出，数据库报错。
         */
        OrderView poison() {
            sku("S-POISON", 10);
            OrderView p = place("S-POISON", 2);
            jdbc.update("UPDATE sku SET stock = 2147483647 WHERE sku_no = 'S-POISON'");
            return p;
        }

        @Test
        @DisplayName("毒单排在最前：它后面的订单照常被回收（旧实现下它们永远回收不了）")
        void poisonAtTheHeadDoesNotBlockTheRest() {
            OrderView poison = poison();                            // id 最小
            List<OrderView> good = placeMany("S-A", 5);
            ageAll();

            int released = orderService.releaseAllExpired(TTL, 100, Duration.ofSeconds(30));

            assertThat(released).isEqualTo(5);
            good.forEach(v -> assertThat(statusOf(v)).isEqualTo("cancelled"));
            assertThat(statusOf(poison)).as("毒单原样留着，等人处理").isEqualTo("pending_payment");
            assertThat(stock("S-A")).isEqualTo(1000);
        }

        @Test
        @DisplayName("毒单在批与批之间：游标越过它，后面的批照常翻")
        void poisonDoesNotStallPaging() {
            placeMany("S-A", 3);
            OrderView poison = poison();
            placeMany("S-B", 7);
            ageAll();

            // 批大小 2：[A,A] [A,poison] [B,B] [B,B] [B,B] [B]  —— 毒单在第二批
            int released = orderService.releaseAllExpired(TTL, 2, Duration.ofSeconds(30));

            assertThat(released).isEqualTo(10);
            assertThat(count("pending_payment")).isEqualTo(1);
            assertThat(statusOf(poison)).isEqualTo("pending_payment");
            assertThat(stock("S-B")).isEqualTo(1000);
        }

        @Test
        @DisplayName("毒单多到塞满整批：没有游标就原地空转，后面的健康订单一笔都收不到")
        void manyPoisonOrdersFillingAWholeBatchStillMakeProgress() {
            // 这条是游标存在的全部理由。前面那些用例里毒单都少于批大小，每批至多撞上一笔，
            // 没有游标也能往前走——去掉 `id > afterId` 它们照样全绿（变异验证量出来的）。
            // 只有当失败的订单数 >= 批大小，下一批才会"全是上一批失败的那几笔"：
            // 它们仍然是 pending_payment，每批都被重新选中，循环空转到时间预算耗尽。
            sku("S-POISON", 10);
            for (int i = 0; i < 3; i++) {
                place("S-POISON", 1);
            }
            jdbc.update("UPDATE sku SET stock = 2147483647 WHERE sku_no = 'S-POISON'");
            List<OrderView> good = placeMany("S-A", 4);
            ageAll();

            long t0 = System.nanoTime();
            // 预算只给 2 秒：有游标是一瞬间的事；没有游标会一路空转到预算用完才停
            int released = orderService.releaseAllExpired(TTL, 2, Duration.ofSeconds(2));
            long ms = (System.nanoTime() - t0) / 1_000_000;

            assertThat(released).isEqualTo(4);
            good.forEach(v -> assertThat(statusOf(v)).isEqualTo("cancelled"));
            assertThat(count("pending_payment")).as("三笔毒单原样留着").isEqualTo(3);
            assertThat(ms).as("应当翻完队列后自己结束，而不是撞时间预算").isLessThan(1500);
        }

        @Test
        @DisplayName("单批方法也不再因一笔失败而整批作废")
        void singleBatchSurvivesAFailure() {
            OrderView poison = poison();
            OrderView good = place("S-A", 1);
            ageAll();

            assertThat(orderService.releaseExpired(TTL, 100)).isEqualTo(1);
            assertThat(statusOf(good)).isEqualTo("cancelled");
        }

        @Test
        @DisplayName("失败数被如实报告，不是悄悄吞掉")
        void failureIsReported() {
            poison();
            placeMany("S-A", 2);
            ageAll();

            OrderService.ReleaseBatch b = orderService.releaseExpiredBatch(TTL, 100, OrderService.Cursor.START);

            assertThat(b.scanned()).isEqualTo(3);
            assertThat(b.released()).isEqualTo(2);
            assertThat(b.failed()).isEqualTo(1);
        }

        @Test
        @DisplayName("毒单每一轮都会被重试——偶发失败（比如死锁）自然恢复")
        void failedOrderIsRetriedOnTheNextRun() {
            OrderView poison = poison();
            ageAll();

            assertThat(orderService.releaseAllExpired(TTL, 100, Duration.ofSeconds(30))).isZero();
            // 修好数据之后，下一轮就能收掉它
            jdbc.update("UPDATE sku SET stock = 5 WHERE sku_no = 'S-POISON'");
            assertThat(orderService.releaseAllExpired(TTL, 100, Duration.ofSeconds(30))).isEqualTo(1);
            assertThat(statusOf(poison)).isEqualTo("cancelled");
            assertThat(stock("S-POISON")).isEqualTo(7);
        }
    }

    @Nested
    @DisplayName("库存回补的完整性")
    class StockIntegrity {

        @Test
        @DisplayName("跑两遍不会回补两次——游标翻页没有破坏至多一次")
        void runningTwiceRestoresOnce() {
            placeMany("S-A", 25);
            ageAll();

            orderService.releaseAllExpired(TTL, 10, Duration.ofSeconds(30));
            orderService.releaseAllExpired(TTL, 10, Duration.ofSeconds(30));

            assertThat(stock("S-A")).isEqualTo(1000);
        }

        @Test
        @DisplayName("SKU 被删了：订单照样取消，不抛异常（库存无处可补，但会记一条告警）")
        void deletedSkuStillCancelsTheOrder() {
            OrderView v = place("S-A", 3);
            ageAll();
            jdbc.update("UPDATE sku SET deleted = 1 WHERE sku_no = 'S-A'");

            assertThat(orderService.releaseAllExpired(TTL, 10, Duration.ofSeconds(30))).isEqualTo(1);
            assertThat(statusOf(v)).isEqualTo("cancelled");
        }
    }
}
