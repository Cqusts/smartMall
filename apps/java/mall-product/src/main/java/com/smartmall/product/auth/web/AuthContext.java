package com.smartmall.product.auth.web;

import com.smartmall.common.auth.AuthPrincipal;

/**
 * 当前请求的身份，放在 ThreadLocal 里。
 *
 * <p>用 ThreadLocal 而不是往 request attribute 里塞，是为了让 Service 层
 * 也能拿到（记日志、审计），而不必把 principal 一路当参数传下去。
 *
 * <p><b>必须在 finally 里 clear。</b> Tomcat 的线程是复用的，不清就会把
 * 上一个请求的身份带给下一个请求——那是比没有鉴权更糟的一种越权，
 * 因为它随机发生、只在高并发下出现。
 */
public final class AuthContext {

    private static final ThreadLocal<AuthPrincipal> CURRENT = new ThreadLocal<>();

    private AuthContext() {
    }

    public static void set(AuthPrincipal principal) {
        CURRENT.set(principal);
    }

    public static AuthPrincipal get() {
        return CURRENT.get();
    }

    public static void clear() {
        CURRENT.remove();
    }
}
