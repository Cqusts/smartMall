package com.smartmall.product.order.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.smartmall.product.order.entity.Sku;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

@Mapper
public interface SkuMapper extends BaseMapper<Sku> {

    @Select("SELECT * FROM sku WHERE sku_no = #{skuNo} AND deleted = 0")
    Sku findBySkuNo(@Param("skuNo") String skuNo);

    /**
     * 扣库存。<b>整个下单链路的防超卖就是这一条 SQL。</b>
     *
     * <p>关键在 {@code AND stock >= #{quantity}} 与减法写在**同一条 UPDATE 里**。
     * InnoDB 执行 UPDATE 时对命中行加排他锁，谓词是在持锁状态下求值的，
     * 所以并发请求会被串行化：50 个人抢 5 件，恰好 5 条返回 1，其余返回 0。
     *
     * <p>反面写法是先 {@code SELECT stock} 判断够不够、再 {@code UPDATE} 扣减。
     * 两句之间没有锁，两个请求可以同时读到 stock=1 都认为够，然后各扣一次，
     * 库存变成 -1。**这是超卖最常见的成因**，而且在低并发的手工测试里
     * 永远复现不出来——所以本仓库有一个 50 线程的并发测试盯着它
     * （{@code OrderConcurrencyTest}）。
     *
     * <p>{@code status = 'on_sale'} 一并放进条件：下架商品不能被下单，
     * 而"检查状态"和"扣库存"如果分成两步，中间同样有窗口。
     *
     * @return 受影响行数。1 = 扣减成功；0 = SKU 不存在、已下架、或库存不足
     */
    @Update("UPDATE sku SET stock = stock - #{quantity} "
            + "WHERE sku_no = #{skuNo} AND deleted = 0 "
            + "AND status = 'on_sale' AND stock >= #{quantity}")
    int deductStock(@Param("skuNo") String skuNo, @Param("quantity") int quantity);

    /**
     * 回补库存。取消订单时调用。
     *
     * <p>这里**没有**上界条件（比如 {@code stock + qty <= 初始库存}）——初始库存
     * 不是个能查到的量，运营随时会改。防重复回补靠的是调用方那侧：只有把订单
     * 从 pending_payment 条件更新成 cancelled 成功的那一次才会走到这里。
     */
    @Update("UPDATE sku SET stock = stock + #{quantity} "
            + "WHERE sku_no = #{skuNo} AND deleted = 0")
    int restoreStock(@Param("skuNo") String skuNo, @Param("quantity") int quantity);
}
