# -*- coding: utf-8 -*-
"""KB 权限 scope（02 §7.1，从零新建；不做复杂 RBAC）。

scope 模型：cap-user-name → scope 集合，来源 = 环境变量显式映射
（EAGENT_KB_SCOPE_MANAGERS / REVIEWERS / AGENT_ADMINS /
EAGENT_GATEWAY_SCOPE_MANAGERS，逗号分隔）。

语义（与现状治理面一致）:
- 任一映射已配置 → deny-by-default，只有映射内用户获得对应 scope；
- 全部映射未配置 → 单管理员模式（管理面本就由 X-Admin-Token 统一放行，
  与 /api/v1 既有路由的权限口径一致），全部 scope 放行。
"""
from __future__ import annotations

from fastapi import Header, Request

from .. import errors
from ..config import settings
from ..persistence.db import Database
from ..security import console_auth

SCOPE_KB_MANAGE = "kb:manage"
SCOPE_KB_REVIEW = "kb:review"
SCOPE_AGENT_MANAGE = "agent:manage"
SCOPE_GATEWAY_MANAGE = "gateway:manage"

ALL_SCOPES = (
    SCOPE_KB_MANAGE, SCOPE_KB_REVIEW, SCOPE_AGENT_MANAGE,
    SCOPE_GATEWAY_MANAGE,
)


def _mapping_configured() -> bool:
    return bool(
        settings.kb_scope_managers or settings.kb_scope_reviewers
        or settings.kb_scope_agent_admins or settings.gateway_scope_managers,
    )


def user_scopes(cap_user_name: str) -> set[str]:
    if not cap_user_name:
        # 匿名一律无 scope（治理操作要求可归责身份，deny-by-default）；
        # 运行面 /a2a 的匿名可调不经过本层，由可见服务集合约束
        return set()
    if not _mapping_configured():
        return set(ALL_SCOPES)  # 单管理员模式（见模块 docstring）
    scopes: set[str] = set()
    if cap_user_name in settings.kb_scope_managers:
        scopes.add(SCOPE_KB_MANAGE)
    if cap_user_name in settings.kb_scope_reviewers:
        scopes.add(SCOPE_KB_REVIEW)
    if cap_user_name in settings.kb_scope_agent_admins:
        scopes.add(SCOPE_AGENT_MANAGE)
    if cap_user_name in settings.gateway_scope_managers:
        scopes.add(SCOPE_GATEWAY_MANAGE)
    return scopes


def session_scopes(roles: set[str] | list[str]) -> set[str]:
    """会话角色 → scope（admin-auth 01 §3.2）：eagent_admin 全量；
    eagent_user（及未知角色）无治理 scope。"""
    if console_auth.ROLE_ADMIN in set(roles):
        return set(ALL_SCOPES)
    return set()


def make_scope_dependency(scope: str):
    """FastAPI 依赖工厂：无 scope → 403（deny-by-default）。

    双轨判定顺序（admin-auth 01）：
    1. Bearer 会话（admin / Pluto 员工）——**中间件已验真**并挂
       request.state.session_ctx（review P0 后中间件是唯一闸门，此处只取
       结果；Bearer 无效的请求到不了路由层）；
    2. 头通道（脚本/过渡期）：cap-user-name + 环境变量映射。
    身份头选填后，无身份 = 匿名 = 无 scope，同样 403（治理操作必须
    可归责身份）。无 Bearer 头时不产生 DB 查询。"""

    def _dep(
        request: Request, cap_user_name: str | None = Header(default=None),
    ) -> str:
        ctx = getattr(request.state, "session_ctx", None)
        if ctx is None and console_auth.bearer_token(request):
            # 防御：正常到不了（中间件已挡），保守 401 而非回落头通道
            db = Database()
            try:
                ctx = console_auth.auth_context_from_request(db, request)
            finally:
                db.close()
            if ctx is None:
                raise errors.UnauthenticatedError("登录会话无效或已过期")
        if ctx is not None:
            if scope in session_scopes(ctx.roles):
                return ctx.user_id
            # 页面权限把旧的粗粒度 scope 拆到具体控制台页面。中间件已做
            # 第一道校验；这里让仍挂旧 scope 依赖的路由复用同一映射。
            from .page_permissions import mapped_request_allowed
            if mapped_request_allowed(
                request, ctx.user_id, ctx.is_admin(),
            ):
                return ctx.user_id
            raise errors.ForbiddenError(f"缺少权限 scope: {scope}")
        user = (cap_user_name or "").strip()
        if scope not in user_scopes(user):
            raise errors.ForbiddenError(f"缺少权限 scope: {scope}")
        return user

    return _dep


require_kb_manage = make_scope_dependency(SCOPE_KB_MANAGE)
require_kb_review = make_scope_dependency(SCOPE_KB_REVIEW)
require_agent_manage = make_scope_dependency(SCOPE_AGENT_MANAGE)
require_gateway_manage = make_scope_dependency(SCOPE_GATEWAY_MANAGE)


# ---------------------------------------------------------------------------
# 角色级依赖（permission-control v1）
# ---------------------------------------------------------------------------
def require_admin_dep(request: Request) -> str:
    """管理员角色检查（权限管理页 / 用户管理专用）。

    只认会话角色 eagent_admin（本地 admin 账号、被提权的 Pluto 工号）；
    strict 模式下经中间件 hmac 验真的 X-Admin-Token 亦视为管理员（脚本
    兼容）。env 头通道**不参与**——角色管理本身不允许被环境变量放行。
    身份头经 request.headers 直读（不走 Header 参数，避免与
    /users/{cap_user_name}/... 的路径参数撞名）。"""
    ctx = getattr(request.state, "session_ctx", None)
    if ctx is None and console_auth.bearer_token(request):
        db = Database()
        try:
            ctx = console_auth.auth_context_from_request(db, request)
        finally:
            db.close()
        if ctx is None:
            raise errors.UnauthenticatedError("登录会话无效或已过期")
    if ctx is not None:
        if ctx.is_admin():  # AuthContext.is_admin 是方法不是 property
            return ctx.user_id
        raise errors.ForbiddenError("需要管理员角色")
    if request.headers.get("x-admin-token"):
        return (request.headers.get("cap-user-name") or "").strip() or "admin"
    raise errors.UnauthenticatedError("需要登录会话")


require_admin = require_admin_dep


def make_console_user_dependency():
    """使用面身份依赖（试运行/我的权限）：任何已认证身份即可，返回
    (user_id, is_admin)。

    双轨口径与 make_scope_dependency 一致：Bearer 会话按角色判 is_admin；
    头通道在单管理员模式（env 未配映射）下视为受信身份（可代指定身份
    试运行），配了映射则按 scope 有无判定。"""
    def _dep(
        request: Request, cap_user_name: str | None = Header(default=None),
    ) -> tuple[str, bool]:
        ctx = getattr(request.state, "session_ctx", None)
        if ctx is None and console_auth.bearer_token(request):
            db = Database()
            try:
                ctx = console_auth.auth_context_from_request(db, request)
            finally:
                db.close()
            if ctx is None:
                raise errors.UnauthenticatedError("登录会话无效或已过期")
        if ctx is not None:
            return ctx.user_id, ctx.is_admin()
        user = (cap_user_name or "").strip()
        if not user:
            raise errors.UnauthenticatedError("需要登录会话或身份头")
        return user, bool(user_scopes(user))

    return _dep


require_console_user = make_console_user_dependency()
