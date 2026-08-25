package com.smartmall.product;

import com.smartmall.common.time.AppTimeZone;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 商品 · SKU · 类目 · 库存价格
 */
@SpringBootApplication(scanBasePackages = {"com.smartmall.product", "com.smartmall.common"})
// 按领域分包，每个领域自带 mapper 子包。@MapperScan 不支持 ant 通配，
// 所以新增领域时要在这里补一行——显式列出比一个匹配不上的通配符好排查
@MapperScan({"com.smartmall.product.mapper", "com.smartmall.product.order.mapper",
             "com.smartmall.product.auth.mapper"})
// 超时未支付订单的库存释放任务需要它。任务本身可以用
// smartmall.order.release-expired.enabled=false 关掉
@EnableScheduling
public class ProductApplication {

    public static void main(String[] args) {
        // 必须在 run 之前：连接池与日志都会在启动过程中读默认时区。
        // 不钉死的话，Java 写的 created_at 与 SQL 里 NOW() 写的 shipped_at
        // 会来自两个时钟，客服就会对用户讲出一段没发生过的发货延迟
        AppTimeZone.apply();
        SpringApplication.run(ProductApplication.class, args);
    }
}
