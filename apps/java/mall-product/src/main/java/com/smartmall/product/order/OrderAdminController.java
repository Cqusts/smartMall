package com.smartmall.product.order;

import com.smartmall.common.api.ApiResponse;
import com.smartmall.product.order.dto.OrderView;
import com.smartmall.product.order.dto.RefundRequest;
import com.smartmall.product.order.dto.ShipRequest;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 商家侧订单操作：发货、标记送达、退款审核。
 *
 * <p><b>⚠️ 这些接口目前没有任何鉴权，谁都能调。</b>项目还没有认证体系
 * （M0–M7 路线图里没排这一项），所以「同意退款」这种直接放款的动作现在
 * 是裸奔的。这里没有假装安全，而是把它写出来。
 *
 * <p><b>单独开一个 {@code /admin} 前缀就是为了这件事。</b>商家动作与用户
 * 动作混在同一个控制器里，将来接入认证时得一个方法一个方法地判断该不该拦，
 * 漏一个就是一个洞；分开之后，一条路径前缀规则就能把整片挡住。
 *
 * <p>用户侧动作（下单、支付、取消、确认收货、申请退款）在
 * {@link OrderController}，那些靠 userId 校验归属。
 */
@RestController
@RequestMapping("/api/product/admin/orders")
@Tag(name = "订单·商家", description = "发货 · 送达 · 退款审核（⚠️ 暂无鉴权）")
public class OrderAdminController {

    private final OrderService orderService;

    public OrderAdminController(OrderService orderService) {
        this.orderService = orderService;
    }

    @PostMapping("/{orderNo}/ship")
    @Operation(summary = "发货", description = "paid → shipped，写运单号与第一条物流轨迹")
    public ApiResponse<OrderView> ship(@PathVariable("orderNo") String orderNo,
                                       @Valid @RequestBody ShipRequest req) {
        return ApiResponse.ok(orderService.ship(orderNo, req.company(), req.expressNo()));
    }

    @PostMapping("/{orderNo}/deliver")
    @Operation(summary = "标记送达", description = "shipped → delivered。真实系统由快递回调驱动")
    public ApiResponse<OrderView> deliver(@PathVariable("orderNo") String orderNo) {
        return ApiResponse.ok(orderService.deliver(orderNo));
    }

    @PostMapping("/{orderNo}/refund/approve")
    @Operation(summary = "同意退款", description = "这一步才回补库存。重复点击幂等")
    public ApiResponse<OrderView> approveRefund(@PathVariable("orderNo") String orderNo) {
        return ApiResponse.ok(orderService.approveRefund(orderNo));
    }

    @PostMapping("/{orderNo}/refund/reject")
    @Operation(summary = "驳回退款", description = "回到申请前的状态，不动库存也不动钱")
    public ApiResponse<OrderView> rejectRefund(@PathVariable("orderNo") String orderNo,
                                               @Valid @RequestBody RefundRequest req) {
        return ApiResponse.ok(orderService.rejectRefund(orderNo, req.reason()));
    }
}
