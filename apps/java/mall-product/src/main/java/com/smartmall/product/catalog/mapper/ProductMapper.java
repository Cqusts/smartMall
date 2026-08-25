package com.smartmall.product.catalog.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.smartmall.product.catalog.entity.Product;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
public interface ProductMapper extends BaseMapper<Product> {

    @Select("SELECT * FROM product WHERE product_no = #{no} AND deleted = 0")
    Product findByProductNo(@Param("no") String no);

    @Select("SELECT * FROM product WHERE id = #{id} AND deleted = 0")
    Product findById(@Param("id") Long id);

    /** 商家侧列表：**包含 draft 与 off_shelf**，那正是商家要管的东西。 */
    @Select("SELECT * FROM product WHERE deleted = 0 ORDER BY id DESC LIMIT #{limit}")
    List<Product> listAll(@Param("limit") int limit);

    /**
     * 这个商品下有几个可售 SKU。
     *
     * <p>上架前要拿它把关：**没有 SKU 的商品上架了也买不了**——页面上看得见、
     * 点进去选不了规格，用户只会以为网站坏了。
     */
    @Select("SELECT COUNT(*) FROM sku WHERE product_id = #{pid} AND deleted = 0"
            + " AND status = 'on_sale'")
    int countSellableSkus(@Param("pid") Long pid);
}
