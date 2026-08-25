package com.smartmall.product.auth.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.smartmall.product.auth.entity.MallUser;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

@Mapper
public interface MallUserMapper extends BaseMapper<MallUser> {

    @Select("SELECT * FROM mall_user WHERE username = #{username}")
    MallUser findByUsername(@Param("username") String username);
}
