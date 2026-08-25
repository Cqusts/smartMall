package com.smartmall.product.catalog;

import com.smartmall.common.api.ApiResponse;
import com.smartmall.product.auth.web.RequireMerchant;
import com.smartmall.product.catalog.dto.*;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;

import java.util.List;

/**
 * 商家侧的商品维护。
 *
 * <p>整个类要求 merchant 角色。买家令牌调这里会拿到 1403，不带令牌 1401——
 * 由 {@link RequireMerchant} 与拦截器判定，不在这个类里逐个方法写。
 *
 * <p>路径放在 {@code /admin} 下与订单商家接口一致：一条路径前缀规则就能在
 * 网关上把整片挡住，不必逐个方法判断。
 */
@RequireMerchant
@RestController
@RequestMapping("/api/product/admin/products")
@Tag(name = "商品·商家", description = "上架 · 改价改库存 · 上下架（仅 merchant 角色）")
public class ProductAdminController {

    private final ProductAdminService service;

    public ProductAdminController(ProductAdminService service) {
        this.service = service;
    }

    @Operation(summary = "商品列表（含草稿与已下架）")
    @GetMapping
    public ApiResponse<List<ProductAdminView>> list(
            @RequestParam(value = "limit", defaultValue = "50") int limit) {
        return ApiResponse.ok(service.list(limit));
    }

    @Operation(summary = "商品详情，带上架自检结果")
    @GetMapping("/{id}")
    public ApiResponse<ProductAdminView> get(@PathVariable("id") Long id) {
        return ApiResponse.ok(service.view(id));
    }

    @Operation(summary = "新建商品", description = "建出来是 draft，需要单独上架")
    @PostMapping
    public ApiResponse<ProductAdminView> create(
            @Valid @RequestBody CreateProductRequest req) {
        return ApiResponse.ok(service.create(req));
    }

    @Operation(summary = "改基本信息与属性")
    @PutMapping("/{id}")
    public ApiResponse<ProductAdminView> update(
            @PathVariable("id") Long id,
            @Valid @RequestBody UpdateProductRequest req) {
        return ApiResponse.ok(service.update(id, req));
    }

    @Operation(summary = "新增或修改 SKU（改价改库存走这里）")
    @PutMapping("/{id}/skus")
    public ApiResponse<ProductAdminView> upsertSku(
            @PathVariable("id") Long id, @Valid @RequestBody SkuSpec spec) {
        return ApiResponse.ok(service.upsertSku(id, spec));
    }

    @Operation(summary = "上架", description = "无可售 SKU 会被挡下并说明原因")
    @PostMapping("/{id}/on-shelf")
    public ApiResponse<ProductAdminView> onShelf(@PathVariable("id") Long id) {
        return ApiResponse.ok(service.onShelf(id));
    }

    @Operation(summary = "下架", description = "只改状态不删数据——历史订单还引用着它")
    @PostMapping("/{id}/off-shelf")
    public ApiResponse<ProductAdminView> offShelf(@PathVariable("id") Long id) {
        return ApiResponse.ok(service.offShelf(id));
    }
}
