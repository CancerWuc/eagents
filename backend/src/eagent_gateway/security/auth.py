# -*- coding: utf-8 -*-
"""网关鉴权（设计 §10.4 / T3 一期落地方）。

两个身份域：
- 管理面 `/api/v1/*`：strict 模式需 `X-Admin-Token`；治理操作另受
  permission/scopes 授权层约束（无身份 = 匿名 = 无映射 scope）
- 运行面 `/a2a/*`：由 A2A 子应用从有效管理台 Session 或可信入口身份建立
  RuntimePrincipal；外部身份必须在用户目录中存在且启用；Card 发现公开

通用 Header 解析器允许 `cap-user-name` / `real-name` 缺失并兼容下划线；
但 A2A 运行面要求“有效 Session 或可信 cap-user-name”至少一种，且不会把
这里解析的原始身份直接作为 Principal。权威身份出站传播见
router/context.py。

`EAGENT_AUTH_MODE`:
- strict（默认）: 必须配置 EAGENT_ADMIN_TOKEN，否则启动失败；管理面校验 token
- dev: 仅本地开发，跳过 admin token
生产应在统一登录网关之后部署，由网关注入可信身份头（T3 最终方案）。
"""
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import settings
from ..errors import ForbiddenError
from ..persistence.db import Database
from . import console_auth
from .identity import extract_identity

PUBLIC_PREFIXES = (
    "/healthz",
    "/.well-known/",
)
# A2A 子应用内的 Card 发现路径（对 /a2a/{slug}/.well-known/... 放行）
WELL_KNOWN = "/.well-known/"


def assert_production_auth_configured() -> None:
    """fail-secure: strict 模式没配 token 直接拒绝启动。"""
    if settings.auth_mode == "strict" and not settings.admin_token:
        raise RuntimeError(
            "EAGENT_AUTH_MODE=strict 但未配置 EAGENT_ADMIN_TOKEN；"
            "生产必须提供管理令牌（本地开发可设 EAGENT_AUTH_MODE=dev）",
        )
    # 启动自检（review P2）：strict 下 ADMIN_PASSWORD 与 Pluto 都未配置时，
    # 管理面没有任何可用登录方式（会锁死），启动即告警提醒
    if settings.auth_mode == "strict" and not settings.admin_password:
        import logging

        from .pluto_oauth import pluto_configured

        if not pluto_configured():
            logging.getLogger("eagent.auth").warning(
                "strict 模式未配置 ADMIN_PASSWORD 且 Pluto 未配置："
                "管理面将没有任何可用的登录方式",
            )


# 登录端点（免管理令牌——它们自己签发凭据；login-topic §3.1）
LOGIN_PREFIXES = (
    "/api/v1/admin/auth/",
    "/api/v1/admin/console/",
)


class GatewayAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        path = request.url.path

        if path.startswith(PUBLIC_PREFIXES) or (
            # Card 发现（review P2：仅限 /a2a/ 前缀，防 /api/v1/x/.well-known/ 绕过）
            path.startswith("/a2a/") and WELL_KNOWN in path
        ):
            return await call_next(request)

        # 身份提取统一口径：连字符 canonical、兼容下划线；real-name 选填
        cap_user, real_name = extract_identity(request.headers)
        # 归一身份挂 state，下游（/api/* 路由等）不必再各自兼容两种拼写
        request.state.cap_user_name = cap_user or None
        request.state.real_name = real_name

        if path.startswith("/api/"):
            # 双轨（admin-auth 01）：Bearer 会话在中间件内**真实校验**——
            # review P0：存量路由多数未挂鉴权依赖，"带 Bearer 头即放行"会把
            # 未挂依赖的治理路由对任意伪造 Bearer 开放。无效会话一律 401
            # （即使同时带有效 X-Admin-Token，让调用方主动清过期会话）；
            # 有效会话挂 request.state.session_ctx 供 scopes 依赖复用。
            if console_auth.bearer_token(request):
                db = Database()
                try:
                    ctx = console_auth.auth_context_from_request(db, request)
                finally:
                    db.close()
                if ctx is None:
                    return _unauthorized(request, "登录会话无效或已过期")
                request.state.session_ctx = ctx
                # 页面权限是控制台 API 的最终边界；管理员直接放行，普通用户
                # 在进入路由、读取资源或产生副作用之前完成校验。
                from ..permission.page_permissions import authorize_mapped_request
                try:
                    authorize_mapped_request(
                        request, ctx.user_id, ctx.is_admin(),
                    )
                except ForbiddenError as exc:
                    return JSONResponse(
                        {
                            "code": "FORBIDDEN", "message": str(exc),
                            "traceId": getattr(request.state, "trace_id", None),
                        },
                        status_code=403,
                    )
                return await call_next(request)
            if path.startswith(LOGIN_PREFIXES):
                # 登录端点免令牌（自己签发凭据；限流见 console_auth.admin_login）
                return await call_next(request)
            if settings.auth_mode == "strict":
                token = request.headers.get("x-admin-token", "")
                if not token or not hmac.compare_digest(
                    token, settings.admin_token,
                ):
                    return _unauthorized(
                        request, "管理令牌无效或缺省（X-Admin-Token）",
                    )
            return await call_next(request)

        if path.startswith("/a2a/"):
            # A2A 子应用在 SDK 创建 Task 前完成 Session/可信外部身份解析、
            # 用户目录准入和单服务授权。
            return await call_next(request)

        return await call_next(request)


def _unauthorized(request: Request, message: str) -> JSONResponse:
    """401 统一为 platform-error.v1 结构（traceId 由 TraceContextMiddleware 注入）。"""
    return JSONResponse(
        {
            "code": "UNAUTHENTICATED",
            "message": message,
            "traceId": getattr(request.state, "trace_id", None),
        },
        status_code=401,
    )
