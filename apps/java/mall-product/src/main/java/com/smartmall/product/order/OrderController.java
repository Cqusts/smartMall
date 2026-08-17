package com.smartmall.product.order;

import com.smartmall.common.api.ApiResponse;
import com.smartmall.product.order.dto.CreateOrderRequest;
import com.smartmall.product.order.dto.OrderView;
import com.smartmall.product.order.dto.RefundRequest;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 下单接口。
 *
 * <p>路径带 {@code /api/product} 前缀是因为网关那条路由**不做 StripPrefix**
 * （见 mall-gateway 的 application.yml）——Java 侧服务自己承担完整路径，
 * 所以直连 8081 和走网关 8080 是同一个 URL，调试时不用换。
 */
@RestController
@RequestMapping("/api/product/orders")
@Tag(name = "订单", description = "下单 · 取消 · 查询")
public class OrderController {

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @PostMapping
    @Operation(summary = "下单", description = "扣库存与建单同事务；requestId 相同的重复提交只产生一笔")
    public ApiResponse<OrderView> create(@Valid @RequestBody CreateOrderRequest req) {
        return ApiResponse.ok(orderService.place(req));
    }

    // 绑定名一律显式写出。父 POM 已经开了编译器的 -parameters，靠推断本也能work，
    // 但那意味着一个构建参数没了、接口就在运行时炸——显式写死两个字符的成本，
    // 换掉对编译选项的隐式依赖
    @PostMapping("/{orderNo}/pay")
    @Operation(summary = "支付", description = "重复支付幂等返回成功；已取消的订单报错，不会改成已支付")
    public ApiResponse<OrderView> pay(@PathVariable("orderNo") String orderNo,
                                      @RequestParam("userId") Long userId) {
        return ApiResponse.ok(orderService.pay(orderNo, userId));
    }

    @PostMapping("/{orderNo}/cancel")
    @Operation(summary = "取消订单", description = "回补库存。仅待支付可取消，且至多回补一次")
    public ApiResponse<OrderView> cancel(@PathVariable("orderNo") String orderNo,
                                         @RequestParam("userId") Long userId) {
        return ApiResponse.ok(orderService.cancel(orderNo, userId));
    }

    @PostMapping("/{orderNo}/confirm")
    @Operation(summary = "确认收货", description = "shipped/delivered → completed")
    public ApiResponse<OrderView> confirm(@PathVariable("orderNo") String orderNo,
                                          @RequestParam("userId") Long userId) {
        return ApiResponse.ok(orderService.confirmReceipt(orderNo, userId));
    }

    @PostMapping("/{orderNo}/refund")
    @Operation(summary = "申请退款",
            description = "只挂起等审核，不动钱也不动库存——放款需要人点头")
    public ApiResponse<OrderView> applyRefund(@PathVariable("orderNo") String orderNo,
                                              @RequestParam("userId") Long userId,
                                              @Valid @RequestBody RefundRequest req) {
        return ApiResponse.ok(orderService.applyRefund(orderNo, userId, req.reason()));
    }

    @GetMapping("/{orderNo}")
    @Operation(summary = "查询订单", description = "不属于该用户的订单返回「订单不存在」，不泄露存在性")
    public ApiResponse<OrderView> get(@PathVariable("orderNo") String orderNo,
                                      @RequestParam("userId") Long userId) {
        return ApiResponse.ok(orderService.get(orderNo, userId));
    }
}
