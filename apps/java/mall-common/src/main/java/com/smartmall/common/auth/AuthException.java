package com.smartmall.common.auth;

import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.exception.BizException;

/**
 * 认证失败。统一映射成 1401，不区分「签名不对」「已过期」「格式错」——
 * 区分对调用方没用，对攻击者倒是有用。
 */
public class AuthException extends BizException {

    public AuthException(String message) {
        super(ErrorCode.UNAUTHORIZED, message);
    }
}
