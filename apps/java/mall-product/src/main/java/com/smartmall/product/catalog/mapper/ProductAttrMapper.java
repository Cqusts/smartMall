package com.smartmall.product.catalog.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.smartmall.product.catalog.entity.ProductAttr;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
public interface ProductAttrMapper extends BaseMapper<ProductAttr> {

    @Select("SELECT * FROM product_attr WHERE product_id = #{pid} ORDER BY sort_order, id")
    List<ProductAttr> listByProduct(@Param("pid") Long pid);

    @Delete("DELETE FROM product_attr WHERE product_id = #{pid}")
    int deleteByProduct(@Param("pid") Long pid);
}
