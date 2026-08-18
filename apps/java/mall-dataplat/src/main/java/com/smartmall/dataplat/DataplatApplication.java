package com.smartmall.dataplat;

import com.smartmall.common.time.AppTimeZone;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 数据中台业务侧：数据源登记 · 清洗任务 · 数据集版本发布
 */
@SpringBootApplication(scanBasePackages = {"com.smartmall.dataplat", "com.smartmall.common"})
@MapperScan("com.smartmall.dataplat.mapper")
public class DataplatApplication {

    public static void main(String[] args) {
        // 必须在 run() 之前：连接池、日志、JSON 序列化都会在启动过程中
        // 读默认时区，晚了就有组件已经拿到旧值。五个服务一致，跨服务
        // 对时间戳时才不会一个 +08:00 一个 Z。
        AppTimeZone.apply();
        SpringApplication.run(DataplatApplication.class, args);
    }
}
