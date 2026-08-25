package com.smartmall.product.catalog.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableLogic;
import com.baomidou.mybatisplus.annotation.TableName;

import java.time.LocalDateTime;

/**
 * 商品。
 *
 * <p><b>三个状态是有序的</b>：{@code draft → on_sale → off_shelf}。
 * 新建一律是 draft——商家建到一半跑去吃饭，页面上不该出现一个买不了的商品。
 * 上架是一个独立动作，且有前置校验（见 ProductAdminService.onShelf）。
 */
@TableName("product")
public class Product {

    public static final String DRAFT = "draft";
    public static final String ON_SALE = "on_sale";
    public static final String OFF_SHELF = "off_shelf";

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 业务编号，对外暴露。唯一。 */
    private String productNo;

    private String name;

    /** 简称。直播口播商品对齐时作为热词，见 docs/08。 */
    private String shortName;

    private Long categoryId;
    private String brand;

    /** 卖点副标题。运营 Agent 生成的文案可以回填到这里。 */
    private String subtitle;
    private String description;

    /** 主图。运营 Agent 生成宣传图时作为视觉锚点。 */
    private String mainImage;

    private String status;

    private LocalDateTime createdAt;
    private LocalDateTime updatedAt;

    @TableLogic
    private Integer deleted;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public String getProductNo() { return productNo; }
    public void setProductNo(String productNo) { this.productNo = productNo; }
    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
    public String getShortName() { return shortName; }
    public void setShortName(String shortName) { this.shortName = shortName; }
    public Long getCategoryId() { return categoryId; }
    public void setCategoryId(Long categoryId) { this.categoryId = categoryId; }
    public String getBrand() { return brand; }
    public void setBrand(String brand) { this.brand = brand; }
    public String getSubtitle() { return subtitle; }
    public void setSubtitle(String subtitle) { this.subtitle = subtitle; }
    public String getDescription() { return description; }
    public void setDescription(String description) { this.description = description; }
    public String getMainImage() { return mainImage; }
    public void setMainImage(String mainImage) { this.mainImage = mainImage; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime createdAt) { this.createdAt = createdAt; }
    public LocalDateTime getUpdatedAt() { return updatedAt; }
    public void setUpdatedAt(LocalDateTime updatedAt) { this.updatedAt = updatedAt; }
    public Integer getDeleted() { return deleted; }
    public void setDeleted(Integer deleted) { this.deleted = deleted; }
}
