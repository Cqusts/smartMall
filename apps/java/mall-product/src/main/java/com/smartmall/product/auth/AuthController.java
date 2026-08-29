package com.smartmall.product.auth;

import com.smartmall.common.api.ApiResponse;
import com.smartmall.common.auth.AuthPrincipal;
import com.smartmall.product.auth.dto.LoginRequest;
import com.smartmall.product.auth.dto.LoginResponse;
import com.smartmall.product.auth.dto.RegisterRequest;
import com.smartmall.product.auth.web.CurrentUser;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;

@Tag(name = "认证")
@RestController
@RequestMapping("/api/product/auth")
public class AuthController {

    private final AuthService authService;

    public AuthController(AuthService authService) {
        this.authService = authService;
    }

    @Operation(summary = "登录，换取 JWT")
    @PostMapping("/login")
    public ApiResponse<LoginResponse> login(@Valid @RequestBody LoginRequest req) {
        return ApiResponse.ok(authService.login(req.username(), req.password()));
    }

    /**
     * 注册买家账号。
     *
     * <p><b>只能注册买家。</b> {@link RegisterRequest} 里没有 role，也不会有——
     * 理由见那个类与 {@link AuthService#register} 的注释。想要商家账号，走
     * {@code deploy/sql/migrations/010_auth.sql} 的种子或让 DBA 建。
     */
    @Operation(summary = "注册买家账号，注册成功直接返回 JWT")
    @PostMapping("/register")
    public ApiResponse<LoginResponse> register(@Valid @RequestBody RegisterRequest req) {
        return ApiResponse.ok(
                authService.register(req.username(), req.password(), req.nickname()));
    }

    @Operation(summary = "当前登录身份")
    @GetMapping("/me")
    public ApiResponse<AuthPrincipal> me(@CurrentUser AuthPrincipal principal) {
        return ApiResponse.ok(principal);
    }
}
