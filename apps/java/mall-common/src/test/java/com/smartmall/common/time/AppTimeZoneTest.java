package com.smartmall.common.time;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.TimeZone;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 时区钉死逻辑。
 *
 * <p>这段代码的价值在于**它防的那个 bug 单元测试看不见**：JVM 与 MySQL 时区
 * 不一致时，Java 写的 created_at 与 SQL 里 NOW() 写的 shipped_at 会差几小时，
 * 客服照着这些字段就会讲出一段没发生过的发货延迟。真实一致性由
 * deploy/scripts/verify-orders.py lifecycle 对真库验证。
 *
 * <p>这里只测能测的部分：取值优先级，以及配错时不能悄悄放过。
 */
class AppTimeZoneTest {

    TimeZone original;

    @BeforeEach
    void save() {
        original = TimeZone.getDefault();
    }

    @AfterEach
    void restore() {
        TimeZone.setDefault(original);
        System.clearProperty("smartmall.timezone");
    }

    @Test
    @DisplayName("不配任何东西时用 Asia/Shanghai —— 与 compose 各处一致")
    void defaults_to_shanghai() {
        // 环境变量 TZ 在测试进程里通常没设；设了的话这条断言的是它，同样合理
        String expected = System.getenv("TZ") != null && !System.getenv("TZ").isBlank()
                ? System.getenv("TZ") : AppTimeZone.DEFAULT_ZONE;

        assertThat(AppTimeZone.apply()).isEqualTo(expected);
        assertThat(TimeZone.getDefault().getID()).isEqualTo(expected);
    }

    @Test
    @DisplayName("系统属性优先级最高")
    void system_property_wins() {
        System.setProperty("smartmall.timezone", "Europe/Berlin");

        assertThat(AppTimeZone.apply()).isEqualTo("Europe/Berlin");
        assertThat(TimeZone.getDefault().getID()).isEqualTo("Europe/Berlin");
    }

    /**
     * TimeZone.getTimeZone 对无法识别的 ID **静默返回 GMT**，不抛异常。
     * 那是最坏的情况：配置写错了，时间跟着错，而且没有任何提示。
     * 所以这里必须回退到已知的默认值并告警，不能让 GMT 蒙混过关。
     */
    @Test
    @DisplayName("时区 ID 写错时回退到默认值，而不是悄悄变成 GMT")
    void unknown_zone_falls_back_instead_of_silently_becoming_gmt() {
        System.setProperty("smartmall.timezone", "Mars/Olympus_Mons");

        assertThat(AppTimeZone.apply()).isEqualTo(AppTimeZone.DEFAULT_ZONE);
        assertThat(TimeZone.getDefault().getID())
                .as("不能是 GMT —— 那正是 getTimeZone 静默失败的表现")
                .isEqualTo(AppTimeZone.DEFAULT_ZONE);
    }

    @Test
    @DisplayName("空字符串当作没配")
    void blank_is_treated_as_unset() {
        System.setProperty("smartmall.timezone", "   ");
        String expected = System.getenv("TZ") != null && !System.getenv("TZ").isBlank()
                ? System.getenv("TZ") : AppTimeZone.DEFAULT_ZONE;

        assertThat(AppTimeZone.apply()).isEqualTo(expected);
    }
}
