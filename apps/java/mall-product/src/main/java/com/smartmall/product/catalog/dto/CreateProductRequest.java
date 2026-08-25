package com.smartmall.product.catalog.dto;

import jakarta.validation.Valid;
import jakarta.validation.constraints.*;

import java.util.List;
import java.util.Map;

/**
 * 上架商品。
 *
 * <p><b>建出来是 draft，不是直接在售。</b>商家填到一半跑去吃饭，页面上不该
 * 出现一个买不了的商品。上架是一个独立动作，且要过前置校验。
 *
 * <p>{@code attrs} 不是可有可无的装饰：运营 Agent 写文案时**只能用这里的
 * 事实**，填不填决定了之后机器能不能替你写。空着的话它会明说"属性表是空的"
 * 而不是硬编（见 marketing/nodes.py 的 load 节点）。
 */
public record CreateProductRequest(

        @NotBlank(message = "productNo 不能为空")
        @Size(max = 64, message = "productNo 最长 64 字符")
        String productNo,

        @NotBlank(message = "name 不能为空")
        @Size(max = 256, message = "name 最长 256 字符")
        String name,

        @Size(max = 64, message = "shortName 最长 64 字符")
        String shortName,

        @NotNull(message = "categoryId 不能为空")
        @Positive(message = "categoryId 必须为正")
        Long categoryId,

        @Size(max = 128) String brand,
        @Size(max = 512) String subtitle,
        String description,
        @Size(max = 512) String mainImage,

        /** 结构化属性：材质 / 克重 / 产地 / 洗涤方式… */
        Map<String, String> attrs,

        /** 至少给一个 SKU 才有意义——但这里不强制，允许先建壳再补规格。 */
        @Valid List<SkuSpec> skus
) {
}
