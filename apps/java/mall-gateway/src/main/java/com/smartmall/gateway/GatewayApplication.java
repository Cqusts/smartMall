package com.smartmall.gateway;

import com.smartmall.common.time.AppTimeZone;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 网关服务。路由规则声明在 application.yml，不在代码里硬编码，
 * 便于按环境覆盖（本地 localhost / 容器内服务名）。
 */
@SpringBootApplication(scanBasePackages = {"com.smartmall.gateway", "com.smartmall.common"})
public class GatewayApplication {

    public static void main(String[] args) {
        // 必须在 run() 之前：连接池、日志、JSON 序列化都会在启动过程中
        // 读默认时区，晚了就有组件已经拿到旧值。五个服务一致，跨服务
        // 对时间戳时才不会一个 +08:00 一个 Z。
        AppTimeZone.apply();
        SpringApplication.run(GatewayApplication.class, args);
    }
}
