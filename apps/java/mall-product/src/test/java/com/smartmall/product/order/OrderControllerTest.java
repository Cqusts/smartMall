package com.smartmall.product.order;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.web.servlet.MockMvc;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

/**
 * HTTP 层测试。
 *
 * <p><b>这个类的由来值得记一笔。</b>最初只写了 service 层测试，21 条全绿，
 * 然后真机一调 {@code POST /orders/{orderNo}/cancel} 就 1400：
 *
 * <pre>Name for argument of type [java.lang.String] not specified...</pre>
 *
 * 原因是本仓库的父 POM 不是 spring-boot-starter-parent，没继承到编译器的
 * {@code -parameters}，于是 {@code @PathVariable} / {@code @RequestParam}
 * 推断不出参数名。下单接口没炸，因为 {@code @RequestBody} 不依赖参数名——
 * 所以只测 service、或者只测一个 POST，都发现不了。
 *
 * <p>教训是：**控制器的绑定是一层独立的、会独立坏掉的逻辑**，service 测试
 * 再密也覆盖不到它。
 */
@SpringBootTest
@AutoConfigureMockMvc
class OrderControllerTest {

    @Autowired
    MockMvc mvc;
    @Autowired
    JdbcTemplate jdbc;
    @Autowired
    ObjectMapper json;

    static final long USER = 10086L;

    @BeforeEach
    void reset() {
        jdbc.execute("DELETE FROM mall_order");
        jdbc.execute("DELETE FROM sku");
        jdbc.update("INSERT INTO sku (sku_no, product_id, spec, price, origin_price, stock, status)"
                + " VALUES ('S-W', 9001, '{\"尺码\":\"M\"}', 100.00, 150.00, 10, 'on_sale')");
    }

    String placeAndGetOrderNo(int qty) throws Exception {
        String body = """
                {"requestId":"%s","userId":%d,"skuNo":"S-W","quantity":%d}"""
                .formatted(UUID.randomUUID(), USER, qty);
        String res = mvc.perform(post("/api/product/orders")
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andReturn().getResponse().getContentAsString();
        JsonNode n = json.readTree(res);
        assertThat(n.get("code").asInt()).as("下单应成功：%s", res).isZero();
        return n.get("data").get("orderNo").asText();
    }

    int stock() {
        return jdbc.queryForObject("SELECT stock FROM sku WHERE sku_no = 'S-W'", Integer.class);
    }

    @Nested
    @DisplayName("路径与查询参数绑定")
    class Binding {

        @Test
        @DisplayName("取消接口的 {orderNo} 与 ?userId 能正确绑定")
        void cancel_binds_path_and_query_params() throws Exception {
            String orderNo = placeAndGetOrderNo(3);
            assertThat(stock()).isEqualTo(7);

            String res = mvc.perform(post("/api/product/orders/{orderNo}/cancel", orderNo)
                            .param("userId", String.valueOf(USER)))
                    .andReturn().getResponse().getContentAsString();

            assertThat(json.readTree(res).get("code").asInt())
                    .as("绑定失败会返回 1400 而不是 0：%s", res).isZero();
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("查询接口的 {orderNo} 与 ?userId 能正确绑定")
        void get_binds_path_and_query_params() throws Exception {
            String orderNo = placeAndGetOrderNo(1);

            String res = mvc.perform(get("/api/product/orders/{orderNo}", orderNo)
                            .param("userId", String.valueOf(USER)))
                    .andReturn().getResponse().getContentAsString();

            JsonNode n = json.readTree(res);
            assertThat(n.get("code").asInt()).as(res).isZero();
            assertThat(n.get("data").get("orderNo").asText()).isEqualTo(orderNo);
        }
    }

    @Nested
    @DisplayName("入参校验")
    class Validation {

        @Test
        @DisplayName("数量为 0 被拒")
        void zero_quantity_rejected() throws Exception {
            String body = """
                    {"requestId":"v1","userId":10086,"skuNo":"S-W","quantity":0}""";
            String res = mvc.perform(post("/api/product/orders")
                            .contentType(MediaType.APPLICATION_JSON).content(body))
                    .andReturn().getResponse().getContentAsString();

            assertThat(json.readTree(res).get("code").asInt()).isEqualTo(1400);
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("超过单笔上限被拒——不设上界一次请求就能清空库存")
        void over_limit_quantity_rejected() throws Exception {
            String body = """
                    {"requestId":"v2","userId":10086,"skuNo":"S-W","quantity":9999}""";
            String res = mvc.perform(post("/api/product/orders")
                            .contentType(MediaType.APPLICATION_JSON).content(body))
                    .andReturn().getResponse().getContentAsString();

            assertThat(json.readTree(res).get("code").asInt()).isEqualTo(1400);
            assertThat(stock()).isEqualTo(10);
        }

        @Test
        @DisplayName("缺 requestId 被拒——没有幂等键就没法防重复下单")
        void missing_request_id_rejected() throws Exception {
            String body = """
                    {"userId":10086,"skuNo":"S-W","quantity":1}""";
            String res = mvc.perform(post("/api/product/orders")
                            .contentType(MediaType.APPLICATION_JSON).content(body))
                    .andReturn().getResponse().getContentAsString();

            assertThat(json.readTree(res).get("code").asInt()).isEqualTo(1400);
        }
    }

    @Nested
    @DisplayName("响应契约")
    class Contract {

        @Test
        @DisplayName("响应是统一信封，且不含 userId")
        void envelope_shape() throws Exception {
            String body = """
                    {"requestId":"%s","userId":10086,"skuNo":"S-W","quantity":1}"""
                    .formatted(UUID.randomUUID());
            String res = mvc.perform(post("/api/product/orders")
                            .contentType(MediaType.APPLICATION_JSON).content(body))
                    .andReturn().getResponse().getContentAsString();

            JsonNode n = json.readTree(res);
            assertThat(n.has("code")).isTrue();
            assertThat(n.has("message")).isTrue();
            assertThat(n.has("data")).isTrue();
            assertThat(n.has("timestamp")).isTrue();
            assertThat(res).doesNotContain("userId");
        }

        @Test
        @DisplayName("库存不足回 2409，且消息里带上还剩多少")
        void out_of_stock_code() throws Exception {
            String body = """
                    {"requestId":"%s","userId":10086,"skuNo":"S-W","quantity":20}"""
                    .formatted(UUID.randomUUID());
            String res = mvc.perform(post("/api/product/orders")
                            .contentType(MediaType.APPLICATION_JSON).content(body))
                    .andReturn().getResponse().getContentAsString();

            JsonNode n = json.readTree(res);
            assertThat(n.get("code").asInt()).isEqualTo(2409);
            assertThat(n.get("message").asText()).contains("10");
        }

        @Test
        @DisplayName("查别人的订单回 2406，与查不存在的订单一字不差")
        void cross_user_matches_not_found() throws Exception {
            String orderNo = placeAndGetOrderNo(1);

            String cross = mvc.perform(get("/api/product/orders/{n}", orderNo)
                            .param("userId", "99999"))
                    .andReturn().getResponse().getContentAsString();
            String missing = mvc.perform(get("/api/product/orders/{n}", "NOPE-404")
                            .param("userId", "99999"))
                    .andReturn().getResponse().getContentAsString();

            JsonNode a = json.readTree(cross);
            JsonNode b = json.readTree(missing);
            assertThat(a.get("code").asInt()).isEqualTo(2406);
            assertThat(a.get("code")).isEqualTo(b.get("code"));
            assertThat(a.get("message")).isEqualTo(b.get("message"));
        }
    }
}
