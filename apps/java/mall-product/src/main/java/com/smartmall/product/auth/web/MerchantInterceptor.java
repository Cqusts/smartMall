package com.smartmall.product.auth.web;

import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.auth.AuthException;
import com.smartmall.common.auth.AuthPrincipal;
import com.smartmall.common.exception.BizException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerInterceptor;

/** 执行 {@link RequireMerchant}。 */
@Component
public class MerchantInterceptor implements HandlerInterceptor {

    private static final Logger log = LoggerFactory.getLogger(MerchantInterceptor.class);

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response,
                             Object handler) {
        if (!(handler instanceof HandlerMethod method)) {
            return true;
        }
        boolean needed = method.hasMethodAnnotation(RequireMerchant.class)
                || method.getBeanType().isAnnotationPresent(RequireMerchant.class);
        if (!needed) {
            return true;
        }

        AuthPrincipal principal = AuthContext.get();
        if (principal == null) {
            throw new AuthException("请先登录");
        }
        if (!principal.isMerchant()) {
            // 拒绝要留痕：买家账号去点发货，本身就是需要告警的信号
            log.warn("非商家账号尝试访问商家接口 userId={} role={} path={}",
                    principal.userId(), principal.role(), request.getRequestURI());
            throw new BizException(ErrorCode.FORBIDDEN, "该操作仅限商家");
        }
        return true;
    }
}
