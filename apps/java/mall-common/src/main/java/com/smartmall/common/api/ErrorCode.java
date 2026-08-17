package com.smartmall.common.api;

/**
 * 全局错误码。
 *
 * <p>分段规则：
 * <ul>
 *   <li>0        成功</li>
 *   <li>1xxx     通用错误（参数、鉴权、限流）</li>
 *   <li>2xxx     商品域</li>
 *   <li>3xxx     素材域</li>
 *   <li>4xxx     数据中台域</li>
 *   <li>5xxx     考核域</li>
 *   <li>9xxx     系统与下游依赖</li>
 * </ul>
 */
public enum ErrorCode {

    OK(0, "success"),

    BAD_REQUEST(1400, "请求参数不合法"),
    UNAUTHORIZED(1401, "未认证"),
    FORBIDDEN(1403, "无权限"),
    NOT_FOUND(1404, "资源不存在"),
    RATE_LIMITED(1429, "请求过于频繁"),

    PRODUCT_NOT_FOUND(2404, "商品不存在"),
    SKU_NOT_FOUND(2405, "SKU 不存在"),
    ORDER_NOT_FOUND(2406, "订单不存在"),
    // 库存不足与「SKU 不存在」必须是两个码：前者用户改个数量就能下单，
    // 后者改数量没用。合并成一个码，前端就只能给一句含糊的「下单失败」
    SKU_OUT_OF_STOCK(2409, "库存不足"),
    ORDER_STATE_ILLEGAL(2410, "订单状态流转不合法"),

    ASSET_NOT_FOUND(3404, "素材不存在"),
    ASSET_STATE_ILLEGAL(3409, "素材状态流转不合法"),
    ASSET_AI_LABEL_MISSING(3422, "AI 生成素材缺少标识，不允许流转"),

    DATASET_NOT_FOUND(4404, "数据集版本不存在"),
    DATASET_GATE_REJECTED(4422, "数据集质量门禁未通过"),

    KPI_SCORE_NOT_FOUND(5404, "考核评分不存在"),
    KPI_APPEAL_EXPIRED(5410, "申诉已超过有效期"),

    INTERNAL_ERROR(9500, "系统内部错误"),
    DEPENDENCY_UNAVAILABLE(9503, "下游依赖不可用"),
    DEPENDENCY_TIMEOUT(9504, "下游依赖超时");

    private final int code;
    private final String message;

    ErrorCode(int code, String message) {
        this.code = code;
        this.message = message;
    }

    public int getCode() {
        return code;
    }

    public String getMessage() {
        return message;
    }
}
