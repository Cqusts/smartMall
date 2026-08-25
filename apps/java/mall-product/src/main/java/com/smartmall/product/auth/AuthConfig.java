package com.smartmall.product.auth;

import com.smartmall.common.auth.JwtService;
import com.smartmall.product.auth.web.CurrentUserArgumentResolver;
import com.smartmall.product.auth.web.MerchantInterceptor;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.method.support.HandlerMethodArgumentResolver;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import java.time.Duration;
import java.util.List;

@Configuration
public class AuthConfig implements WebMvcConfigurer {

    private final CurrentUserArgumentResolver currentUserResolver;
    private final MerchantInterceptor merchantInterceptor;

    public AuthConfig(CurrentUserArgumentResolver currentUserResolver,
                      MerchantInterceptor merchantInterceptor) {
        this.currentUserResolver = currentUserResolver;
        this.merchantInterceptor = merchantInterceptor;
    }

    /**
     * @param secret 至少 32 字节。默认值只够本地跑起来，**生产必须换掉**——
     *               JwtService 对长度不足会直接拒绝启动，但对"用了默认值"
     *               无从察觉，所以这里写死一个显眼的字符串，泄露了也一眼看得出。
     */
    @Bean
    public JwtService jwtService(
            @Value("${smartmall.auth.secret:CHANGE-ME-local-dev-only-secret-32bytes+}") String secret,
            @Value("${smartmall.auth.ttl-hours:12}") long ttlHours) {
        return new JwtService(secret, Duration.ofHours(ttlHours), "smartmall");
    }

    @Override
    public void addArgumentResolvers(List<HandlerMethodArgumentResolver> resolvers) {
        resolvers.add(currentUserResolver);
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(merchantInterceptor);
    }
}
