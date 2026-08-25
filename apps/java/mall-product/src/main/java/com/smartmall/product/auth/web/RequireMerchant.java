package com.smartmall.product.auth.web;

import java.lang.annotation.*;

/**
 * 标在商家侧接口上：只有 {@code role=merchant} 能调。
 *
 * <p>在它存在之前，{@code /api/product/admin/orders/**}（发货、确认送达、
 * 退款审批、退款驳回）<b>一个校验都没有</b>——知道订单号就能把别人的单发掉、
 * 把退款批掉。
 */
@Target({ElementType.METHOD, ElementType.TYPE})
@Retention(RetentionPolicy.RUNTIME)
@Documented
public @interface RequireMerchant {
}
