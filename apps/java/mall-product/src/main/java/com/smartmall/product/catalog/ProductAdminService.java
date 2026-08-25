package com.smartmall.product.catalog;

import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.exception.BizException;
import com.smartmall.product.catalog.dto.*;
import com.smartmall.product.catalog.entity.Product;
import com.smartmall.product.catalog.entity.ProductAttr;
import com.smartmall.product.catalog.mapper.ProductAttrMapper;
import com.smartmall.product.catalog.mapper.ProductMapper;
import com.smartmall.product.order.entity.Sku;
import com.smartmall.product.order.mapper.SkuMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.*;

/**
 * 商家侧的商品维护。
 *
 * <p><b>这一层的存在理由是「写操作必须走 Java」。</b>Python 侧的工具层是
 * 刻意全只读的（见 agent/tools.py：AI 误触发的改价、下架是不可逆的损失）。
 * 商品的增删改如果也在 Python 做，那道只读边界就开了个口子，而且会出现
 * 两份实现——库存与价格到底以谁为准就说不清了。
 */
@Service
public class ProductAdminService {

    private static final Logger log = LoggerFactory.getLogger(ProductAdminService.class);

    private final ProductMapper productMapper;
    private final ProductAttrMapper attrMapper;
    private final SkuMapper skuMapper;

    public ProductAdminService(ProductMapper productMapper,
                               ProductAttrMapper attrMapper,
                               SkuMapper skuMapper) {
        this.productMapper = productMapper;
        this.attrMapper = attrMapper;
        this.skuMapper = skuMapper;
    }

    // ------------------------------------------------------------ 建

    @Transactional
    public ProductAdminView create(CreateProductRequest req) {
        if (productMapper.findByProductNo(req.productNo()) != null) {
            // 直接撞唯一索引会抛 DuplicateKeyException → 9500，页面上是"系统内部
            // 错误"。而这其实是个用户改一下就能解决的问题，要说清楚
            throw new BizException(ErrorCode.BAD_REQUEST,
                    "商品编号已存在：" + req.productNo());
        }

        Product p = new Product();
        p.setProductNo(req.productNo());
        p.setName(req.name());
        p.setShortName(req.shortName());
        p.setCategoryId(req.categoryId());
        p.setBrand(req.brand());
        p.setSubtitle(req.subtitle());
        p.setDescription(req.description());
        p.setMainImage(req.mainImage());
        // **一律建成 draft。** 建到一半跑去吃饭，页面上不该出现买不了的商品
        p.setStatus(Product.DRAFT);
        productMapper.insert(p);

        replaceAttrs(p.getId(), req.attrs());
        for (SkuSpec s : Optional.ofNullable(req.skus()).orElse(List.of())) {
            insertSku(p.getId(), s);
        }
        log.info("建商品 #{} {} 编号={}", p.getId(), p.getName(), p.getProductNo());
        return view(p.getId());
    }

    // ------------------------------------------------------------ 改

    @Transactional
    public ProductAdminView update(Long id, UpdateProductRequest req) {
        Product p = require(id);
        if (req.name() != null) p.setName(req.name());
        if (req.shortName() != null) p.setShortName(req.shortName());
        if (req.brand() != null) p.setBrand(req.brand());
        if (req.subtitle() != null) p.setSubtitle(req.subtitle());
        if (req.description() != null) p.setDescription(req.description());
        if (req.mainImage() != null) p.setMainImage(req.mainImage());
        productMapper.updateById(p);

        // 非 null 时**整体替换**。增量合并没法表达"删掉这条属性"，
        // 而属性表是文案合规的事实基准，删不掉就意味着旧属性会一直约束新文案
        if (req.attrs() != null) {
            replaceAttrs(id, req.attrs());
        }
        return view(id);
    }

    @Transactional
    public ProductAdminView upsertSku(Long productId, SkuSpec spec) {
        require(productId);
        Sku existing = skuMapper.findBySkuNo(spec.skuNo());
        if (existing != null && !Objects.equals(existing.getProductId(), productId)) {
            // **不是小题大做**：不校验的话，改 A 商品的价格会改到 B 商品的 SKU 上，
            // 而两边都不会报错——错误要等到用户下单付了错价才暴露
            throw new BizException(ErrorCode.BAD_REQUEST,
                    "SKU " + spec.skuNo() + " 属于商品 #" + existing.getProductId()
                    + "，不能挂到 #" + productId + " 下");
        }
        if (existing == null) {
            insertSku(productId, spec);
        } else {
            existing.setSpec(spec.spec());
            existing.setPrice(spec.price());
            existing.setOriginPrice(spec.originPrice());
            existing.setStock(spec.stock());
            skuMapper.updateById(existing);
            log.info("改 SKU {} 价格={} 库存={}", spec.skuNo(), spec.price(), spec.stock());
        }
        return view(productId);
    }

    // ------------------------------------------------------------ 上下架

    /**
     * 上架。
     *
     * <p><b>有前置校验，而且校验失败是常态不是异常。</b>没有 SKU 的商品上架了
     * 也买不了——页面上看得见、点进去选不了规格，用户只会以为网站坏了。
     * 所以这里宁可挡住，并且把原因逐条说清楚。
     */
    @Transactional
    public ProductAdminView onShelf(Long id) {
        Product p = require(id);
        List<String> blockers = blockers(p);
        if (!blockers.isEmpty()) {
            throw new BizException(ErrorCode.BAD_REQUEST,
                    "不能上架：" + String.join("；", blockers));
        }
        p.setStatus(Product.ON_SALE);
        productMapper.updateById(p);
        log.info("上架商品 #{} {}", id, p.getName());
        return view(id);
    }

    /**
     * 下架。
     *
     * <p><b>只改状态，不删数据。</b>已经产生的订单引用着这个商品与 SKU，
     * 删了之后那些订单就查不出商品名——用户看自己买过什么会看到一片空白。
     */
    @Transactional
    public ProductAdminView offShelf(Long id) {
        Product p = require(id);
        p.setStatus(Product.OFF_SHELF);
        productMapper.updateById(p);
        log.info("下架商品 #{} {}", id, p.getName());
        return view(id);
    }

    /** 上架前的自检。空列表 = 可以上架。 */
    public List<String> blockers(Product p) {
        List<String> out = new ArrayList<>();
        if (productMapper.countSellableSkus(p.getId()) == 0) {
            out.add("没有可售 SKU，上架了也买不了");
        }
        if (attrMapper.listByProduct(p.getId()).isEmpty()) {
            // 不是硬伤，但要提醒：属性表空着的话运营 Agent 写不了文案
            // （它会明说"属性表是空的"而不是硬编）
            out.add("没有结构化属性，运营 Agent 无法生成文案");
        }
        return out;
    }

    // ------------------------------------------------------------ 查

    public List<ProductAdminView> list(int limit) {
        return productMapper.listAll(Math.min(Math.max(limit, 1), 200))
                .stream().map(p -> view(p.getId())).toList();
    }

    public ProductAdminView view(Long id) {
        Product p = require(id);
        Map<String, String> attrs = new LinkedHashMap<>();
        for (ProductAttr a : attrMapper.listByProduct(id)) {
            attrs.put(a.getAttrKey(), a.getAttrValue());
        }
        List<ProductAdminView.SkuView> skus = skuMapper.listByProduct(id).stream()
                .map(s -> new ProductAdminView.SkuView(
                        s.getSkuNo(), s.getSpec(), s.getPrice(), s.getOriginPrice(),
                        s.getStock(), s.getStatus()))
                .toList();
        return new ProductAdminView(
                p.getId(), p.getProductNo(), p.getName(), p.getShortName(),
                p.getCategoryId(), p.getBrand(), p.getSubtitle(), p.getDescription(),
                p.getMainImage(), p.getStatus(), attrs, skus, blockers(p));
    }

    // ------------------------------------------------------------ 内部

    private Product require(Long id) {
        Product p = productMapper.findById(id);
        if (p == null) {
            throw new BizException(ErrorCode.PRODUCT_NOT_FOUND, "商品不存在：#" + id);
        }
        return p;
    }

    private void replaceAttrs(Long productId, Map<String, String> attrs) {
        attrMapper.deleteByProduct(productId);
        if (attrs == null) return;
        int order = 0;
        for (Map.Entry<String, String> e : attrs.entrySet()) {
            if (e.getKey() == null || e.getKey().isBlank()) continue;
            ProductAttr a = new ProductAttr();
            a.setProductId(productId);
            a.setAttrKey(e.getKey());
            a.setAttrValue(e.getValue() == null ? "" : e.getValue());
            // 材质是核心属性——文案合规检查拿它当基准（marketing/compliance.py）
            a.setIsCore("材质".equals(e.getKey()) ? 1 : 0);
            a.setSortOrder(order++);
            attrMapper.insert(a);
        }
    }

    private void insertSku(Long productId, SkuSpec spec) {
        if (skuMapper.findBySkuNo(spec.skuNo()) != null) {
            throw new BizException(ErrorCode.BAD_REQUEST,
                    "SKU 编号已存在：" + spec.skuNo());
        }
        Sku s = new Sku();
        s.setSkuNo(spec.skuNo());
        s.setProductId(productId);
        s.setSpec(spec.spec());
        s.setPrice(spec.price());
        s.setOriginPrice(spec.originPrice());
        s.setStock(spec.stock());
        s.setStatus("on_sale");
        skuMapper.insert(s);
    }
}
