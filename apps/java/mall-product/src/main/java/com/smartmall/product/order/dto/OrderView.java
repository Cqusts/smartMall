package com.smartmall.product.order.dto;

import com.smartmall.product.order.entity.MallOrder;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 订单出参。
 *
 * <p>不直接回实体：{@code user_id} 不能出现在响应里。它是越权校验的依据，
 * 回给前端等于告诉调用方"这张单属于谁"，而客服工具层那边正是靠它做鉴权的
 * （见 {@code tools.py} 的 get_order_status）。两边口径要一致。
 *
 * @param idempotentHit true 表示本次请求命中幂等、没有新建订单。
 *                      前端据此把提示从「下单成功」换成「该订单已创建」，
 *                      否则用户双击两次会看到两次「下单成功」，以为买了两单。
 */
public record OrderView(
        String orderNo,
        Long productId,
        String skuNo,
        String spec,
        Integer quantity,
        BigDecimal amount,
        String status,
        LocalDateTime createdAt,
        boolean idempotentHit
) {

    public static OrderView of(MallOrder o, boolean idempotentHit) {
        return new OrderView(
                o.getOrderNo(),
                o.getProductId(),
                o.getSkuNo(),
                o.getSpec(),
                o.getQuantity(),
                o.getAmount(),
                o.getStatus(),
                o.getCreatedAt(),
                idempotentHit
        );
    }
}
