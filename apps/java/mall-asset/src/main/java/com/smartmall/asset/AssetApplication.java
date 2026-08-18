package com.smartmall.asset;

import com.smartmall.common.time.AppTimeZone;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * AI 素材中心：素材元数据 · 商品关联 · 版本 · 审核流
 */
@SpringBootApplication(scanBasePackages = {"com.smartmall.asset", "com.smartmall.common"})
@MapperScan("com.smartmall.asset.mapper")
public class AssetApplication {

    public static void main(String[] args) {
        // 必须在 run() 之前：连接池、日志、JSON 序列化都会在启动过程中
        // 读默认时区，晚了就有组件已经拿到旧值。五个服务一致，跨服务
        // 对时间戳时才不会一个 +08:00 一个 Z。
        AppTimeZone.apply();
        SpringApplication.run(AssetApplication.class, args);
    }
}
