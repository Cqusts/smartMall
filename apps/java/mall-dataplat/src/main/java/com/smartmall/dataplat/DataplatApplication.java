package com.smartmall.dataplat;

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
        SpringApplication.run(DataplatApplication.class, args);
    }
}
