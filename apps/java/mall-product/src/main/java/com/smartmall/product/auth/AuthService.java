package com.smartmall.product.auth;

import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.auth.AuthException;
import com.smartmall.common.auth.AuthPrincipal;
import com.smartmall.common.auth.JwtService;
import com.smartmall.common.exception.BizException;
import com.smartmall.product.auth.dto.LoginResponse;
import com.smartmall.product.auth.entity.MallUser;
import com.smartmall.product.auth.mapper.MallUserMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;

import java.util.Locale;

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
        MallUser user = userMapper.findByUsername(normalize(username));

        String hash = user != null ? user.getPasswordHash() : DUMMY_HASH;
        boolean ok = encoder.matches(rawPassword, hash);

        if (user == null || !ok) {
            log.info("登录失败 username={}", username);
            throw new AuthException("用户名或密码不正确");
        }
        if (!"active".equals(user.getStatus())) {
            throw new AuthException("账号已停用");
        }

        return issue(user);
    }

    /**
     * 注册一个买家账号，并直接发令牌（注册完不用再登一次）。
     *
     * <p><b>角色写死 customer，不接受调用方指定。</b> 这个接口是公开的：开源
     * 之后任何人都能打到它。只要 role 是个参数，某天就会有人传 merchant——
     * 那就等于把发货、退款审批、素材审核的后台自助开通了。商家账号只从种子
     * 数据或 DBA 手工来。（素材那边写过同一句：能传参数就意味着某天会有人
     * 传 approved。）
     *
     * <p>status 同理写死 active：否则一个被停用的账号，用同一个用户名再注册
     * 一次就"复活"了——那时唯一索引会挡住，但如果哪天改成 upsert 就不会。
     *
     * <p><b>先查重只是为了给一句人话的报错，真正的保证是唯一索引。</b>
     * 查完到插入之间隔着一个窗口，两个请求可以同时查到"没人用"再一起插——
     * 只靠这个查重，并发注册就会各建一个同名账号（如果索引也没有的话），
     * 之后 findByUsername 返回哪一个是随机的。所以 DuplicateKeyException
     * 那条分支不是防御性代码，是那个窗口真实的出口。
     *
     * <p>没做限流。这个端点现在可以被脚本反复调用灌垃圾账号——真要上线得在
     * 网关加 IP 维度限流或验证码，本项目定位是作品集，先把这条限制写在这里。
     */
    public LoginResponse register(String username, String rawPassword, String nickname) {
        String name = normalize(username);

        if (userMapper.findByUsername(name) != null) {
            throw new BizException(ErrorCode.USERNAME_TAKEN, "用户名已被占用，换一个试试");
        }

        MallUser user = new MallUser();
        user.setUsername(name);
        user.setPasswordHash(encoder.encode(rawPassword));
        user.setNickname(nickname == null || nickname.isBlank() ? name : nickname.trim());
        user.setRole("customer");
        user.setStatus("active");

        try {
            userMapper.insert(user);
        } catch (DuplicateKeyException e) {
            // 查重与插入之间那个窗口，见方法注释
            throw new BizException(ErrorCode.USERNAME_TAKEN, "用户名已被占用，换一个试试");
        }

        log.info("注册成功 username={} userId={}", name, user.getId());
        return issue(user);
    }

    private LoginResponse issue(MallUser user) {
        AuthPrincipal principal =
                new AuthPrincipal(user.getId(), user.getUsername(), user.getRole());
        return new LoginResponse(
                jwtService.issue(principal), user.getId(), user.getUsername(),
                user.getNickname(), user.getRole());
    }

    /**
     * 用户名一律按小写存、按小写查。
     *
     * <p>不归一的话，同一份代码在两个库上是两种行为：MySQL 默认排序规则
     * （utf8mb4_0900_ai_ci）比较字符串**不分大小写**，{@code Demo} 会被唯一索引
     * 当成 {@code demo} 挡掉；H2 分大小写，两个都能注册进去，然后登录时
     * 谁被查出来看运气。测试全绿、上线报重复键，就是这么来的。
     *
     * <p>{@code Locale.ROOT} 不能省。默认 locale 在土耳其语环境下会把 {@code I}
     * 转成无点的 {@code ı}，那个字符落到库里就再也登不进来了——而这台机器上
     * 永远复现不了。
     */
    private static String normalize(String username) {
        return username == null ? "" : username.trim().toLowerCase(Locale.ROOT);
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
