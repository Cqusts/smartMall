package com.smartmall.product.auth;

import com.smartmall.common.api.ApiResponse;
import com.smartmall.common.auth.AuthPrincipal;
import com.smartmall.product.auth.dto.LoginRequest;
import com.smartmall.product.auth.dto.LoginResponse;
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

    @Operation(summary = "当前登录身份")
    @GetMapping("/me")
    public ApiResponse<AuthPrincipal> me(@CurrentUser AuthPrincipal principal) {
        return ApiResponse.ok(principal);
    }
}
