package com.smartmall.kpi;

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
        SpringApplication.run(KpiApplication.class, args);
    }
}
