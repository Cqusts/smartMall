package com.smartmall.common.web;

import com.smartmall.common.api.ApiResponse;
import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.exception.BizException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.BindException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.stream.Collectors;

/**
 * 全局异常处理。仅在 Servlet 栈生效——mall-gateway 走 WebFlux，有独立的错误处理链路。
 *
 * <p>核心约定：未预期的异常一律兜底为 {@link ErrorCode#INTERNAL_ERROR}，
 * 堆栈只进日志，绝不出现在响应体里。
 */
@RestControllerAdvice
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    /**
     * 非生产环境下，把未预期异常的类型与消息也放进响应体。
     *
     * <p><b>生产环境必须关掉</b>（{@code ENV=prod}）—— 异常消息里常常带着表名、
     * SQL 片段甚至连接串，那是给攻击者的免费情报。
     *
     * <p>但在开发与演示环境，"系统内部错误"这五个字等于什么都没说：用户看到
     * 下单失败，要去翻服务端日志才知道是连不上库、还是缺列、还是别的。
     * 实测就卡在这里过一轮 —— 前端只有一句通用文案，只能让人去看另一个终端。
     */
    @Value("${ENV:dev}")
    private String env;

    private boolean exposeDetail() {
        return !"prod".equalsIgnoreCase(env);
    }

    private static String trim(String s) {
        String one = s.replaceAll("\\s+", " ").trim();
        return one.length() > 300 ? one.substring(0, 300) + "…" : one;
    }

    @ExceptionHandler(BizException.class)
    public ResponseEntity<ApiResponse<Void>> handleBiz(BizException ex) {
        log.warn("业务异常 code={} msg={}", ex.getErrorCode().getCode(), ex.getMessage());
        return ResponseEntity.status(HttpStatus.OK)
                .body(ApiResponse.fail(ex.getErrorCode(), ex.getMessage()));
    }

    @ExceptionHandler({MethodArgumentNotValidException.class, BindException.class})
    public ResponseEntity<ApiResponse<Void>> handleValidation(BindException ex) {
        String detail = ex.getBindingResult().getFieldErrors().stream()
                .map(e -> e.getField() + ": " + e.getDefaultMessage())
                .collect(Collectors.joining("; "));
        log.warn("参数校验失败 {}", detail);
        return ResponseEntity.ok(ApiResponse.fail(ErrorCode.BAD_REQUEST, detail));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<ApiResponse<Void>> handleIllegalArgument(IllegalArgumentException ex) {
        log.warn("非法参数 {}", ex.getMessage());
        return ResponseEntity.ok(ApiResponse.fail(ErrorCode.BAD_REQUEST, ex.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiResponse<Void>> handleUnexpected(Exception ex) {
        // 堆栈始终只进日志，任何环境都不外泄
        log.error("未预期异常", ex);

        String message = ErrorCode.INTERNAL_ERROR.getMessage();
        if (exposeDetail()) {
            // 只带类型与消息，不带堆栈。根因常常在 cause 上
            // （Spring 会把 SQLException 包成 DataAccessException），
            // 只报最外层往往看不出是数据库的问题
            Throwable root = ex;
            while (root.getCause() != null && root.getCause() != root) {
                root = root.getCause();
            }
            message = message + "：" + root.getClass().getSimpleName()
                    + (root.getMessage() == null ? "" : " - " + trim(root.getMessage()));
        }
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(ApiResponse.fail(ErrorCode.INTERNAL_ERROR.getCode(), message, null));
    }
}
