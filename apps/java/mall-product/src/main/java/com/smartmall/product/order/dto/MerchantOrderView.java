package com.smartmall.product.order.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 商家侧的订单视图。
 *
 * <p><b>为什么不复用 {@link OrderView}。</b>那个是买家侧的契约，刻意<b>不含
 * userId</b>（买家侧还有一条测试断言响应里不出现这个字段）。而商家恰恰需要
 * 知道是谁下的单——两边要看的东西不一样，硬塞进一个 record 的结果是买家
 * 也能看到本不该给他的字段。
 *
 * <p>这里只暴露 {@code userId}，没有姓名、电话、收货地址——那些这个项目
 * 还没有。真接入时它们属于**个人信息**，商家侧要不要看全、看多久，
 * 是要单独定策略的，不能顺手加个字段了事。
 */
public record MerchantOrderView(
        String orderNo,
        Long userId,
        Long productId,
        String skuNo,
        String spec,
        Integer quantity,
        BigDecimal amount,
        String status,
        LocalDateTime createdAt,
        String expressCompany,
        String expressNo,
        String refundReason
) {
}
