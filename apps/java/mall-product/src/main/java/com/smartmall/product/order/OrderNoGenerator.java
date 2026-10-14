package com.smartmall.product.order;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.lang.management.ManagementFactory;
import java.net.InetAddress;
import java.time.Clock;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 订单号生成。20 位纯数字：{@code yyyyMMddHHmmss}（14）+ 节点号（2）+ 序号（4）。
 *
 * <p><b>为什么不再用「时间戳 + 6 位随机数」。</b>那是生日问题：同一秒内有 n 笔订单，
 * 从 10⁶ 个随机后缀里抽，碰撞概率约 n²/(2×10⁶)。压测 200 TPS 时约 2 万单就撞出
 * 一次 {@code Duplicate entry ... for key 'mall_order.uk_order_no'}，用户拿到 500。
 * 事务会回滚所以不超卖，但订单丢了；而且碰撞率随流量<b>平方</b>增长，
 * 恰恰在大促最需要它稳的时候最不稳（每笔订单失败率约 n/(2×10⁶)：
 * 1000 TPS 时 0.05%，5000 TPS 时 0.25%）。
 *
 * <p><b>现在的做法：同一节点内按构造无碰撞。</b>序号是进程内的原子自增，
 * 每个调用拿到不同的值，同一秒内只要少于 10⁴ 笔就一定不重复——一个节点的下单
 * 能力远到不了这个量级（热点 SKU 实测上限约 450 TPS，分散下单约 2000 TPS）。
 *
 * <p>多个实例之间靠节点号（0–99）隔开。没配置时由「主机名 + 进程号 + 启动时刻」
 * 散列出来，<b>两个实例散列到同一个值的概率是 1%</b>，那时它们仍可能在同一秒
 * 撞同一个序号——所以这里不是唯一的防线：{@link OrderService#place} 对
 * {@code uk_order_no} 冲突还有整事务重试兜底。生产上多实例部署应显式配
 * {@code smartmall.order.node-id}，让这条兜底变成真正的"几乎不会触发"。
 *
 * <p>格式保持 20 位纯数字：店铺页的订单号白名单（{@code \d{14,24}}）、
 * 客服从用户话里抠订单号的正则（{@code \d{10,24}}）都依赖它。
 */
@Component
public class OrderNoGenerator {

    private static final DateTimeFormatter TIME = DateTimeFormatter.ofPattern("yyyyMMddHHmmss");
    private static final int SEQ_MODULUS = 10_000;
    private static final int NODE_MODULUS = 100;

    private final int nodeId;
    private final Clock clock;
    private final AtomicInteger seq;

    @Autowired
    public OrderNoGenerator(@Value("${smartmall.order.node-id:-1}") int configuredNodeId) {
        this(configuredNodeId >= 0 ? configuredNodeId : derivedNodeId(), Clock.systemDefaultZone());
    }

    /** 测试用：固定节点号与时钟。 */
    OrderNoGenerator(int nodeId, Clock clock) {
        if (nodeId < 0 || nodeId >= NODE_MODULUS) {
            throw new IllegalArgumentException(
                    "smartmall.order.node-id 必须在 [0, " + NODE_MODULUS + ") 内，实际 " + nodeId);
        }
        this.nodeId = nodeId;
        this.clock = clock;
        // 起点随机：重启后序号不会总是从 0 开始，同一秒内重启前后的两个进程不会
        // 恰好从同一个序号发起
        this.seq = new AtomicInteger((int) (System.nanoTime() % SEQ_MODULUS));
    }

    public String next() {
        // 注意取模放在 getAndIncrement 之后：AtomicInteger 溢出成负数时
        // floorMod 仍给出 [0, 10⁴)，直接 % 会得到负号，把订单号撑出 21 位
        int s = Math.floorMod(seq.getAndIncrement(), SEQ_MODULUS);
        return LocalDateTime.now(clock).format(TIME)
                + String.format("%02d%04d", nodeId, s);
    }

    public int nodeId() {
        return nodeId;
    }

    static int derivedNodeId() {
        String host;
        try {
            host = InetAddress.getLocalHost().getHostName();
        } catch (Exception e) {
            host = "unknown-host";
        }
        String pid = ManagementFactory.getRuntimeMXBean().getName();   // "pid@host"
        long start = ManagementFactory.getRuntimeMXBean().getStartTime();
        return Math.floorMod((host + "|" + pid + "|" + start).hashCode(), NODE_MODULUS);
    }
}
