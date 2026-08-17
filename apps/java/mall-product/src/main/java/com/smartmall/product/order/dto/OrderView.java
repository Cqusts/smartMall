package com.smartmall.product.order.dto;

import com.smartmall.product.order.entity.MallOrder;

import java.math.BigDecimal;
import java.time.Duration;
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
 * @param expiresAt     待支付订单的库存预占到期时刻；非待支付时为 null。
 *                      <b>由服务端算而不是让前端写死「30 分钟」</b>——超时时长是
 *                      配置项（{@code smartmall.order.payment-ttl}），前端硬编码
 *                      的话，改了配置页面就开始骗人。这条规矩和商品页与客服
 *                      读同一份数据是同一条：显示的东西必须来自真实来源。
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
        LocalDateTime expiresAt,
        String expressCompany,
        String expressNo,
        /**
         * 物流轨迹，原样透传的 JSON 字符串 {@code [{"ts","desc"}]}。
         * 与客服工具层 {@code get_order_status} 读的是同一份、同一个形状——
         * 页面显示什么，客服就得答什么。
         */
        String tracks,
        String refundReason,
        String refundRejectReason,
        boolean idempotentHit
) {

    public static OrderView of(MallOrder o, boolean idempotentHit, Duration paymentTtl) {
        LocalDateTime expiresAt =
                "pending_payment".equals(o.getStatus()) && o.getCreatedAt() != null
                        ? o.getCreatedAt().plus(paymentTtl)
                        : null;
        return new OrderView(
                o.getOrderNo(),
                o.getProductId(),
                o.getSkuNo(),
                o.getSpec(),
                o.getQuantity(),
                o.getAmount(),
                o.getStatus(),
                o.getCreatedAt(),
                expiresAt,
                o.getExpressCompany(),
                o.getExpressNo(),
                o.getTracks(),
                o.getRefundReason(),
                o.getRefundRejectReason(),
                idempotentHit
        );
    }
}
