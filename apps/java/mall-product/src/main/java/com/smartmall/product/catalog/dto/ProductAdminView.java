package com.smartmall.product.catalog.dto;

import java.util.List;
import java.util.Map;

/** 商家侧的商品视图，比买家侧多了 draft/off_shelf 与库存明细。 */
public record ProductAdminView(
        Long id, String productNo, String name, String shortName,
        Long categoryId, String brand, String subtitle, String description,
        String mainImage, String status,
        Map<String, String> attrs,
        List<SkuView> skus,
        /** 上架前的自检结果。空 = 可以上架。 */
        List<String> blockers
) {
    public record SkuView(String skuNo, String spec, java.math.BigDecimal price,
                          java.math.BigDecimal originPrice, Integer stock,
                          String status) {
    }
}
