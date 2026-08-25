package com.smartmall.product.auth;

import com.smartmall.common.auth.AuthException;
import com.smartmall.common.auth.AuthPrincipal;
import com.smartmall.common.auth.JwtService;
import com.smartmall.product.auth.dto.LoginResponse;
import com.smartmall.product.auth.entity.MallUser;
import com.smartmall.product.auth.mapper.MallUserMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;

@Service
public class AuthService {

    private static final Logger log = LoggerFactory.getLogger(AuthService.class);

    private final MallUserMapper userMapper;
    private final JwtService jwtService;
    private final BCryptPasswordEncoder encoder = new BCryptPasswordEncoder();

    public AuthService(MallUserMapper userMapper, JwtService jwtService) {
        this.userMapper = userMapper;
        this.jwtService = jwtService;
    }

    /**
     * 登录。
     *
     * <p><b>用户名不存在与密码错误返回同一个错误。</b> 分开报等于送了一个
     * 用户名枚举接口：攻击者拿字典跑一遍，就知道哪些账号是真的，
     * 然后只对这些账号爆破密码。
     *
     * <p><b>用户不存在时也要走一遍哈希校验。</b> 直接 return 的话，
     * 「不存在」比「密码错」快一个数量级（BCrypt 工作因子 10 大约 50–100ms），
     * 计时差本身就是那个枚举接口——只是换成了用秒表读。
     */
    public LoginResponse login(String username, String rawPassword) {
        MallUser user = userMapper.findByUsername(username);

        String hash = user != null ? user.getPasswordHash() : DUMMY_HASH;
        boolean ok = encoder.matches(rawPassword, hash);

        if (user == null || !ok) {
            log.info("登录失败 username={}", username);
            throw new AuthException("用户名或密码不正确");
        }
        if (!"active".equals(user.getStatus())) {
            throw new AuthException("账号已停用");
        }

        AuthPrincipal principal =
                new AuthPrincipal(user.getId(), user.getUsername(), user.getRole());
        return new LoginResponse(
                jwtService.issue(principal), user.getId(), user.getUsername(),
                user.getNickname(), user.getRole());
    }

    /**
     * 一个合法、但口令无人知晓的 BCrypt 哈希，用来把「用户不存在」这条路径的
     * 耗时拉到与正常校验一致（见 {@link #login} 的注释）。
     *
     * <p>由一段随机串生成，<b>工作因子必须与种子数据同为 10</b> ——
     * 不同档的话耗时还是对不齐，计时侧信道照样存在。实测两条路径
     * 各约 70ms，同量级。
     *
     * <p>它必须是**能被解析的**哈希：随手写一串假字符会让 {@code matches}
     * 在格式校验阶段就抛出/返回，快得多，等于这道防护根本没生效。
     */
    private static final String DUMMY_HASH =
            "$2a$10$3YuQjDWFDZrMMHBLxWpP5.ffB0E6fZcQgWgcMnPMIkIJEu7zw51am";
}
