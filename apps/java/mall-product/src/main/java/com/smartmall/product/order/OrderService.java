package com.smartmall.product.order;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
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
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;
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

    /** 物流节点的时间格式。与 004 种子数据一致，客服会照着念给用户听。 */
    private static final DateTimeFormatter TRACK_TIME =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");

    private static final ObjectMapper JSON = new ObjectMapper();

    private final MallOrderMapper orderMapper;
    private final SkuMapper skuMapper;
    private final TransactionTemplate tx;

    /**
     * 库存预占时长。**单一事实来源**——出参里的 expiresAt 与超时回收任务
     * 用的是同一个值，两边各配一份迟早对不上：页面说「还有 30 分钟」而
     * 任务 15 分钟就把单收走了。
     */
    private final Duration paymentTtl;

    private final int releaseBatchSize;

    public OrderService(MallOrderMapper orderMapper, SkuMapper skuMapper, TransactionTemplate tx,
                        @Value("${smartmall.order.payment-ttl:PT30M}") Duration paymentTtl,
                        @Value("${smartmall.order.release-expired.batch-size:200}") int releaseBatchSize) {
        this.orderMapper = orderMapper;
        this.skuMapper = skuMapper;
        this.tx = tx;
        this.paymentTtl = paymentTtl;
        this.releaseBatchSize = releaseBatchSize;
    }

    public Duration paymentTtl() {
        return paymentTtl;
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
            return OrderView.of(hit, true, paymentTtl);
        }

        try {
            MallOrder created = tx.execute(status -> doPlace(req));
            return OrderView.of(created, false, paymentTtl);
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
            return OrderView.of(winner, true, paymentTtl);
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

        if (!cancelAndRestore(order, "用户取消")) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL, "订单状态已变更，请刷新后重试");
        }
        return OrderView.of(orderMapper.findByOrderNo(orderNo), false, paymentTtl);
    }

    /**
     * 「置为已取消 + 回补库存」这一对动作的<b>唯一</b>入口。
     *
     * <p>手动取消与超时释放都走这里，不是为了少写几行，而是因为**这两条路
     * 必须共用同一个 at-most-once 判据**。各写一份的话，两边并发时可能都
     * 认为自己该回补，同一笔订单的库存就会被加回去两次——凭空多出货来。
     *
     * <p>判据就是 {@code markCancelled} 里那句 {@code AND status =
     * 'pending_payment'}：谁的 UPDATE 返回 1，谁才有资格回补。返回 0 的
     * 一方什么都不做，因为状态已经被别人推走了（可能是另一个取消，
     * 也可能是用户刚好支付成功）。
     *
     * @return true 表示本次调用完成了取消并回补了库存
     */
    private boolean cancelAndRestore(MallOrder order, String reason) {
        if (orderMapper.markCancelled(order.getId()) == 0) {
            return false;
        }
        skuMapper.restoreStock(order.getSkuNo(), order.getQuantity());
        log.info("订单已取消（{}），库存回补 orderNo={} skuNo={} qty={}",
                reason, order.getOrderNo(), order.getSkuNo(), order.getQuantity());
        return true;
    }

    /**
     * 支付。
     *
     * <p><b>已支付的订单再次调用返回成功而不是报错</b>，这是刻意的：真实支付
     * 渠道的回调会重试（网络抖动、我们这边响应慢），回调收到错误就会继续重试
     * 甚至走对账补偿。对同一笔已完成的支付，「再说一次成功」才是正确答复。
     *
     * <p><b>而已取消的订单必须报错，不能顺手改成 paid。</b>取消时库存已经回补，
     * 若此时还允许置为 paid，就会出现一笔"付了钱但货已经还回库存"的订单——
     * 超卖会从这个口子漏出来。这条路径在超时释放上线后不再是理论问题：
     * 用户在超时那一刻点支付，就正好撞上。
     */
    @Transactional(rollbackFor = Exception.class)
    public OrderView pay(String orderNo, Long userId) {
        MallOrder order = orderMapper.findByOrderNo(orderNo);
        if (order == null || !order.getUserId().equals(userId)) {
            if (order != null) {
                log.warn("越权支付：用户 {} 试图支付属于 {} 的订单 {}",
                        userId, order.getUserId(), orderNo);
            }
            throw new BizException(ErrorCode.ORDER_NOT_FOUND);
        }
        if ("paid".equals(order.getStatus())) {
            return OrderView.of(order, true, paymentTtl);
        }
        if (!"pending_payment".equals(order.getStatus())) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL,
                    "订单当前状态为 " + order.getStatus() + "，不可支付");
        }

        if (orderMapper.markPaid(order.getId()) == 0) {
            // 抢输了。要么是另一个支付回调抢先（幂等，返回成功），
            // 要么是超时任务刚把它取消了（必须如实报错——钱不能收）
            MallOrder now = orderMapper.findByOrderNo(orderNo);
            if (now != null && "paid".equals(now.getStatus())) {
                return OrderView.of(now, true, paymentTtl);
            }
            String state = now == null ? "unknown" : now.getStatus();
            log.warn("支付未生效，订单状态已变为 {} orderNo={}", state, orderNo);
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL,
                    "订单状态已变更为 " + state + "，支付未生效"
                            + ("cancelled".equals(state) ? "（超时已自动取消，请重新下单）" : ""));
        }

        log.info("订单已支付 orderNo={}", orderNo);
        return OrderView.of(orderMapper.findByOrderNo(orderNo), false, paymentTtl);
    }

    /**
     * 释放超时未支付订单占用的库存。
     *
     * <p><b>为什么需要它：</b>下单即扣库存（预占），这样才能在最早的时刻杜绝
     * 超卖。代价是没付钱的单会一直占着货——不回收的话，一批放弃支付的订单
     * 就能把热销 SKU 的库存永久锁死，页面显示无货而实际一件没卖出去。
     *
     * <p><b>关于多实例：这里不需要分布式锁。</b>两个 mall-product 实例的定时
     * 任务会扫到同一批订单，但它们最终都要过 {@code markCancelled} 那句条件
     * UPDATE，同一笔订单只有一个实例能拿到 1。重复扫描浪费的是几次查询，
     * 而正确性由数据库保证——用行锁充当互斥，比引一套分布式锁简单得多。
     *
     * <p>逐单一个小事务，而不是整批一个大事务：一笔出问题不该连累其余，
     * 而且大事务会长时间持有一批行锁，挡住正在下单的人。
     *
     * @param ttl        下单后多久算超时
     * @param batchLimit 单轮最多处理多少笔
     * @return 本次实际释放的订单数
     */
    /** 按配置的超时时长与批量上限释放。定时任务走这个重载，参数不重复配一份。 */
    public int releaseExpired() {
        return releaseExpired(paymentTtl, releaseBatchSize);
    }

    public int releaseExpired(Duration ttl, int batchLimit) {
        LocalDateTime deadline = LocalDateTime.now().minus(ttl);
        List<MallOrder> expired = orderMapper.findExpiredPending(deadline, batchLimit);
        if (expired.isEmpty()) {
            return 0;
        }

        int released = 0;
        for (MallOrder order : expired) {
            Boolean done = tx.execute(s -> cancelAndRestore(order, "超时未支付"));
            if (Boolean.TRUE.equals(done)) {
                released++;
            }
        }
        log.info("超时释放：扫到 {} 笔超期未支付，实际释放 {} 笔（其余已被支付或取消）",
                expired.size(), released);
        return released;
    }

    // ================================================================ 履约
    //
    // 发货与送达是**商家/物流侧**动作，没有 userId 可校验。
    // ⚠️ 目前它们也没有任何鉴权 —— 项目还没有认证体系，谁都能调。
    // 控制器把这几个口子放在 /api/product/admin/orders 下面，就是为了
    // 将来接入认证时能整个前缀一起拦，而不用一个个找。

    /** 发货。paid → shipped，写运单号与第一条物流轨迹。 */
    @Transactional(rollbackFor = Exception.class)
    public OrderView ship(String orderNo, String company, String expressNo) {
        MallOrder order = mustFind(orderNo);
        if (!"paid".equals(order.getStatus())) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL,
                    "订单当前状态为 " + order.getStatus() + "，不可发货");
        }
        String tracks = appendTrack(order.getTracks(), "商品已出库，" + company + "揽收");
        if (orderMapper.markShipped(order.getId(), company, expressNo, tracks) == 0) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL, "订单状态已变更，发货未生效");
        }
        log.info("订单已发货 orderNo={} {} {}", orderNo, company, expressNo);
        return view(orderNo);
    }

    /** 物流送达。shipped → delivered。真实系统由快递回调驱动。 */
    @Transactional(rollbackFor = Exception.class)
    public OrderView deliver(String orderNo) {
        MallOrder order = mustFind(orderNo);
        if (!"shipped".equals(order.getStatus())) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL,
                    "订单当前状态为 " + order.getStatus() + "，不可标记送达");
        }
        String tracks = appendTrack(order.getTracks(), "快件已送达，请注意查收");
        if (orderMapper.markDelivered(order.getId(), tracks) == 0) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL, "订单状态已变更");
        }
        return view(orderNo);
    }

    /**
     * 确认收货。用户动作，shipped / delivered → completed。
     *
     * <p>允许从 shipped 直接确认：用户拿到货就点了，而物流的"已签收"回调
     * 可能还没到。要求必须先 delivered，会让按钮在用户明明拿到货的时候点不动。
     */
    @Transactional(rollbackFor = Exception.class)
    public OrderView confirmReceipt(String orderNo, Long userId) {
        MallOrder order = mustOwn(orderNo, userId, "确认收货");
        if (!"shipped".equals(order.getStatus()) && !"delivered".equals(order.getStatus())) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL,
                    "订单当前状态为 " + order.getStatus() + "，不可确认收货");
        }
        if (orderMapper.markCompleted(order.getId()) == 0) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL, "订单状态已变更，请刷新后重试");
        }
        return view(orderNo);
    }

    // ================================================================ 退款

    /**
     * 申请退款。用户动作，把订单挂到 refunding 等审核。
     *
     * <p><b>申请不等于退款。</b>钱出去是不可逆的，所以这里只改状态，
     * 不动库存、不放款——那是 {@link #approveRefund} 的事，而它需要人点头。
     * 这与工具层全只读是同一条原则：不可逆的动作不能被自动触发。
     */
    @Transactional(rollbackFor = Exception.class)
    public OrderView applyRefund(String orderNo, Long userId, String reason) {
        MallOrder order = mustOwn(orderNo, userId, "申请退款");
        if ("refunding".equals(order.getStatus())) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL, "该订单已在退款审核中");
        }
        if (!REFUNDABLE.contains(order.getStatus())) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL,
                    "订单当前状态为 " + order.getStatus() + "，不可申请退款"
                            + ("pending_payment".equals(order.getStatus())
                               ? "（尚未支付，直接取消即可）" : ""));
        }
        // 金额由服务端取订单实付，不接受客户端传——理由与下单不接受 price 相同
        if (orderMapper.markRefunding(order.getId(), reason, order.getAmount()) == 0) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL, "订单状态已变更，请刷新后重试");
        }
        log.info("退款申请 orderNo={} amount={} reason={}", orderNo, order.getAmount(), reason);
        return view(orderNo);
    }

    /**
     * 同意退款。<b>这一步才回补库存。</b>
     *
     * <p>它是第三条会回补库存的路径（另外两条是手动取消与超时回收），
     * 而"库存至多回补一次"这条不变式必须跨三条路径都成立。这里靠的仍然是
     * 条件更新：{@code markRefunded} 要求前置状态是 refunding，而 refunding
     * 只能从已支付系的状态进入——与 cancelled 那条路的前置 pending_payment
     * 互斥。已取消的单付不了款（有测试），也就永远进不了退款流程。
     *
     * <p><b>一个说清楚的简化：</b>已发货的单同意退款就立刻回补库存，
     * 而现实里货还在路上，得等退货入库才算。真实系统会把"退款完成"与
     * "退货入库"拆成两步，库存跟着后者走。这里合并了，因为演示没有仓储环节。
     */
    @Transactional(rollbackFor = Exception.class)
    public OrderView approveRefund(String orderNo) {
        MallOrder order = mustFind(orderNo);
        if ("refunded".equals(order.getStatus())) {
            return OrderView.of(order, true, paymentTtl);   // 幂等：审核动作会被重复点
        }
        if (!"refunding".equals(order.getStatus())) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL,
                    "订单当前状态为 " + order.getStatus() + "，没有待审的退款申请");
        }
        if (orderMapper.markRefunded(order.getId()) == 0) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL, "订单状态已变更，退款未生效");
        }
        skuMapper.restoreStock(order.getSkuNo(), order.getQuantity());
        log.info("退款已同意，库存回补 orderNo={} skuNo={} qty={} amount={}",
                orderNo, order.getSkuNo(), order.getQuantity(), order.getAmount());
        return view(orderNo);
    }

    /** 驳回退款：回到申请前的状态，不动库存也不动钱。 */
    @Transactional(rollbackFor = Exception.class)
    public OrderView rejectRefund(String orderNo, String reason) {
        MallOrder order = mustFind(orderNo);
        if (!"refunding".equals(order.getStatus())) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL,
                    "订单当前状态为 " + order.getStatus() + "，没有待审的退款申请");
        }
        if (orderMapper.markRefundRejected(order.getId(), reason) == 0) {
            throw new BizException(ErrorCode.ORDER_STATE_ILLEGAL, "订单状态已变更，驳回未生效");
        }
        log.info("退款已驳回 orderNo={} 回到 {} reason={}",
                orderNo, order.getStatusBeforeRefund(), reason);
        return view(orderNo);
    }

    // ---------------------------------------------------------------- 内部

    /** 可申请退款的状态。未支付不在其中——没付钱谈不上退，直接取消即可。 */
    private static final java.util.Set<String> REFUNDABLE =
            java.util.Set.of("paid", "shipped", "delivered", "completed");

    private MallOrder mustFind(String orderNo) {
        MallOrder order = orderMapper.findByOrderNo(orderNo);
        if (order == null) {
            throw new BizException(ErrorCode.ORDER_NOT_FOUND);
        }
        return order;
    }

    /** 查单并校验归属。越权与不存在返回完全相同的错误，不给存在性预言机。 */
    private MallOrder mustOwn(String orderNo, Long userId, String action) {
        MallOrder order = orderMapper.findByOrderNo(orderNo);
        if (order == null || !order.getUserId().equals(userId)) {
            if (order != null) {
                log.warn("越权{}：用户 {} 试图操作属于 {} 的订单 {}",
                        action, userId, order.getUserId(), orderNo);
            }
            throw new BizException(ErrorCode.ORDER_NOT_FOUND);
        }
        return order;
    }

    private OrderView view(String orderNo) {
        return OrderView.of(orderMapper.findByOrderNo(orderNo), false, paymentTtl);
    }

    /**
     * 往物流轨迹里追加一条。
     *
     * <p>形状与 004 种子数据一致：{@code [{"ts":"YYYY-MM-DD HH:mm","desc":"..."}]}，
     * 因为客服工具层 {@code get_order_status} 直接把它读出来讲给用户听。
     * 这里改了形状，客服那边就会说出一句语法正确但没有信息的话。
     */
    private String appendTrack(String existingJson, String desc) {
        try {
            List<java.util.Map<String, String>> nodes = new java.util.ArrayList<>();
            if (existingJson != null && !existingJson.isBlank()) {
                nodes.addAll(JSON.readValue(existingJson, new TypeReference<>() {
                }));
            }
            nodes.add(java.util.Map.of(
                    "ts", LocalDateTime.now().format(TRACK_TIME),
                    "desc", desc));
            return JSON.writeValueAsString(nodes);
        } catch (JsonProcessingException e) {
            // 轨迹读不出来不该让发货失败——发货是主线，轨迹是附带信息。
            // 重建一条而不是抛异常，同时留日志说明原来那份坏了
            log.warn("物流轨迹解析失败，重建：{}", e.getMessage());
            try {
                return JSON.writeValueAsString(List.of(java.util.Map.of(
                        "ts", LocalDateTime.now().format(TRACK_TIME), "desc", desc)));
            } catch (JsonProcessingException fatal) {
                throw new IllegalStateException(fatal);
            }
        }
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
        return OrderView.of(order, false, paymentTtl);
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
