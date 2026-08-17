package com.smartmall.product.order.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/** 发货请求：快递公司与运单号。 */
public record ShipRequest(

        @NotBlank(message = "快递公司不能为空")
        @Size(max = 32, message = "快递公司名最长 32 字")
        String company,

        /**
         * 运单号限定为字母数字与连字符。它会被原样展示给用户、也会被客服
         * 读出来，放任意字符进来等于给页面开了一个注入口子（前端虽然有 esc，
         * 但输入侧也该把形状约束住，而不是全靠输出侧兜）。
         */
        @NotBlank(message = "运单号不能为空")
        @Size(max = 64, message = "运单号最长 64 字符")
        @Pattern(regexp = "[A-Za-z0-9-]+", message = "运单号只能是字母、数字与连字符")
        String expressNo
) {
}
