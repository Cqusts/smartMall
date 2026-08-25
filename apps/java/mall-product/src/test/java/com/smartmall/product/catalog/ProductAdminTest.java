package com.smartmall.product.catalog;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.smartmall.common.auth.AuthPrincipal;
import com.smartmall.common.auth.JwtService;
import com.smartmall.common.exception.BizException;
import com.smartmall.product.catalog.dto.*;
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

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

/**
 * 商家侧商品维护。
 *
 * <p>这一层要钉的是<b>上架前的把关</b>：没有 SKU 的商品上架了也买不了——
 * 页面上看得见、点进去选不了规格，用户只会以为网站坏了。这类"能建出来但
 * 用不了"的状态，靠人工记得检查是靠不住的。
 */
@SpringBootTest
@AutoConfigureMockMvc
class ProductAdminTest {

    @Autowired ProductAdminService service;
    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired ObjectMapper json;
    @Autowired JwtService jwtService;

    @BeforeEach
    void reset() {
        jdbc.execute("DELETE FROM mall_order");
        jdbc.execute("DELETE FROM sku");
        jdbc.execute("DELETE FROM product_attr");
        jdbc.execute("DELETE FROM product");
    }

    String token(String role) {
        return "Bearer " + jwtService.issue(new AuthPrincipal(
                "merchant".equals(role) ? 1L : 10086L, "u", role));
    }

    CreateProductRequest req(String no, List<SkuSpec> skus, Map<String, String> attrs) {
        return new CreateProductRequest(no, "测试针织衫", "针织衫", 1024L,
                "smartMall", "柔软亲肤", "描述", "x.jpg", attrs, skus);
    }

    SkuSpec sku(String no, String price, int stock) {
        return new SkuSpec(no, "{\"尺码\":\"M\"}", new BigDecimal(price), null, stock);
    }

    Map<String, String> attrs() {
        return Map.of("材质", "100%羊毛", "克重", "320g");
    }

    // ---------------------------------------------------------------- 上架把关

    @Nested
    @DisplayName("上架把关")
    class OnShelf {

        @Test
        @DisplayName("新建的商品是 draft，不是直接在售")
        void created_as_draft() {
            var v = service.create(req("P-1", List.of(sku("K-1", "299", 10)), attrs()));
            assertThat(v.status()).isEqualTo("draft");
        }

        @Test
        @DisplayName("没有 SKU 不让上架——上架了也买不了")
        void cannot_publish_without_sku() {
            var v = service.create(req("P-2", List.of(), attrs()));
            assertThatThrownBy(() -> service.onShelf(v.id()))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("没有可售 SKU");
            // 被挡下之后状态不能变
            assertThat(service.view(v.id()).status()).isEqualTo("draft");
        }

        @Test
        @DisplayName("没有属性也不让上架——运营 Agent 写不了文案")
        void cannot_publish_without_attrs() {
            var v = service.create(req("P-3", List.of(sku("K-3", "299", 5)), Map.of()));
            assertThatThrownBy(() -> service.onShelf(v.id()))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("结构化属性");
        }

        @Test
        @DisplayName("齐了就能上架——判据必须有真的会通过的分支")
        void publishes_when_ready() {
            var v = service.create(req("P-4", List.of(sku("K-4", "299", 5)), attrs()));
            assertThat(service.onShelf(v.id()).status()).isEqualTo("on_sale");
        }

        @Test
        @DisplayName("自检结果随详情一起返回，不用试一次才知道缺什么")
        void blockers_are_visible_before_trying() {
            var v = service.create(req("P-5", List.of(), Map.of()));
            assertThat(v.blockers()).hasSize(2);
            assertThat(String.join("", v.blockers())).contains("SKU").contains("属性");
        }

        @Test
        @DisplayName("下架只改状态不删数据——历史订单还引用着它")
        void off_shelf_keeps_the_row() {
            var v = service.create(req("P-6", List.of(sku("K-6", "299", 5)), attrs()));
            service.onShelf(v.id());
            assertThat(service.offShelf(v.id()).status()).isEqualTo("off_shelf");
            // 行还在，SKU 也还在
            assertThat(service.view(v.id()).skus()).hasSize(1);
        }
    }

    // ---------------------------------------------------------------- SKU

    @Nested
    @DisplayName("SKU 维护")
    class Skus {

        @Test
        @DisplayName("改价改库存")
        void upsert_updates_price_and_stock() {
            var v = service.create(req("P-7", List.of(sku("K-7", "299", 5)), attrs()));
            var after = service.upsertSku(v.id(), sku("K-7", "199", 50));
            var s = after.skus().get(0);
            assertThat(s.price()).isEqualByComparingTo("199");
            assertThat(s.stock()).isEqualTo(50);
            assertThat(after.skus()).as("是改不是新增").hasSize(1);
        }

        @Test
        @DisplayName("SKU 不能跨商品改——不校验的话会改错商品的价格且两边都不报错")
        void sku_cannot_be_moved_across_products() {
            var a = service.create(req("P-8", List.of(sku("K-8", "299", 5)), attrs()));
            var b = service.create(req("P-9", List.of(sku("K-9", "399", 5)), attrs()));

            assertThatThrownBy(() -> service.upsertSku(b.id(), sku("K-8", "1", 0)))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("属于商品");

            // A 的价格没被动过
            assertThat(service.view(a.id()).skus().get(0).price())
                    .isEqualByComparingTo("299");
        }

        @Test
        @DisplayName("重复的商品编号给出可操作的提示，不是 500")
        void duplicate_product_no_is_a_400() {
            service.create(req("P-10", List.of(sku("K-10", "1", 1)), attrs()));
            assertThatThrownBy(() -> service.create(
                    req("P-10", List.of(sku("K-11", "1", 1)), attrs())))
                    .isInstanceOf(BizException.class)
                    .hasMessageContaining("商品编号已存在");
        }
    }

    // ---------------------------------------------------------------- 属性

    @Nested
    @DisplayName("属性")
    class Attrs {

        @Test
        @DisplayName("材质自动标成核心属性——文案合规拿它当基准")
        void material_is_core() {
            var v = service.create(req("P-11", List.of(sku("K-12", "1", 1)), attrs()));
            Integer core = jdbc.queryForObject(
                    "SELECT is_core FROM product_attr WHERE product_id = ? AND attr_key = '材质'",
                    Integer.class, v.id());
            assertThat(core).isEqualTo(1);
        }

        @Test
        @DisplayName("改属性是整体替换，删得掉旧的")
        void update_replaces_attrs() {
            var v = service.create(req("P-12", List.of(sku("K-13", "1", 1)), attrs()));
            var after = service.update(v.id(), new UpdateProductRequest(
                    null, null, null, null, null, null, Map.of("材质", "涤纶")));
            assertThat(after.attrs()).containsOnlyKeys("材质");
            assertThat(after.attrs().get("材质")).isEqualTo("涤纶");
        }

        @Test
        @DisplayName("不传属性就不动它")
        void null_attrs_leaves_them_alone() {
            var v = service.create(req("P-13", List.of(sku("K-14", "1", 1)), attrs()));
            var after = service.update(v.id(), new UpdateProductRequest(
                    "改了名", null, null, null, null, null, null));
            assertThat(after.name()).isEqualTo("改了名");
            assertThat(after.attrs()).hasSize(2);
        }
    }

    // ---------------------------------------------------------------- 鉴权

    @Nested
    @DisplayName("鉴权")
    class Auth {

        String body() {
            return """
                    {"productNo":"P-HTTP","name":"x","categoryId":1024,
                     "attrs":{"材质":"棉"},"skus":[]}""";
        }

        @Test
        @DisplayName("买家令牌建商品 → 1403")
        void customer_cannot_create() throws Exception {
            String res = mvc.perform(post("/api/product/admin/products")
                            .header("Authorization", token("customer"))
                            .contentType(MediaType.APPLICATION_JSON).content(body()))
                    .andReturn().getResponse().getContentAsString();
            assertThat(json.readTree(res).get("code").asInt()).isEqualTo(1403);
        }

        @Test
        @DisplayName("不带令牌 → 1401")
        void anonymous_cannot_create() throws Exception {
            String res = mvc.perform(post("/api/product/admin/products")
                            .contentType(MediaType.APPLICATION_JSON).content(body()))
                    .andReturn().getResponse().getContentAsString();
            assertThat(json.readTree(res).get("code").asInt()).isEqualTo(1401);
        }

        @Test
        @DisplayName("商家令牌走得通")
        void merchant_can_create_and_list() throws Exception {
            String res = mvc.perform(post("/api/product/admin/products")
                            .header("Authorization", token("merchant"))
                            .contentType(MediaType.APPLICATION_JSON).content(body()))
                    .andReturn().getResponse().getContentAsString();
            JsonNode n = json.readTree(res);
            assertThat(n.get("code").asInt()).as(res).isZero();
            assertThat(n.get("data").get("status").asText()).isEqualTo("draft");

            String listed = mvc.perform(get("/api/product/admin/products")
                            .header("Authorization", token("merchant")))
                    .andReturn().getResponse().getContentAsString();
            assertThat(json.readTree(listed).get("data")).hasSize(1);
        }

        @Test
        @DisplayName("负库存被参数校验挡下")
        void negative_stock_rejected() throws Exception {
            String bad = """
                    {"productNo":"P-NEG","name":"x","categoryId":1024,
                     "skus":[{"skuNo":"N-1","spec":"{}","price":10,"stock":-5}]}""";
            String res = mvc.perform(post("/api/product/admin/products")
                            .header("Authorization", token("merchant"))
                            .contentType(MediaType.APPLICATION_JSON).content(bad))
                    .andReturn().getResponse().getContentAsString();
            assertThat(json.readTree(res).get("code").asInt()).isEqualTo(1400);
        }
    }
}
