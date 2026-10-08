# -*- coding: utf-8 -*-
"""管理面登录与会话接口（admin-auth 01；login-topic §3.1 五接口）。

- `GET  /api/v1/admin/console/login`    生成 state + authorize_url（跳 Pluto）
- `POST /api/v1/admin/console/callback` code 换 token → userinfo → 懒同步
  user → 签发 ConsoleSession
- `POST /api/v1/admin/console/logout`   注销会话（双轨通用，按 token_hash 删）
- `POST /api/v1/admin/auth/login`       admin 本地密码登录（受开关控制）
- `GET/PUT /api/v1/admin/auth/local-login`  开关读/写（写需 admin；dev 禁关）

state 防 CSRF：无 Redis 的无状态实现——state = 过期时间戳 + HMAC 签名
（密钥取 pluto secret，未配置时回退 admin token），回调时验签 + 查过期。
错误统一 platform-error.v1；token/密钥不入日志。
"""
import hashlib
import hmac
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..config import settings
from ..errors import (
    DependencyError,
    ForbiddenError,
    UnauthenticatedError,
    ValidationError,
)
from ..persistence.db import Database
from ..security import console_auth, pluto_oauth

router = APIRouter(prefix="/api/v1")

_STATE_TTL_S = 600


class ConsoleCallbackIn(BaseModel):
    code: str
    state: str


class AdminLoginIn(BaseModel):
    username: str
    password: str


class LocalLoginIn(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# state 签发与校验（无状态 CSRF 防护）
# ---------------------------------------------------------------------------
def _state_key() -> bytes:
    secret = settings.pluto_access_secret or settings.admin_token
    return hashlib.sha256(("eagent-console-state:" + secret).encode()).digest()


def issue_state() -> str:
    payload = str(int(time.time()) + _STATE_TTL_S)
    sig = hmac.new(_state_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_state(state: str) -> bool:
    try:
        payload, sig = state.split(".", 1)
        expected = hmac.new(
            _state_key(), payload.encode(), hashlib.sha256,
        ).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return False
        return int(payload) >= int(time.time())
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# 五接口
# ---------------------------------------------------------------------------
@router.get("/admin/console/login")
async def console_login() -> dict:
    if not pluto_oauth.pluto_configured():
        raise ValidationError(
            "Pluto 未配置，员工登录不可用（请使用本地管理员账号登录）",
        )
    state = issue_state()
    return {
        "authorize_url": pluto_oauth.build_authorize_url(state),
        "state": state,
    }


@router.post("/admin/console/callback")
async def console_callback(body: ConsoleCallbackIn) -> dict:
    if not verify_state(body.state):
        raise ValidationError("state 无效或已过期，请重新发起登录")
    try:
        token_response = await pluto_oauth.exchange_code(body.code)
        userinfo = await pluto_oauth.fetch_userinfo(
            token_response.get("access_token") or "",
        )
    except ValueError as exc:
        raise DependencyError(str(exc)) from exc
    profile = pluto_oauth.extract_user_profile(userinfo)
    if not profile["badge_id"] and profile["wecom_id"]:
        # 工号补全钩子（login-topic §3.3）；补不到 → 拒登
        profile["badge_id"] = await pluto_oauth.lookup_badge_id_by_wecom_id(
            profile["wecom_id"],
        )
    db = Database()
    try:
        try:
            user = console_auth.upsert_console_user(db, profile, userinfo)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        raw_token = console_auth.create_console_session(db, user, token_response)
        roles = console_auth.user_roles(db, user["user_id"])
    finally:
        db.close()
    return {
        "token": raw_token,
        "userId": user["user_id"],
        "displayName": user["display_name"] or user["user_id"],
        "roles": roles,
    }


@router.post("/admin/console/logout")
async def console_logout(request: Request) -> dict:
    db = Database()
    try:
        removed = console_auth.logout(db, console_auth.bearer_token(request))
    finally:
        db.close()
    return {"logged_out": bool(removed)}


@router.post("/admin/auth/login")
async def admin_login(body: AdminLoginIn, request: Request) -> dict:
    db = Database()
    try:
        raw_token = console_auth.admin_login(
            db, body.username, body.password,
            ip=console_auth.client_ip(request),
        )
    finally:
        db.close()
    return {
        "token": raw_token,
        "username": body.username,
        "roles": [console_auth.ROLE_ADMIN],
    }


@router.get("/admin/auth/providers")
async def auth_providers() -> dict:
    """登录方式探测（公开，供登录页一次请求渲染）：
    pluto=Pluto 员工登录是否可用；local=本地账号登录开关。"""
    db = Database()
    try:
        return {
            "pluto": pluto_oauth.pluto_configured(),
            "local_login": console_auth.get_local_login_enabled(db),
        }
    finally:
        db.close()


@router.get("/admin/auth/local-login")
async def read_local_login() -> dict:
    db = Database()
    try:
        return {"enabled": console_auth.get_local_login_enabled(db)}
    finally:
        db.close()


@router.put("/admin/auth/local-login")
async def write_local_login(
    body: LocalLoginIn, request: Request,
) -> dict:
    db = Database()
    try:
        ctx = console_auth.auth_context_from_request(db, request)
        if ctx is None or not ctx.is_admin():
            raise ForbiddenError("仅管理员可修改本地登录开关")
        console_auth.set_local_login_enabled(db, body.enabled)
        return {"enabled": console_auth.get_local_login_enabled(db)}
    finally:
        db.close()
