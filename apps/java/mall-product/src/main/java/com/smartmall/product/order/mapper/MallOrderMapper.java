package com.smartmall.product.order.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.smartmall.product.order.entity.MallOrder;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

@Mapper
public interface MallOrderMapper extends BaseMapper<MallOrder> {

    @Select("SELECT * FROM mall_order WHERE request_id = #{requestId}")
    MallOrder findByRequestId(@Param("requestId") String requestId);

    @Select("SELECT * FROM mall_order WHERE order_no = #{orderNo}")
    MallOrder findByOrderNo(@Param("orderNo") String orderNo);

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
     */
    @Update("UPDATE mall_order SET status = 'paid' "
            + "WHERE id = #{id} AND status = 'pending_payment'")
    int markPaid(@Param("id") Long id);
}
