package com.smartmall.product.order.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 退款申请 / 驳回的理由。
 *
 * <p><b>没有 amount 字段。</b>退款金额由服务端取订单实付，客户端传不进来——
 * 理由和下单不接受 price 完全一样：让前端决定出款金额，等于把付款权交出去。
 * 将来支持部分退款时，金额也应由后端按退货明细算，而不是照抄请求体。
 */
public record RefundRequest(

        @NotBlank(message = "理由不能为空")
        @Size(max = 200, message = "理由最长 200 字")
        String reason
) {
}
