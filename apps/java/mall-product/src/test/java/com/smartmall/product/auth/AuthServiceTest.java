package com.smartmall.product.auth;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.smartmall.common.api.ErrorCode;
import com.smartmall.common.auth.AuthException;
import com.smartmall.common.exception.BizException;
import com.smartmall.product.auth.dto.RegisterRequest;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.test.web.servlet.MockMvc;

import java.lang.reflect.RecordComponent;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

/**
 * 登录与注册。
 *
 * <p>这一层要钉的是两件在演示里看不出、上线才会疼的事：
 * <ul>
 *   <li><b>登录不能泄露"这个用户名存在"</b>——消息要一样，耗时也要一样；</li>
 *   <li><b>注册只能注册出买家</b>——这是个公开端点，开源之后谁都能打。</li>
 * </ul>
 *
 * <p>约定同 {@code OrderServiceTest}：所有 @Test 写在 @Nested 里。
 */
@SpringBootTest
@AutoConfigureMockMvc
class AuthServiceTest {

    @Autowired AuthService authService;
    @Autowired MockMvc mvc;
    @Autowired ObjectMapper json;
    @Autowired JdbcTemplate jdbc;

    static final BCryptPasswordEncoder BCRYPT = new BCryptPasswordEncoder();

    /** 演示口令，与 010_auth.sql 一致。 */
    static final String SEED_PASSWORD = "smartmall123";

    /** 010_auth.sql 里三个种子账号共用的那个哈希，逐字符抄过来。 */
    static final String SEED_HASH =
            "$2a$10$0T1bQy1/VNXmPAQT/9h7Tuwr8KSFPUtISz5esok09np6/gjbvZNR.";

    @BeforeEach
    void reset() {
        jdbc.execute("DELETE FROM mall_user");
    }

    /**
     * 建一个用户。<b>不指定 id</b>：H2 的 identity 序列不会因为显式插入而前进，
     * 写死 id 的话下一次自增插入就撞主键——而报错会指向注册代码，不是这里。
     */
    void seed(String username, String role, String status) {
        jdbc.update("INSERT INTO mall_user (username, password_hash, nickname, role, status)"
                + " VALUES (?, ?, ?, ?, ?)", username, SEED_HASH, "某人", role, status);
    }

    JsonNode postJson(String path, Map<String, Object> body) throws Exception {
        String raw = mvc.perform(post(path)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(body)))
                .andReturn().getResponse().getContentAsString();
        return json.readTree(raw);
    }

    // ---------------------------------------------------------------- 登录

    @Nested
    @DisplayName("登录")
    class Login {

        @Test
        @DisplayName("种子哈希能验开 smartmall123")
        void seed_hash_matches_documented_password() {
            // 010_auth.sql 的注释里写着这句。凭印象抄一个示例哈希的话，
            // 演示账号会全部登不进去，而现象是"密码不对"，看不出是种子的问题
            assertThat(BCRYPT.matches(SEED_PASSWORD, SEED_HASH)).isTrue();
            assertThat(BCRYPT.matches("wrong-password", SEED_HASH)).isFalse();
        }

        @Test
        @DisplayName("种子账号能登进来，角色带出来")
        void seed_account_logs_in() {
            seed("merchant", "merchant", "active");
            var r = authService.login("merchant", SEED_PASSWORD);
            assertThat(r.role()).isEqualTo("merchant");
            assertThat(r.token()).isNotBlank();
        }

        @Test
        @DisplayName("用户名不存在与密码错误是同一句话——否则就是个用户名枚举接口")
        void same_message_for_unknown_user_and_bad_password() {
            seed("demo", "customer", "active");

            String unknown = messageOf(() -> authService.login("nobody", SEED_PASSWORD));
            String badPass = messageOf(() -> authService.login("demo", "not-the-password"));

            assertThat(unknown).isEqualTo(badPass);
            // 而且不能把名字漏在消息里
            assertThat(unknown).doesNotContain("nobody").doesNotContain("demo");
        }

        @Test
        @DisplayName("用户名不存在也要走一遍哈希——两条路径耗时同量级")
        void unknown_user_path_is_not_faster() {
            seed("demo", "customer", "active");

            long unknown = millisOf(() -> authService.login("nobody", SEED_PASSWORD));
            long badPass = millisOf(() -> authService.login("demo", "not-the-password"));

            // 没有 DUMMY_HASH 的话，"不存在"这条是一次查询就返回，实测 <2ms；
            // 走了 BCrypt（工作因子 10）才会是几十毫秒。20ms 这条线卡在两者
            // 中间，够低不会被快机器误伤，够高能挡住"直接 return"那版实现。
            assertThat(unknown)
                    .as("不存在的用户名 %dms，密码错误 %dms", unknown, badPass)
                    .isGreaterThanOrEqualTo(20);
        }

        @Test
        @DisplayName("停用的账号，密码对也登不进来")
        void disabled_account_cannot_login() {
            seed("gone", "customer", "disabled");
            assertThatThrownBy(() -> authService.login("gone", SEED_PASSWORD))
                    .isInstanceOf(AuthException.class)
                    .hasMessageContaining("停用");
        }

        @Test
        @DisplayName("用户名大小写不敏感——MySQL 本来就这样，H2 上别装成另一种行为")
        void username_is_case_insensitive() {
            seed("demo", "customer", "active");
            assertThat(authService.login("DEMO", SEED_PASSWORD).username()).isEqualTo("demo");
            assertThat(authService.login("  Demo  ", SEED_PASSWORD).username()).isEqualTo("demo");
        }

        String messageOf(Runnable call) {
            try {
                call.run();
                throw new AssertionError("本该登录失败");
            } catch (BizException e) {
                return e.getMessage();
            }
        }

        long millisOf(Runnable call) {
            long t0 = System.nanoTime();
            try {
                call.run();
            } catch (BizException ignored) {
                // 两条路径都必然失败，这里量的是失败得多快
            }
            return (System.nanoTime() - t0) / 1_000_000;
        }
    }

    // ---------------------------------------------------------------- 注册

    @Nested
    @DisplayName("注册")
    class Register {

        @Test
        @DisplayName("注册成功直接给令牌，不用再登一次")
        void register_returns_token() {
            var r = authService.register("newbie", "password123", "新来的");
            assertThat(r.token()).isNotBlank();
            assertThat(r.userId()).isNotNull();
            assertThat(r.nickname()).isEqualTo("新来的");
        }

        @Test
        @DisplayName("注册出来的账号能用同一个口令登进来")
        void registered_account_can_login() {
            authService.register("newbie", "password123", "新来的");
            var r = authService.login("newbie", "password123");
            assertThat(r.userId()).isNotNull();
            assertThat(authService.login("newbie", "password123").userId())
                    .isEqualTo(r.userId());
        }

        @Test
        @DisplayName("库里存的是哈希，不是明文")
        void password_is_hashed_at_rest() {
            authService.register("newbie", "password123", null);
            String hash = jdbc.queryForObject(
                    "SELECT password_hash FROM mall_user WHERE username = 'newbie'",
                    String.class);
            assertThat(hash).isNotEqualTo("password123").startsWith("$2");
            assertThat(BCRYPT.matches("password123", hash)).isTrue();
        }

        @Test
        @DisplayName("昵称留空就用用户名兜底，不留一个空白的名字在页面上")
        void blank_nickname_falls_back_to_username() {
            assertThat(authService.register("a1", "password123", null).nickname())
                    .isEqualTo("a1");
            assertThat(authService.register("a2", "password123", "   ").nickname())
                    .isEqualTo("a2");
        }

        @Test
        @DisplayName("用户名被占了要报 1409，前端才好把光标放回用户名那一栏")
        void duplicate_username_rejected() {
            authService.register("taken", "password123", null);
            assertThatThrownBy(() -> authService.register("taken", "another-pass", null))
                    .isInstanceOf(BizException.class)
                    .extracting(e -> ((BizException) e).getErrorCode())
                    .isEqualTo(ErrorCode.USERNAME_TAKEN);
        }

        @Test
        @DisplayName("大小写不同的同名也算占用——否则 MySQL 上会撞唯一索引")
        void duplicate_check_is_case_insensitive() {
            authService.register("taken", "password123", null);
            assertThatThrownBy(() -> authService.register("TAKEN", "password123", null))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("已被占用");
        }

        @Test
        @DisplayName("注册的账号一律是 customer / active")
        void always_customer_and_active() {
            authService.register("newbie", "password123", null);
            Map<String, Object> row = jdbc.queryForMap(
                    "SELECT role, status FROM mall_user WHERE username = 'newbie'");
            assertThat(row.get("role")).isEqualTo("customer");
            assertThat(row.get("status")).isEqualTo("active");
        }
    }

    // ------------------------------------------------- 注册端点不能被提权

    @Nested
    @DisplayName("注册端点不能被提权")
    class NoPrivilegeEscalation {

        @Test
        @DisplayName("请求体里塞 role=merchant 也只能注册出买家")
        void role_in_body_is_ignored() throws Exception {
            var body = postJson("/api/product/auth/register", Map.of(
                    "username", "sneaky",
                    "password", "password123",
                    "nickname", "我要当商家",
                    "role", "merchant",
                    "status", "active",
                    "id", 1));

            assertThat(body.path("code").asInt()).isZero();
            assertThat(body.path("data").path("role").asText()).isEqualTo("customer");
            // 返回体可以被改，库里那行才是真的
            assertThat(jdbc.queryForObject(
                    "SELECT role FROM mall_user WHERE username = 'sneaky'", String.class))
                    .isEqualTo("customer");
        }

        @Test
        @DisplayName("RegisterRequest 上不许出现 role/status/id 字段")
        void request_dto_has_no_privileged_fields() {
            List<String> names = Arrays.stream(RegisterRequest.class.getRecordComponents())
                    .map(RecordComponent::getName)
                    .toList();
            // 这条不是重复上一条：上一条验的是"现在忽略"，这条挡的是将来有人
            // 觉得"加个 role 参数挺方便"。真加了，上一条会跟着一起绿
            assertThat(names).containsExactlyInAnyOrder("username", "password", "nickname");
        }

        @Test
        @DisplayName("注册返回体里不带密码哈希")
        void response_carries_no_password_hash() throws Exception {
            var body = postJson("/api/product/auth/register", Map.of(
                    "username", "newbie", "password", "password123", "nickname", "新来的"));
            assertThat(body.toString()).doesNotContain("$2").doesNotContain("passwordHash");
        }
    }

    // ---------------------------------------------------------------- 入参校验

    @Nested
    @DisplayName("注册入参校验")
    class Validation {

        @Test
        @DisplayName("太短的密码、带空格的用户名都要被挡在库外")
        void bad_input_rejected() throws Exception {
            assertRejected(Map.of("username", "ok_name", "password", "short"));
            assertRejected(Map.of("username", "ab", "password", "password123"));
            assertRejected(Map.of("username", "has space", "password", "password123"));
            assertRejected(Map.of("username", "中文名", "password", "password123"));
            assertRejected(Map.of("username", "a".repeat(33), "password", "password123"));
            assertRejected(Map.of("username", "ok_name", "password", "a".repeat(73)));

            // 一条都不许落库——校验挡住了但还是插进去了，是最难查的那种
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM mall_user", Integer.class))
                    .isZero();
        }

        void assertRejected(Map<String, Object> body) throws Exception {
            assertThat(postJson("/api/product/auth/register", body).path("code").asInt())
                    .as("这个入参本该被拒：%s", body)
                    .isEqualTo(ErrorCode.BAD_REQUEST.getCode());
        }
    }
}
