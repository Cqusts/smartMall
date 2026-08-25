package com.smartmall.common.auth;

import org.junit.jupiter.api.Test;

import java.time.Duration;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * JWT 签发与校验。
 *
 * <p>这一层要钉住的不是「能签能验」，而是<b>伪造进不来</b> —— 在它存在之前，
 * 订单接口的身份是 {@code @RequestParam("userId")}，也就是调用方自己填的。
 */
class JwtServiceTest {

    private static final String SECRET = "smartmall-test-secret-key-at-least-32-bytes";

    private JwtService svc() {
        return new JwtService(SECRET, Duration.ofHours(2), "smartmall");
    }

    private final AuthPrincipal demo =
            new AuthPrincipal(10086L, "demo", AuthPrincipal.CUSTOMER);

    @Test
    void 签发的令牌能验回同一个身份() {
        AuthPrincipal got = svc().verify(svc().issue(demo));
        assertThat(got.userId()).isEqualTo(10086L);
        assertThat(got.username()).isEqualTo("demo");
        assertThat(got.role()).isEqualTo("customer");
    }

    @Test
    void 换个密钥就验不开() {
        String token = svc().issue(demo);
        JwtService other = new JwtService(
                "another-secret-key-that-is-also-32-bytes-long", Duration.ofHours(2), "smartmall");
        assertThatThrownBy(() -> other.verify(token)).isInstanceOf(AuthException.class);
    }

    @Test
    void 改了载荷签名就对不上() {
        // 把 payload 段替换掉（把 userId 改成别人），签名段不动 —— 必须验不过。
        // 这正是「越权」最直接的攻击形态：拿自己的 token 改个 id 去查别人的订单
        String token = svc().issue(demo);
        String[] parts = token.split("\\.");
        String forged = parts[0] + "."
                + java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(
                        "{\"sub\":\"10087\",\"role\":\"merchant\"}"
                                .getBytes(java.nio.charset.StandardCharsets.UTF_8))
                + "." + parts[2];
        assertThatThrownBy(() -> svc().verify(forged)).isInstanceOf(AuthException.class);
    }

    @Test
    void 过期的令牌不认() throws Exception {
        JwtService shortLived = new JwtService(SECRET, Duration.ofMillis(1), "smartmall");
        String token = shortLived.issue(demo);
        Thread.sleep(50);
        assertThatThrownBy(() -> shortLived.verify(token)).isInstanceOf(AuthException.class);
    }

    @Test
    void 签发方不对不认() {
        String token = new JwtService(SECRET, Duration.ofHours(2), "someone-else").issue(demo);
        assertThatThrownBy(() -> svc().verify(token)).isInstanceOf(AuthException.class);
    }

    @Test
    void alg_none_攻击进不来() {
        // JWT 最经典的一类漏洞：把头部 alg 改成 none 并去掉签名，
        // 实现若「按 token 自己声明的算法去验」就会直接放行。
        // jjwt 的 verifyWith 是拿给定密钥验，不看 token 怎么说
        String header = java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(
                "{\"alg\":\"none\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8));
        String payload = java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(
                "{\"sub\":\"1\",\"role\":\"merchant\",\"iss\":\"smartmall\"}"
                        .getBytes(java.nio.charset.StandardCharsets.UTF_8));
        assertThatThrownBy(() -> svc().verify(header + "." + payload + "."))
                .isInstanceOf(AuthException.class);
    }

    @Test
    void 空令牌不认() {
        assertThatThrownBy(() -> svc().verify(null)).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> svc().verify("  ")).isInstanceOf(AuthException.class);
    }

    @Test
    void 弱密钥直接拒绝启动() {
        // 补零凑长度等于把弱密钥伪装成强密钥，而没人会发现。宁可起不来
        assertThatThrownBy(() -> new JwtService("short", Duration.ofHours(1), "smartmall"))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("32 字节");
    }

    @Test
    void 解析_Authorization_头() {
        assertThat(JwtService.bearer("Bearer abc.def.ghi")).isEqualTo("abc.def.ghi");
        assertThat(JwtService.bearer("bearer abc")).isEqualTo("abc");   // 大小写不敏感
        assertThat(JwtService.bearer("Basic abc")).isNull();
        assertThat(JwtService.bearer("Bearer   ")).isNull();
        assertThat(JwtService.bearer(null)).isNull();
    }
}
