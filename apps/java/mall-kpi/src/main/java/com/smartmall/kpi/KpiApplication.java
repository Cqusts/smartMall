package com.smartmall.kpi;

import com.smartmall.common.time.AppTimeZone;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 销售考核：指标计算 · 评分存储 · 报表 · 申诉
 */
@SpringBootApplication(scanBasePackages = {"com.smartmall.kpi", "com.smartmall.common"})
@MapperScan("com.smartmall.kpi.mapper")
public class KpiApplication {

    public static void main(String[] args) {
        // 必须在 run() 之前：连接池、日志、JSON 序列化都会在启动过程中
        // 读默认时区，晚了就有组件已经拿到旧值。五个服务一致，跨服务
        // 对时间戳时才不会一个 +08:00 一个 Z。
        AppTimeZone.apply();
        SpringApplication.run(KpiApplication.class, args);
    }
}
