package com.smartmall.common.auth;

/**
 * 当前调用者的身份。
 *
 * <p><b>这个对象只能由 {@link JwtService#verify} 产出，不能由请求参数拼出来。</b>
 * 在它存在之前，订单接口是 {@code @RequestParam("userId") Long userId} —— 也就是
 * 「你说你是谁，你就是谁」。越权校验的 SQL 写得再对也没用：条件里那个 userId
 * 本身就是攻击者填的。
 *
 * @param userId   用户 ID，与 {@code mall_order.user_id} 同一套编号
 * @param username 登录名，只用于日志与展示
 * @param role     {@code customer} 或 {@code merchant}
 */
public record AuthPrincipal(Long userId, String username, String role) {

    public static final String CUSTOMER = "customer";
    public static final String MERCHANT = "merchant";

    public boolean isMerchant() {
        return MERCHANT.equals(role);
    }
}
