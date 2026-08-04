package com.smartmall.common.exception;

import com.smartmall.common.api.ErrorCode;

/**
 * 业务异常。由 {@code GlobalExceptionHandler} 统一转换为 {@code ApiResponse}。
 *
 * <p>约定：只有可预期的业务失败才抛这个异常；未预期的异常直接抛出，
 * 由全局处理器兜底为 {@link ErrorCode#INTERNAL_ERROR}，避免把内部细节暴露给调用方。
 */
public class BizException extends RuntimeException {

    private final ErrorCode errorCode;

    public BizException(ErrorCode errorCode) {
        super(errorCode.getMessage());
        this.errorCode = errorCode;
    }

    public BizException(ErrorCode errorCode, String message) {
        super(message);
        this.errorCode = errorCode;
    }

    public BizException(ErrorCode errorCode, String message, Throwable cause) {
        super(message, cause);
        this.errorCode = errorCode;
    }

    public ErrorCode getErrorCode() {
        return errorCode;
    }

    public static BizException of(ErrorCode errorCode) {
        return new BizException(errorCode);
    }

    public static BizException of(ErrorCode errorCode, String message) {
        return new BizException(errorCode, message);
    }
}
