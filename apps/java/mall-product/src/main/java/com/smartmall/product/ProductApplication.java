package com.smartmall.product;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 商品 · SKU · 类目 · 库存价格
 */
@SpringBootApplication(scanBasePackages = {"com.smartmall.product", "com.smartmall.common"})
@MapperScan("com.smartmall.product.mapper")
public class ProductApplication {

    public static void main(String[] args) {
        SpringApplication.run(ProductApplication.class, args);
    }
}
