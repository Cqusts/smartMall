package com.smartmall.product.order.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

/**
 * 下单请求。
 *
 * <p><b>注意这里没有 price 字段，是刻意的。</b>金额由服务端按 SKU 表算，
 * 客户端传不进来。让前端传价格，等于把改价权交给任何会开浏览器控制台的人。
 *
 * <p><b>这里也没有 userId，同样是刻意的。</b>它曾经在这个请求体里，那意味着
 * 「任何人都能以任意身份下单」——而 OrderService 的归属校验恰好拿它当条件，
 * 于是校验形同虚设。现在身份只从 JWT 的签名里来（{@code @CurrentUser}），
 * 请求体里再也没有可以冒充别人的字段。
 */
public record CreateOrderRequest(

        /**
         * 幂等键，由客户端生成（一次下单动作一个 UUID，重试时复用同一个）。
         * 服务端靠它去重，见迁移 007 的唯一索引。
         */
        @NotBlank(message = "requestId 不能为空")
        @Size(max = 64, message = "requestId 最长 64 字符")
        String requestId,

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
