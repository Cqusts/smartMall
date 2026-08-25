package com.smartmall.common.auth;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.Date;

/**
 * JWT 签发与校验。
 *
 * <p><b>为什么校验要放在每个业务服务里，而不是只放网关。</b>
 * 常见做法是「网关校验 token → 注入 X-User-Id → 下游直接信任这个头」。
 * 这个项目不能这么做：Python 店铺页是<b>直连</b> {@code localhost:8081} 调订单的，
 * 网关根本不在那条路径上。只在网关校验的话，{@code curl localhost:8081} 就绕过去了
 * —— 洞还在，只是看起来堵上了。
 *
 * <p>推论是：<b>下游绝不能信任任何请求头里的身份</b>。哪怕网关已经校验过，
 * 业务服务也要自己验一遍签名。多花的那点 CPU 换的是「鉴权在被访问的那一端」，
 * 而不是「在我希望别人走的那条路上」。
 *
 * <p>算法固定 HS256，密钥从配置来。<b>校验时不读 token 头里的 alg</b> ——
 * jjwt 的 {@code verifyWith} 会用给定的密钥去验，攻击者把 alg 改成 none 或 HS/RS
 * 混淆都过不了。这是 JWT 最经典的一类漏洞，值得在这里点名。
 */
public class JwtService {

    /** 至少 32 字节，HS256 的要求。密钥太短 jjwt 会直接拒绝签发。 */
    private final SecretKey key;
    private final Duration ttl;
    private final String issuer;

    public JwtService(String secret, Duration ttl, String issuer) {
        byte[] raw = secret == null ? new byte[0] : secret.getBytes(StandardCharsets.UTF_8);
        if (raw.length < 32) {
            // 与其悄悄补零凑长度，不如直接不让它起来：补零等于把一个 8 字节的
            // 弱密钥伪装成 32 字节的强密钥，而没人会发现
            throw new IllegalArgumentException(
                    "JWT 密钥至少 32 字节（当前 " + raw.length + "）。"
                    + "配置项 smartmall.auth.secret，生产环境必须换掉默认值。");
        }
        this.key = Keys.hmacShaKeyFor(raw);
        this.ttl = ttl;
        this.issuer = issuer;
    }

    public String issue(AuthPrincipal principal) {
        Instant now = Instant.now();
        return Jwts.builder()
                .issuer(issuer)
                .subject(String.valueOf(principal.userId()))
                .claim("username", principal.username())
                .claim("role", principal.role())
                .issuedAt(Date.from(now))
                .expiration(Date.from(now.plus(ttl)))
                .signWith(key)
                .compact();
    }

    /**
     * 校验并解出身份。
     *
     * @throws AuthException 签名不对、过期、签发方不对、内容缺字段 —— <b>一律同一个异常</b>。
     *         分开报「签名错误」和「已过期」对调用方没有用，对攻击者倒是有用：
     *         那等于告诉他「这个 token 格式对了，只是过期了」。
     */
    public AuthPrincipal verify(String token) {
        if (token == null || token.isBlank()) {
            throw new AuthException("缺少令牌");
        }
        try {
            Claims claims = Jwts.parser()
                    .verifyWith(key)
                    .requireIssuer(issuer)
                    .build()
                    .parseSignedClaims(token)
                    .getPayload();

            String role = claims.get("role", String.class);
            if (role == null || role.isBlank()) {
                throw new AuthException("令牌缺少角色");
            }
            return new AuthPrincipal(
                    Long.valueOf(claims.getSubject()),
                    claims.get("username", String.class),
                    role);
        } catch (JwtException | IllegalArgumentException e) {
            throw new AuthException("令牌无效或已过期");
        }
    }

    /** 从 {@code Authorization: Bearer xxx} 里取出 token，取不到返回 null。 */
    public static String bearer(String header) {
        if (header == null || !header.regionMatches(true, 0, "Bearer ", 0, 7)) {
            return null;
        }
        String token = header.substring(7).trim();
        return token.isEmpty() ? null : token;
    }
}
