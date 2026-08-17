package com.smartmall.product.order.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 订单。
 *
 * <p><b>没有 deleted 字段是刻意的。</b>全局 MyBatis-Plus 配置开了逻辑删除
 * （{@code logic-delete-field: deleted}），实体上有这个字段就会被自动加
 * {@code deleted = 0} 条件。订单不该被逻辑删除——取消是状态流转，不是删除，
 * 而且被删掉的订单意味着客服查不到、对账对不上。
 *
 * <p>{@code spec} 是下单那一刻 SKU 规格的**快照**，不是外键。SKU 后续改名或
 * 下架，历史订单上仍然是用户当时买的那个规格。
 */
@TableName("mall_order")
public class MallOrder {

    @TableId(type = IdType.AUTO)
    private Long id;

    private String orderNo;

    /** 幂等键。唯一索引，见迁移 007。 */
    private String requestId;

    private Long userId;
    private Long productId;
    private String skuNo;
    private String spec;
    private Integer quantity;
    private BigDecimal amount;

    /** pending_payment / paid / shipped / delivered / completed / cancelled / refunding */
    private String status;

    private String expressCompany;
    private String expressNo;

    /** 物流节点 JSON，{@code [{ts, desc}]}。下单时为空，发货后由履约链路写入。 */
    private String tracks;

    private LocalDateTime createdAt;
    private LocalDateTime updatedAt;
    private LocalDateTime shippedAt;
    private LocalDateTime deliveredAt;
    private LocalDateTime completedAt;

    /** 非空即表示库存已回补。与状态机一起构成「只回补一次」的凭据。 */
    private LocalDateTime cancelledAt;

    private LocalDateTime refundAppliedAt;

    /** 非空即表示钱已退、库存已回补。与 cancelledAt 一起是对账时的可查证据。 */
    private LocalDateTime refundedAt;

    private String refundReason;
    private String refundRejectReason;
    private BigDecimal refundAmount;

    /**
     * 申请退款前的状态。驳回时据此还原。
     *
     * <p>不记的话，已发货的单被驳回后只能猜一个状态，而"这单发没发货"是客服
     * 照着回答"我的货到哪了"的依据——猜错就是对用户说了假话。
     */
    private String statusBeforeRefund;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getOrderNo() {
        return orderNo;
    }

    public void setOrderNo(String orderNo) {
        this.orderNo = orderNo;
    }

    public String getRequestId() {
        return requestId;
    }

    public void setRequestId(String requestId) {
        this.requestId = requestId;
    }

    public Long getUserId() {
        return userId;
    }

    public void setUserId(Long userId) {
        this.userId = userId;
    }

    public Long getProductId() {
        return productId;
    }

    public void setProductId(Long productId) {
        this.productId = productId;
    }

    public String getSkuNo() {
        return skuNo;
    }

    public void setSkuNo(String skuNo) {
        this.skuNo = skuNo;
    }

    public String getSpec() {
        return spec;
    }

    public void setSpec(String spec) {
        this.spec = spec;
    }

    public Integer getQuantity() {
        return quantity;
    }

    public void setQuantity(Integer quantity) {
        this.quantity = quantity;
    }

    public BigDecimal getAmount() {
        return amount;
    }

    public void setAmount(BigDecimal amount) {
        this.amount = amount;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public String getExpressCompany() {
        return expressCompany;
    }

    public void setExpressCompany(String expressCompany) {
        this.expressCompany = expressCompany;
    }

    public String getExpressNo() {
        return expressNo;
    }

    public void setExpressNo(String expressNo) {
        this.expressNo = expressNo;
    }

    public String getTracks() {
        return tracks;
    }

    public void setTracks(String tracks) {
        this.tracks = tracks;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(LocalDateTime createdAt) {
        this.createdAt = createdAt;
    }

    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }

    public void setUpdatedAt(LocalDateTime updatedAt) {
        this.updatedAt = updatedAt;
    }

    public LocalDateTime getShippedAt() {
        return shippedAt;
    }

    public void setShippedAt(LocalDateTime shippedAt) {
        this.shippedAt = shippedAt;
    }

    public LocalDateTime getCancelledAt() {
        return cancelledAt;
    }

    public void setCancelledAt(LocalDateTime cancelledAt) {
        this.cancelledAt = cancelledAt;
    }

    public LocalDateTime getDeliveredAt() {
        return deliveredAt;
    }

    public void setDeliveredAt(LocalDateTime deliveredAt) {
        this.deliveredAt = deliveredAt;
    }

    public LocalDateTime getCompletedAt() {
        return completedAt;
    }

    public void setCompletedAt(LocalDateTime completedAt) {
        this.completedAt = completedAt;
    }

    public LocalDateTime getRefundAppliedAt() {
        return refundAppliedAt;
    }

    public void setRefundAppliedAt(LocalDateTime refundAppliedAt) {
        this.refundAppliedAt = refundAppliedAt;
    }

    public LocalDateTime getRefundedAt() {
        return refundedAt;
    }

    public void setRefundedAt(LocalDateTime refundedAt) {
        this.refundedAt = refundedAt;
    }

    public String getRefundReason() {
        return refundReason;
    }

    public void setRefundReason(String refundReason) {
        this.refundReason = refundReason;
    }

    public String getRefundRejectReason() {
        return refundRejectReason;
    }

    public void setRefundRejectReason(String refundRejectReason) {
        this.refundRejectReason = refundRejectReason;
    }

    public BigDecimal getRefundAmount() {
        return refundAmount;
    }

    public void setRefundAmount(BigDecimal refundAmount) {
        this.refundAmount = refundAmount;
    }

    public String getStatusBeforeRefund() {
        return statusBeforeRefund;
    }

    public void setStatusBeforeRefund(String statusBeforeRefund) {
        this.statusBeforeRefund = statusBeforeRefund;
    }
}
