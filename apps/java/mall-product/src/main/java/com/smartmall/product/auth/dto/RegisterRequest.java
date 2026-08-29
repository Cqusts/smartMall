package com.smartmall.product.auth.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/**
 * 注册入参。
 *
 * <p><b>这里没有 role 字段，而且不能有。</b> 这个接口是公开的——开源出去以后
 * 任何人 clone 下来都能打到它。多一个 role 参数，就等于给了一个「自助开通
 * 商家后台」的入口：注册时传 {@code merchant}，发货、退款审批全都开了。
 * 角色在 {@code AuthService#register} 里写死成 customer，商家账号只能由
 * 种子数据或 DBA 建。
 *
 * <p>同理没有 status 字段（否则可以自己把停用的账号注册回 active）、
 * 没有 id 字段（否则可以挑一个别人的 user_id 占位）。
 */
public record RegisterRequest(

        /*
         * 长度上限 32 是**比库里的 VARCHAR(64) 更严**的一道：库那道拦不住
         * 63 个字符的用户名，只会让它安静地存进去，然后在别处显示成一行乱码。
         * 字符集限成 字母/数字/下划线/连字符，是为了挡掉用空格、零宽字符、
         * 同形字（Cyrillic а vs Latin a）伪装成别人用户名的那类账号。
         */
        @NotBlank(message = "用户名不能为空")
        @Size(min = 3, max = 32, message = "用户名长度需在 3-32 个字符之间")
        @Pattern(regexp = "^[A-Za-z0-9_-]+$",
                message = "用户名只能包含字母、数字、下划线与连字符")
        String username,

        /*
         * 8 位下限是随手能定的最低线。上限 72 不是凑数：BCrypt **只取前 72
         * 字节**，更长的部分被静默丢弃——不设上限的话，用户以为自己设了个
         * 100 位的强口令，实际生效的只有前 72 字节，而且他永远不会知道。
         */
        @NotBlank(message = "密码不能为空")
        @Size(min = 8, max = 72, message = "密码长度需在 8-72 个字符之间")
        String password,

        /** 昵称。可为空，为空时用用户名兜底（见 AuthService#register）。 */
        @Size(max = 32, message = "昵称最长 32 个字符")
        String nickname) {
}
