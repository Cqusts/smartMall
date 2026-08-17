package com.smartmall.common.time;

import java.util.TimeZone;

/**
 * 把 JVM 默认时区钉死，让它与数据库一致。
 *
 * <p><b>不钉的话，同一行数据里会出现两个时钟。</b>业务代码用
 * {@code LocalDateTime.now()} 写入的列走 JVM 时区，而 SQL 里 {@code NOW()}
 * 写入的列走 MySQL 时区。两者不一致时，一笔订单可以「15:25 下单、23:25 发货」——
 * 而客服 Agent 正是照着这些字段回答"我的货什么时候发的"，于是它会向用户
 * 陈述一段根本没发生过的 8 小时延迟。这不是显示问题，是存进库里的脏数据。
 *
 * <p>实测就是这么撞上的：docker-compose 给容器设了 {@code TZ: Asia/Shanghai}，
 * 所以容器里跑没问题；而 README 推荐的本地开发方式是「只用 compose 起 MySQL，
 * 应用在 IDE 里直跑」——那条路径不经过 compose，JVM 用的是机器时区，
 * 在 UTC 机器上就差了 8 小时。**部署方式不同、数据就不同**，这种坑最难查。
 *
 * <p>取值顺序：系统属性 {@code smartmall.timezone} → 环境变量 {@code TZ} →
 * 默认 {@code Asia/Shanghai}（与 deploy/docker-compose 各处一致）。
 *
 * <p>必须在 {@code SpringApplication.run} <b>之前</b>调用：连接池、日志、
 * JSON 序列化都会在启动过程中读取默认时区，晚了就有组件已经拿到旧值。
 */
public final class AppTimeZone {

    public static final String DEFAULT_ZONE = "Asia/Shanghai";

    private AppTimeZone() {
    }

    /** @return 实际生效的时区 ID */
    public static String apply() {
        String configured = System.getProperty("smartmall.timezone");
        if (configured == null || configured.isBlank()) {
            configured = System.getenv("TZ");
        }
        if (configured == null || configured.isBlank()) {
            configured = DEFAULT_ZONE;
        }

        TimeZone zone = TimeZone.getTimeZone(configured);
        // getTimeZone 对无法识别的 ID 会**静默**返回 GMT，不抛异常。
        // 那正是最坏的情况：配置写错了，时间还是错的，而且没有任何提示
        if (!zone.getID().equals(configured)) {
            System.err.printf(
                    "[smartMall] 无法识别的时区 \"%s\"，回退到 %s%n",
                    configured, DEFAULT_ZONE);
            zone = TimeZone.getTimeZone(DEFAULT_ZONE);
        }
        TimeZone.setDefault(zone);
        return zone.getID();
    }
}
