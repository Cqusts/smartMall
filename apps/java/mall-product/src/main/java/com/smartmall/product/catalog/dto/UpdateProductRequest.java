package com.smartmall.product.catalog.dto;

import jakarta.validation.constraints.Size;

import java.util.Map;

/** 改商品基本信息。字段为 null 表示不改。 */
public record UpdateProductRequest(
        @Size(max = 256) String name,
        @Size(max = 64) String shortName,
        @Size(max = 128) String brand,
        @Size(max = 512) String subtitle,
        String description,
        @Size(max = 512) String mainImage,
        /** 非 null 时**整体替换**属性表，不是增量合并——增量合并没法删属性。 */
        Map<String, String> attrs
) {
}
