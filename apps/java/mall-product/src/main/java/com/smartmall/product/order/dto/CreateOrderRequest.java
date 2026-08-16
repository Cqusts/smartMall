package com.smartmall.product.order.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

/**
 * 下单请求。
 *
 * <p><b>注意这里没有 price 字段，是刻意的。</b>金额由服务端按 SKU 表算，
 * 客户端传不进来。让前端传价格，等于把改价权交给任何会开浏览器控制台的人。
 *
 * <p><b>userId 目前从请求体传，这是个已知的临时方案。</b>真实系统里它必须
 * 来自会话或 JWT——现在这样，任何人都能以任意身份下单。项目还没有认证体系
 * （M0–M7 路线图里没排），所以这里先显式地把它标出来，而不是假装安全。
 * 接入认证时改动只在控制器一层：把参数换成从 SecurityContext 取。
 */
public record CreateOrderRequest(

        /**
         * 幂等键，由客户端生成（一次下单动作一个 UUID，重试时复用同一个）。
         * 服务端靠它去重，见迁移 007 的唯一索引。
         */
        @NotBlank(message = "requestId 不能为空")
        @Size(max = 64, message = "requestId 最长 64 字符")
        String requestId,

        @NotNull(message = "userId 不能为空")
        @Positive(message = "userId 必须为正")
        Long userId,

        @NotBlank(message = "skuNo 不能为空")
        @Size(max = 64, message = "skuNo 最长 64 字符")
        String skuNo,

        /**
         * 上界 20 不是业务规则，是**防滥用的闸门**：不设上界的话，一次请求
         * 就能把一个 SKU 的库存清空，这是最省事的一种恶意下单。
         */
        @NotNull(message = "quantity 不能为空")
        @Min(value = 1, message = "quantity 至少为 1")
        @Max(value = 20, message = "单笔最多 20 件")
        Integer quantity
) {
}
