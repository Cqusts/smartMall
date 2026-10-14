package com.smartmall.product.order;

import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.exception.BizException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 下单的舱壁：限制同时进入"扣库存事务"的并发数，超出时快速失败，而不是排队到超时。
 *
 * <h3>它要解决的问题：一个爆款拖垮整个店</h3>
 *
 * 热点 SKU 的下单会在<b>同一行库存</b>上串行（行锁）。几百个请求同时来时，每个都占着一条
 * 数据库连接在等那把锁，而连接池只有 10 条——于是<b>别的商品的下单也拿不到连接</b>。
 * 真库实测（300 并发砸一个 SKU，同时一个"路人"每 100ms 买一次别的商品）：
 *
 * <pre>
 *   热点自己        成功 TPS 99，p99 9 秒，还有 Hikari 超时的 500
 *   买别的商品的路人  20 秒只完成 10 次，p50 1.5 秒，最长 10.1 秒（正常十几毫秒）
 * </pre>
 *
 * <h3>每-SKU 上限是主角；全局上限默认放开</h3>
 *
 * <ol>
 *   <li><b>每个 SKU 一个上限</b>（默认 4）。同一行上再多的并发也不会更快——行锁把它们串成一条线，
 *       实测 4 个连接与 32 个连接的热点吞吐一样（447 对 422 TPS），多出来的只是排队、占连接。
 *       一个爆款最多占 4 条连接，池里剩下的留给别的商品——这就是它解决的全部问题。</li>
 *   <li><b>全局上限</b>（默认 64，实际上等于关闭）。<b>第一版把它设成 8，是个错误</b>：
 *       它比连接池（10）还小，自己就成了吞吐限制器。真库实测 150 并发<em>分散</em>下单
 *       （随机买 200 个 SKU，没有任何热点），它误拒了 22%（664/2991），成功 TPS 从 227
 *       掉到 151——事务一慢，8 个名额周转不过来，排队超过 max-wait 就被拒，而旧版本同样的
 *       负载靠连接池排队照样处理了。全局上限只是重复了连接池自己的排队，还更严苛。
 *       需要它的唯一场景是"好几个爆款同时来，各占 4 条合起来超过池大小"，那时系统本来就
 *       饱和了，可以按池大小显式调小；默认不该替用户做这个决定。</li>
 * </ol>
 *
 * 获取顺序：先拿每-SKU 许可再拿全局许可。反过来的话，在热点 SKU 上排队的请求会一直攥着
 * 全局许可，把别的 SKU 也饿死——正是要避免的事。
 *
 * <h3>超时与拒绝</h3>
 *
 * 等许可最多 {@code max-wait}（默认 200ms，用来抹平突发），等不到就抛 {@code RATE_LIMITED}
 * 让用户重试。排队等许可时<b>不占数据库连接</b>——这是它与"让请求在行锁上排队"的本质区别。
 *
 * <p>一个 SKU 的信号量只在该 SKU 真实存在时才创建（{@link OrderService#place} 先做存在性
 * 检查再进来），所以 map 的大小以商品目录为界，外部无法靠乱填 skuNo 撑大它。
 */
@Component
public class PlacementGate {

    private static final Logger log = LoggerFactory.getLogger(PlacementGate.class);

    private final int perSku;
    private final long maxWaitNanos;
    private final Semaphore total;
    private final ConcurrentHashMap<String, Semaphore> perSkuPermits = new ConcurrentHashMap<>();
    private final AtomicLong rejected = new AtomicLong();
    private volatile long lastRejectLogMs;

    public PlacementGate(
            @Value("${smartmall.order.gate.max-concurrent-per-sku:4}") int perSku,
            @Value("${smartmall.order.gate.max-concurrent-total:64}") int total,
            @Value("${smartmall.order.gate.max-wait:PT0.2S}") Duration maxWait) {
        if (perSku < 1 || total < 1) {
            throw new IllegalArgumentException(
                    "下单舱壁的并发上限必须 ≥ 1（每-SKU " + perSku + "，全局 " + total + "）。"
                            + "要关闭舱壁请把它们配成很大的数，而不是 0——0 会让所有下单都被拒绝");
        }
        this.perSku = perSku;
        this.total = new Semaphore(total);
        this.maxWaitNanos = Math.max(0L, maxWait.toNanos());
    }

    /** 进入。拿不到就抛 {@link ErrorCode#RATE_LIMITED}。务必用 try-with-resources。 */
    public Permit enter(String skuNo) {
        long deadline = System.nanoTime() + maxWaitNanos;
        Semaphore s = perSkuPermits.computeIfAbsent(skuNo, k -> new Semaphore(perSku));

        if (!tryAcquire(s, deadline)) {
            throw reject(skuNo, "该商品同时下单的人太多");
        }
        if (!tryAcquire(total, deadline)) {
            // 这一行不能漏：拿到了每-SKU 许可却没拿到全局许可，必须把前者还回去，
            // 否则每被拒绝一次就永久漏掉一个许可，几次之后这个 SKU 再也进不来
            s.release();
            throw reject(skuNo, "店铺此刻下单的人太多");
        }
        return new Permit(s, total);
    }

    private boolean tryAcquire(Semaphore s, long deadlineNanos) {
        try {
            return s.tryAcquire(Math.max(0L, deadlineNanos - System.nanoTime()), TimeUnit.NANOSECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();       // 保留中断标志，让上层有机会响应
            return false;
        }
    }

    private BizException reject(String skuNo, String why) {
        long n = rejected.incrementAndGet();
        // 被拒绝是常态（热点时每秒成百上千次），逐条打日志本身就是一场事故；每秒最多一条
        long now = System.currentTimeMillis();
        if (now - lastRejectLogMs >= 1000) {
            lastRejectLogMs = now;
            log.warn("下单舱壁拒绝：{}（skuNo={}，累计拒绝 {} 次）", why, skuNo, n);
        }
        return new BizException(ErrorCode.RATE_LIMITED, why + "，请稍后再试");
    }

    /** 累计被拒绝的次数。运维看"是不是在限流"用。 */
    public long rejected() {
        return rejected.get();
    }

    /** 当前全局可用许可数。测试与排查用。 */
    int availableTotal() {
        return total.availablePermits();
    }

    /** 已建立信号量的 SKU 数。测试用：它应以商品目录为界，不能被乱填的 skuNo 撑大。 */
    int trackedSkus() {
        return perSkuPermits.size();
    }

    int availableFor(String skuNo) {
        Semaphore s = perSkuPermits.get(skuNo);
        return s == null ? perSku : s.availablePermits();
    }

    /** 许可。{@link #close} 幂等——重复关闭不会多还一个许可。 */
    public static final class Permit implements AutoCloseable {
        private final Semaphore sku;
        private final Semaphore total;
        private final AtomicBoolean closed = new AtomicBoolean();

        private Permit(Semaphore sku, Semaphore total) {
            this.sku = sku;
            this.total = total;
        }

        @Override
        public void close() {
            if (closed.compareAndSet(false, true)) {
                total.release();                      // 与获取顺序相反
                sku.release();
            }
        }
    }
}
