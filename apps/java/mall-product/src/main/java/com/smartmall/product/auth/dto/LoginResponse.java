package com.smartmall.product.auth.dto;

/**
 * 登录返回。
 *
 * <p><b>不含密码哈希，也不含任何用户表原始行。</b> 返回体里多一个字段的成本
 * 是零，泄露的成本不是——哈希拿到手就能离线爆破，而 BCrypt 挡得住在线试探
 * 挡不住无限次的离线尝试。
 *
 * <p>{@code userId} 是回给前端展示用的（"当前身份：演示买家"）。
 * <b>后端一律不信它</b>——身份只从 token 的签名里来。
 */
public record LoginResponse(String token, Long userId, String username,
                            String nickname, String role) {
}
