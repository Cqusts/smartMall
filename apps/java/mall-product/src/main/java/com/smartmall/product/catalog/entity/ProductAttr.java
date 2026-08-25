package com.smartmall.product.catalog.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;

/**
 * 商品结构化属性。
 *
 * <p><b>这张表是防虚假宣传的事实基准。</b>运营 Agent 写完文案后，会把文案里
 * 出现的材质、成分拿来与本表比对，对不上就拦截（见 marketing/compliance.py）。
 * 所以商家在这里填的每一条，都是之后文案能说什么的边界。
 */
@TableName("product_attr")
public class ProductAttr {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long productId;

    /** 材质 | 克重 | 工艺 | 产地 | 洗涤方式 */
    private String attrKey;
    private String attrValue;

    /** 核心属性，文案必须与之一致。 */
    private Integer isCore;
    private Integer sortOrder;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public Long getProductId() { return productId; }
    public void setProductId(Long productId) { this.productId = productId; }
    public String getAttrKey() { return attrKey; }
    public void setAttrKey(String attrKey) { this.attrKey = attrKey; }
    public String getAttrValue() { return attrValue; }
    public void setAttrValue(String attrValue) { this.attrValue = attrValue; }
    public Integer getIsCore() { return isCore; }
    public void setIsCore(Integer isCore) { this.isCore = isCore; }
    public Integer getSortOrder() { return sortOrder; }
    public void setSortOrder(Integer sortOrder) { this.sortOrder = sortOrder; }
}
