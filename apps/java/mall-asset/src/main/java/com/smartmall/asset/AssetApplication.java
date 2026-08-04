package com.smartmall.asset;

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
        SpringApplication.run(AssetApplication.class, args);
    }
}
