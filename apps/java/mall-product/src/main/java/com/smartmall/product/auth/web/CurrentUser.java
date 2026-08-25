package com.smartmall.product.auth.web;

import java.lang.annotation.*;

/**
 * 注入当前调用者的身份。
 *
 * <p>取代此前的 {@code @RequestParam("userId") Long userId} —— 那个写法等于
 * 「你说你是谁，你就是谁」，越权校验的 SQL 写得再对，条件里那个 userId
 * 本身就是攻击者填的。
 *
 * <p>这个注解拿到的身份只可能来自 {@link JwtAuthFilter} 校验过签名的令牌。
 */
@Target(ElementType.PARAMETER)
@Retention(RetentionPolicy.RUNTIME)
@Documented
public @interface CurrentUser {

    /** 为 true 时未登录直接 401；为 false 时注入 null（供可选登录的接口用）。 */
    boolean required() default true;
}
