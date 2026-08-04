package com.smartmall.gateway;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 网关服务。路由规则声明在 application.yml，不在代码里硬编码，
 * 便于按环境覆盖（本地 localhost / 容器内服务名）。
 */
@SpringBootApplication(scanBasePackages = {"com.smartmall.gateway", "com.smartmall.common"})
public class GatewayApplication {

    public static void main(String[] args) {
        SpringApplication.run(GatewayApplication.class, args);
    }
}
