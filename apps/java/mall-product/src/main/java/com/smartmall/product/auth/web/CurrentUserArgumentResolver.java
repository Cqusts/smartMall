package com.smartmall.product.auth.web;

import com.smartmall.common.auth.AuthException;
import com.smartmall.common.auth.AuthPrincipal;
import org.springframework.core.MethodParameter;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.support.WebDataBinderFactory;
import org.springframework.web.context.request.NativeWebRequest;
import org.springframework.web.method.support.HandlerMethodArgumentResolver;
import org.springframework.web.method.support.ModelAndViewContainer;

/** 把 {@link CurrentUser} 标注的参数换成经过签名校验的身份。 */
@Component
public class CurrentUserArgumentResolver implements HandlerMethodArgumentResolver {

    @Override
    public boolean supportsParameter(MethodParameter parameter) {
        return parameter.hasParameterAnnotation(CurrentUser.class)
                && (AuthPrincipal.class.isAssignableFrom(parameter.getParameterType())
                    || Long.class.isAssignableFrom(parameter.getParameterType()));
    }

    @Override
    public Object resolveArgument(MethodParameter parameter,
                                  ModelAndViewContainer mav,
                                  NativeWebRequest request,
                                  WebDataBinderFactory binder) {
        AuthPrincipal principal = AuthContext.get();
        CurrentUser ann = parameter.getParameterAnnotation(CurrentUser.class);
        if (principal == null) {
            if (ann != null && ann.required()) {
                throw new AuthException("请先登录");
            }
            return null;
        }
        // 允许直接注入 Long userId，省得每个控制器都写 principal.userId()
        return Long.class.isAssignableFrom(parameter.getParameterType())
                ? principal.userId() : principal;
    }
}
