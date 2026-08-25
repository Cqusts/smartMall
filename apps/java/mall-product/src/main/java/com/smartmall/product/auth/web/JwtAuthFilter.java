package com.smartmall.product.auth.web;

import com.smartmall.common.auth.AuthException;
import com.smartmall.common.auth.AuthPrincipal;
import com.smartmall.common.auth.JwtService;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;

/**
 * 解析 {@code Authorization: Bearer} 并把身份放进 {@link AuthContext}。
 *
 * <p><b>这个过滤器只负责「认出你是谁」，不负责「你能不能访问」。</b>
 * 令牌无效时它<b>不拦</b>，只是不设置身份——放行与否交给
 * {@link CurrentUserArgumentResolver}（要身份的接口拿不到就 401）与
 * {@code @RequireMerchant}。
 *
 * <p>这么分是因为有些路径本来就不需要登录：商品列表、健康检查、登录接口
 * 本身。在过滤器里一刀切地拦，就得在这里维护一份白名单路径表——
 * 而那份表和控制器上的注解迟早会不一致，不一致的方向通常是**白名单
 * 忘了收窄**，也就是某个新接口悄悄变成公开的。让每个接口自己声明
 * 要不要身份，漏声明的表现是 401（吵闹但安全），而不是裸奔。
 */
@Component
public class JwtAuthFilter extends OncePerRequestFilter {

    private final JwtService jwtService;

    public JwtAuthFilter(JwtService jwtService) {
        this.jwtService = jwtService;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain chain)
            throws ServletException, IOException {
        try {
            String token = JwtService.bearer(request.getHeader("Authorization"));
            if (token != null) {
                AuthPrincipal principal = jwtService.verify(token);
                AuthContext.set(principal);
            }
        } catch (AuthException ignored) {
            // 令牌坏了就当没带。要身份的接口会 401，不要身份的照常放行
        }

        try {
            chain.doFilter(request, response);
        } finally {
            // **Tomcat 线程是复用的。** 不清会把这个请求的身份带给下一个，
            // 那是随机发生、只在高并发下出现的越权——比没有鉴权更难查
            AuthContext.clear();
        }
    }
}
