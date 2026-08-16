package com.smartmall.product;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 商品 · SKU · 类目 · 库存价格
 */
@SpringBootApplication(scanBasePackages = {"com.smartmall.product", "com.smartmall.common"})
// 按领域分包，每个领域自带 mapper 子包。@MapperScan 不支持 ant 通配，
// 所以新增领域时要在这里补一行——显式列出比一个匹配不上的通配符好排查
@MapperScan({"com.smartmall.product.mapper", "com.smartmall.product.order.mapper"})
public class ProductApplication {

    public static void main(String[] args) {
        SpringApplication.run(ProductApplication.class, args);
    }
}
