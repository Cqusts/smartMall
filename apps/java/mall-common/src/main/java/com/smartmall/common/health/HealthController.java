package com.smartmall.common.health;

import com.smartmall.common.api.ApiResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.lang.management.ManagementFactory;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 轻量健康检查端点，供 {@code deploy/scripts/health-check.sh} 与容器 healthcheck 使用。
 *
 * <p>刻意与 Actuator 的 {@code /actuator/health} 区分职责：
 * <ul>
 *   <li>{@code /health} —— 只回答"进程活着吗"，不探测下游依赖，永远快速返回。
 *       用于容器存活探针，避免 MySQL 抖动时容器被反复重启。</li>
 *   <li>{@code /actuator/health} —— 深度检查，探测 DB / Redis / Kafka，用于就绪探针与监控。</li>
 * </ul>
 *
 * <p>使用 {@code @GetMapping} 注解式控制器，Servlet 与 WebFlux 栈均可加载，
 * 因此 mall-gateway（WebFlux）与业务服务（Servlet）共用同一份实现。
 */
@RestController
public class HealthController {

    @Value("${spring.application.name:unknown}")
    private String serviceName;

    @Value("${smartmall.version:0.1.0-SNAPSHOT}")
    private String version;

    @GetMapping("/health")
    public ApiResponse<Map<String, Object>> health() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("service", serviceName);
        body.put("version", version);
        body.put("status", "UP");
        body.put("uptimeMs", ManagementFactory.getRuntimeMXBean().getUptime());
        return ApiResponse.ok(body);
    }
}
