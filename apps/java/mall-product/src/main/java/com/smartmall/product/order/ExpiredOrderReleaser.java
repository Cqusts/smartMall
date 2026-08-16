package com.smartmall.product.order;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;


/**
 * 超时未支付订单的库存释放任务。
 *
 * <p>下单即扣库存（预占）是防超卖的前提——判断"有没有货"和"把货占住"必须是
 * 同一个动作，放到支付时再扣就又出现窗口了。代价是没付钱的单会占着货，
 * 所以必须有人来回收，否则一批放弃支付的订单能把热销 SKU 永久锁死：
 * 页面显示无货，而实际一件都没卖出去。
 *
 * <p><b>30 分钟这个默认值是拍的，不是算的。</b>真实电商这个数由支付渠道的
 * 超时、大促时的库存周转速度共同决定，通常在 15–30 分钟。这里取上限是因为
 * 演示场景下宁可让用户有充裕时间，也不要在演示到一半时订单自己没了。
 * 生产环境应当按渠道实测重新定，配置项是 {@code smartmall.order.payment-ttl}。
 *
 * <p>关掉它：{@code smartmall.order.release-expired.enabled=false}。
 * 测试里就是关掉的——定时任务在后台自己跑会让测试结果不可复现，
 * 测试直接调 {@link OrderService#releaseExpired} 才能精确控制时间点。
 */
@Component
@ConditionalOnProperty(
        name = "smartmall.order.release-expired.enabled",
        havingValue = "true",
        matchIfMissing = true)
public class ExpiredOrderReleaser {

    private static final Logger log = LoggerFactory.getLogger(ExpiredOrderReleaser.class);

    private final OrderService orderService;

    public ExpiredOrderReleaser(OrderService orderService) {
        this.orderService = orderService;
        // 超时时长与批量上限都从 OrderService 取，不在这里再配一份 ——
        // 出参里的 expiresAt 用的是同一个值，两处各读一次配置迟早会漂：
        // 页面说「还有 30 分钟」而任务 15 分钟就把单收走了
        log.info("超时释放已启用：超时 {}", orderService.paymentTtl());
    }

    /**
     * {@code fixedDelay} 而不是 {@code fixedRate}：按「上一轮结束后再等 N」计时。
     * fixedRate 在一轮没跑完时会叠下一轮，积压时几个任务并行扫同一批订单，
     * 白白增加锁竞争——虽然不会出错（条件更新兜着），但没有意义。
     */
    @Scheduled(
            fixedDelayString = "${smartmall.order.release-expired.interval:PT1M}",
            initialDelayString = "${smartmall.order.release-expired.initial-delay:PT30S}")
    public void releaseExpired() {
        try {
            orderService.releaseExpired();
        } catch (Exception e) {
            // 定时任务里异常必须自己吞掉并记日志。抛出去的话 Spring 的调度器
            // 会记一条然后**继续按计划执行**，但如果换成 fixedRate 或将来改用
            // 别的调度实现，未捕获异常可能让这个任务彻底停摆——库存从此不再
            // 回收，而且没有任何人会发现
            log.error("超时释放任务执行失败，下一轮继续", e);
        }
    }
}
