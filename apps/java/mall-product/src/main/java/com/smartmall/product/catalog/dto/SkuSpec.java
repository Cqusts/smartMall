package com.smartmall.product.catalog.dto;

import jakarta.validation.constraints.*;

import java.math.BigDecimal;

/**
 * 建/改 SKU 的入参。
 *
 * @param spec 规格 JSON，形如 {@code {"颜色":"米白","尺码":"M"}}
 */
public record SkuSpec(
        @NotBlank(message = "skuNo 不能为空")
        @Size(max = 64, message = "skuNo 最长 64 字符")
        String skuNo,

        @NotBlank(message = "spec 不能为空")
        String spec,

        // 价格允许 0（赠品），但不能为负——负价格下单会算出负金额
        @NotNull(message = "price 不能为空")
        @DecimalMin(value = "0.0", message = "价格不能为负")
        @Digits(integer = 8, fraction = 2, message = "价格最多两位小数")
        BigDecimal price,

        @DecimalMin(value = "0.0", message = "原价不能为负")
        @Digits(integer = 8, fraction = 2, message = "原价最多两位小数")
        BigDecimal originPrice,

        @NotNull(message = "stock 不能为空")
        @Min(value = 0, message = "库存不能为负")
        Integer stock
) {
}
