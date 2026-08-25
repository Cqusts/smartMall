package com.smartmall.product.order.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.smartmall.product.order.entity.MallOrder;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.LocalDateTime;
import java.util.List;

@Mapper
public interface MallOrderMapper extends BaseMapper<MallOrder> {

    @Select("SELECT * FROM mall_order WHERE request_id = #{requestId}")
    MallOrder findByRequestId(@Param("requestId") String requestId);

    @Select("SELECT * FROM mall_order WHERE order_no = #{orderNo}")
    MallOrder findByOrderNo(@Param("orderNo") String orderNo);

    /**
     * 商家侧订单列表。
     *
     * <p>{@code status} 为空时返回全部；给了就按状态筛——商家最常用的动作是
     * "把待发货的都发了"，那需要 {@code status=paid} 这一档。
     *
     * <p>不做分页只给 limit：这是演示规模的取舍。真实店铺得改成键集分页，
     * OFFSET 翻到第 1000 页时数据库仍要扫过前面所有行。
     */
    @Select("<script>SELECT * FROM mall_order"
            + "<if test='status != null and status != \"\"'> WHERE status = #{status}</if>"
            + " ORDER BY id DESC LIMIT #{limit}</script>")
    List<MallOrder> listForMerchant(@Param("status") String status,
                                    @Param("limit") int limit);

    /**
     * 置为已取消。
     *
     * <p>{@code AND status = 'pending_payment'} 决定了这个方法**至多成功一次**。
     * 两个并发的取消请求里只有一个拿到 1，另一个拿到 0——而只有拿到 1 的那个
     * 才会去回补库存。没有这个条件，取消两次就凭空多出一件库存。
     *
     * @return 1 = 本次调用完成了状态流转（调用方负责回补库存）；0 = 状态已不是待支付
     */
    @Update("UPDATE mall_order SET status = 'cancelled', cancelled_at = NOW() "
            + "WHERE id = #{id} AND status = 'pending_payment'")
    int markCancelled(@Param("id") Long id);

    /**
     * 支付。同样用条件更新保证只成功一次，避免重复的支付回调把订单推过头。
     *
     * <p>它与 {@link #markCancelled} 抢的是同一个前置状态，这一点是刻意的：
     * 用户点支付与超时任务判超时可能同时发生，两条 UPDATE 争同一行，
     * 数据库替我们裁决，赢家唯一。
     */
    @Update("UPDATE mall_order SET status = 'paid' "
            + "WHERE id = #{id} AND status = 'pending_payment'")
    int markPaid(@Param("id") Long id);

    // ---------------------------------------------------------------- 履约
    //
    // 全部是条件更新，前置状态写死在 WHERE 里。**状态机不靠 Java 侧的 if 守，
    // 靠数据库守**：并发下 if 判断与随后的 UPDATE 之间有窗口，两个请求可以
    // 同时读到 paid 都认为自己能发货，于是发两次、写两个运单号。

    @Update("UPDATE mall_order SET status = 'shipped', shipped_at = NOW(), "
            + "express_company = #{company}, express_no = #{expressNo}, tracks = #{tracks} "
            + "WHERE id = #{id} AND status = 'paid'")
    int markShipped(@Param("id") Long id,
                    @Param("company") String company,
                    @Param("expressNo") String expressNo,
                    @Param("tracks") String tracks);

    @Update("UPDATE mall_order SET status = 'delivered', delivered_at = NOW(), "
            + "tracks = #{tracks} WHERE id = #{id} AND status = 'shipped'")
    int markDelivered(@Param("id") Long id, @Param("tracks") String tracks);

    /**
     * 确认收货。允许从 shipped 直接跳到 completed——用户拿到货就点了确认，
     * 而物流的"已签收"回调可能还没到。要求必须先 delivered 会让按钮在
     * 用户手上明明拿到货的时候点不动。
     */
    @Update("UPDATE mall_order SET status = 'completed', completed_at = NOW() "
            + "WHERE id = #{id} AND status IN ('shipped', 'delivered')")
    int markCompleted(@Param("id") Long id);

    // ---------------------------------------------------------------- 退款

    /**
     * 申请退款：挂起等审核，并记下申请前的状态。
     *
     * <p><b>{@code status_before_refund = status} 必须写在 {@code status = 'refunding'}
     * 之前，顺序不能反。</b>MySQL 的 SET 子句从左到右求值，<b>后面的赋值看得见
     * 前面刚写进去的新值</b>——写反的话 status_before_refund 存的就是
     * 'refunding' 自己，驳回时把状态"还原"成 refunding，订单永远卡在审核中。
     *
     * <p>这个坑单元测试抓不到：H2 用的是标准 SQL 语义（整行读旧值），
     * 两种写法在 H2 上都对，只有真 MySQL 会炸。实际就是这么发现的——
     * H2 全绿，真机一跑驳回没反应。所以有
     * {@code deploy/scripts/verify-orders.py lifecycle} 对真库跑一遍状态机。
     */
    @Update("UPDATE mall_order SET status_before_refund = status, "
            + "status = 'refunding', refund_applied_at = NOW(), "
            + "refund_reason = #{reason}, refund_amount = #{amount}, "
            + "refund_reject_reason = '' "
            + "WHERE id = #{id} AND status IN ('paid', 'shipped', 'delivered', 'completed')")
    int markRefunding(@Param("id") Long id,
                      @Param("reason") String reason,
                      @Param("amount") java.math.BigDecimal amount);

    /**
     * 同意退款。<b>这一步之后库存才回补</b>，所以它必须至多成功一次——
     * 与 {@link #markCancelled} 是同一个道理，只是前置状态不同。
     */
    @Update("UPDATE mall_order SET status = 'refunded', refunded_at = NOW() "
            + "WHERE id = #{id} AND status = 'refunding'")
    int markRefunded(@Param("id") Long id);

    /**
     * 驳回退款：回到申请前的状态。
     *
     * <p>{@code status = status_before_refund} 而不是写死某个值——已发货的单
     * 被驳回后必须还是 shipped。写死成 paid 会让"这单发没发货"凭空改变，
     * 而客服正是照着这个字段回答"我的货到哪了"。
     */
    @Update("UPDATE mall_order SET status = status_before_refund, "
            + "refund_reject_reason = #{reason}, refund_applied_at = NULL "
            + "WHERE id = #{id} AND status = 'refunding' AND status_before_refund <> ''")
    int markRefundRejected(@Param("id") Long id, @Param("reason") String reason);

    /**
     * 超期未支付的订单。给超时释放任务用。
     *
     * <p>{@code ORDER BY id} + {@code LIMIT} 是为了让每一轮的工作量有上界。
     * 不限量的话，积压几万单时这个任务会一次性拉进内存、并且长时间持锁。
     */
    @Select("SELECT * FROM mall_order WHERE status = 'pending_payment' "
            + "AND created_at < #{before} ORDER BY id LIMIT #{limit}")
    List<MallOrder> findExpiredPending(@Param("before") LocalDateTime before,
                                       @Param("limit") int limit);
}
