package com.smartmall.product.order;

import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.exception.BizException;
import com.smartmall.product.order.dto.CreateOrderRequest;
import com.smartmall.product.order.dto.OrderView;
import com.smartmall.product.order.entity.MallOrder;
import com.smartmall.product.order.entity.Sku;
import com.smartmall.product.order.mapper.MallOrderMapper;
import com.smartmall.product.order.mapper.SkuMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.concurrent.ThreadLocalRandom;

/**
 * 下单。
 *
 * <p>三条不变式，每一条都对应代码里一处具体写法：
 *
 * <ol>
 *   <li><b>不超卖</b> —— 条件 UPDATE 扣库存，见 {@link SkuMapper#deductStock}</li>
 *   <li><b>不重单</b> —— request_id 唯一索引 + 双路径回查，见 {@link #place}</li>
 *   <li><b>不漏库存</b> —— 扣库存与建单同事务，任一失败一起回滚</li>
 * </ol>
 *
 * <p><b>为什么订单放在 mall-product 而不是独立的 mall-order：</b>
 * 扣库存和建订单必须原子，而库存归 mall-product 管。拆成两个服务，这个原子性
 * 就得靠 Saga 或 TCC 补偿来维持——那是分布式事务的复杂度，而本项目整个跑在
 * 一个 MySQL 实例上，付出这个代价换不来任何东西。真要拆的时候，接缝是
 * 这个类的公开方法，不是数据库。
 */
@Service
public class OrderService {

    private static final Logger log = LoggerFactory.getLogger(OrderService.class);

    private static final DateTimeFormatter ORDER_NO_TIME =
            DateTimeFormatter.ofPattern("yyyyMMddHHmmss");

    private final MallOrderMapper orderMapper;
    private final SkuMapper skuMapper;
    private final TransactionTemplate tx;

    public OrderService(MallOrderMapper orderMapper, SkuMapper skuMapper, TransactionTemplate tx) {
        this.orderMapper = orderMapper;
        this.skuMapper = skuMapper;
        this.tx = tx;
    }

    /**
     * 下单。
     *
     * <p><b>这里用 TransactionTemplate 而不是给整个方法挂 @Transactional，是必须的。</b>
     * 幂等的慢路径要在事务**回滚之后**才回查——同一个 request_id 并发进来时，
     * 输的那个必须整笔回滚（把它扣掉的库存吐回去），然后才能去读赢的那笔。
     * 如果把 try/catch 写在 @Transactional 方法内部，捕获 DuplicateKeyException
     * 时事务还没结束，扣掉的库存不会回滚，就会凭空少一件货。事务边界必须
     * 严格小于 catch 的范围，而这一点用注解表达不出来。
     */
    public OrderView place(CreateOrderRequest req) {
        // 快路径：用户手抖双击、前端超时重试。绝大多数重复提交在这里就返回了，
        // 不进事务、不碰库存
        MallOrder hit = orderMapper.findByRequestId(req.requestId());
        if (hit != null) {
            log.info("幂等命中（快路径） requestId={} orderNo={}", req.requestId(), hit.getOrderNo());
            return OrderView.of(hit, true);
        }

        try {
            MallOrder created = tx.execute(status -> doPlace(req));
            return OrderView.of(created, false);
        } catch (DuplicateKeyException e) {
            // 慢路径：两个同 request_id 的请求并发，快路径都没命中。
            // 到这里事务已经回滚，扣掉的库存跟着回滚了，回查赢家返回即可。
            MallOrder winner = orderMapper.findByRequestId(req.requestId());
            if (winner == null) {
                // 撞的不是 request_id，是别的唯一键（order_no 碰撞）。
                // 不能当幂等吞掉——那会把一个真实故障伪装成成功
                throw e;
            }
            log.info("幂等命中（慢路径·并发） requestId={} orderNo={}",
                    req.requestId(), winner.getOrderNo());
            return OrderView.of(winner, true);
        }
    }

    /** 事务体：扣库存 → 建单。两步任一失败，整笔回滚。 */
    private MallOrder doPlace(CreateOrderRequest req) {
        int affected = skuMapper.deductStock(req.skuNo(), req.quantity());
        if (affected == 0) {
            // 扣减失败有三种原因，回查一次把它们分开——直接回「下单失败」
            // 用户不知道该改数量、换规格、还是根本就没这个货
            Sku sku = skuMapper.findBySkuNo(req.skuNo());
            if (sku == null) {
                throw new BizException(ErrorCode.SKU_NOT_FOUND, "SKU 不存在：" + req.skuNo());
            }
            if (!"on_sale".equals(sku.getStatus())) {
                throw new BizException(ErrorCode.SKU_OUT_OF_STOCK,
                        "该规格已下架（" + sku.getStatus() + "）");
            }
            throw new BizException(ErrorCode.SKU_OUT_OF_STOCK,
                    "库存不足，仅剩 " + sku.getStock() + " 件");
        }

        Sku sku = skuMapper.findBySkuNo(req.skuNo());
        if (sku == null) {
            // 扣减成功却查不到行，只可能是并发删除。抛异常让事务回滚，
            // 库存跟着回来——不能带着一个查不到 SKU 的订单继续往下走
            throw new BizException(ErrorCode.SKU_NOT_FOUND, "SKU 已失效：" + req.skuNo());
        }

        MallOrder order = new MallOrder();
        order.setOrderNo(nextOrderNo());
        order.setRequestId(req.requestId());
        order.setUserId(req.userId());
        order.setProductId(sku.getProductId());
        order.setSkuNo(sku.getSkuNo());
        order.setSpec(sku.getSpec() == null ? "" : sku.getSpec());
        order.setQuantity(req.quantity());
        // 金额服务端算。BigDecimal 而不是 double——钱不能用二进制浮点
        order.setAmount(sku.getPrice().multiply(BigDecimal.valueOf(req.quantity())));
        order.setStatus("pending_payment");
        order.setExpressCompany("");
        order.setExpressNo("");
        order.setCreatedAt(LocalDateTime.now());

        orderMapper.insert(order);
        log.info("下单成功 orderNo={} skuNo={} qty={} amount={}",
                order.getOrderNo(), order.getSkuNo(), order.getQuantity(), order.getAmount());
        return order;
    }

    /**
     * 取消订单，回补库存。
     *
     * <p>越权处置与客服工具层保持一致：不属于当前用户的订单，返回的错误与
     * "订单不存在"**完全相同**。区分开会泄露订单是否存在，攻击者可以靠枚举
     * 单号确认哪些是真的。
     */
    @Transactional(rollbackFor = Exception.class)
    public OrderView cancel(String orderNo, Long userId) {
        MallOrder order = orderMapper.findByOrderNo(orderNo);
        if (order == null || !order.getUserId().equals(userId)) {
            if (order != null) {
                log.warn("越权取消：用户 {} 试图取消属于 {} 的订单 {}",
                        userId, order.getUserId(), orderNo);
            }
            throw new BizException(ErrorCode.ORDER_NOT_FOUND);
        }
        if (!"pending_payment".equals(order.getStatus())) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL,
                    "订单当前状态为 " + order.getStatus() + "，不可取消");
        }

        // 条件更新是这里的关键：并发取消只有一个能拿到 1，也就只回补一次库存
        if (orderMapper.markCancelled(order.getId()) == 0) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL, "订单状态已变更，请刷新后重试");
        }
        skuMapper.restoreStock(order.getSkuNo(), order.getQuantity());
        log.info("订单已取消，库存回补 orderNo={} skuNo={} qty={}",
                orderNo, order.getSkuNo(), order.getQuantity());

        return OrderView.of(orderMapper.findByOrderNo(orderNo), false);
    }

    /** 查单。同样的越权口径：不属于你的单，就是"不存在"。 */
    public OrderView get(String orderNo, Long userId) {
        MallOrder order = orderMapper.findByOrderNo(orderNo);
        if (order == null || !order.getUserId().equals(userId)) {
            if (order != null) {
                log.warn("越权查询：用户 {} 试图查询属于 {} 的订单 {}",
                        userId, order.getUserId(), orderNo);
            }
            throw new BizException(ErrorCode.ORDER_NOT_FOUND);
        }
        return OrderView.of(order, false);
    }

    /**
     * 订单号：时间前缀 + 6 位随机。
     *
     * <p>时间前缀让人肉排查时一眼能看出下单时刻；随机后缀避免同秒碰撞。
     * 真撞了也不会写坏数据——{@code uk_order_no} 会拦住，调用方收到
     * DuplicateKeyException 且 request_id 回查为空，走重新抛出的分支。
     */
    private String nextOrderNo() {
        return LocalDateTime.now().format(ORDER_NO_TIME)
                + String.format("%06d", ThreadLocalRandom.current().nextInt(1_000_000));
    }
}
