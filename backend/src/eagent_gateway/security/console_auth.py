# -*- coding: utf-8 -*-
"""管理面双轨会话与统一鉴权（admin-auth 01；login-topic §5/§6）。

两个会话域，同一套形态：
- AdminSession：本地超级管理员（用户名固定 admin，密码读 ADMIN_PASSWORD，
  不入库）；受 `local_login_enabled` 开关控制（存 eag_system_config，默认开；
  dev 环境禁止关闭）。
- ConsoleSession：Pluto 员工登录（badge_id = user_id），保留 pluto
  access/refresh token 供后续调 Pluto 资源。

形态（login-topic §6）：token = secrets.token_hex(32)，明文只返一次，
库内只存 sha256；`Authorization: Bearer <token>` 鉴权，过期即删。
token/密钥类值永不写日志。

统一鉴权依赖 `get_unified_admin_auth`：同一入口接受双轨会话，返回带角色
上下文（对齐 qagent `get_unified_admin_auth`）；无/失效会话 401。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request

from ..config import settings
from ..errors import ForbiddenError, RateLimitedError, UnauthenticatedError
from ..persistence.db import Database, now

ADMIN_USERNAME = "admin"
ROLE_USER = "eagent_user"
ROLE_ADMIN = "eagent_admin"

LOCAL_LOGIN_KEY = "local_login_enabled"

_ADMIN_TOKEN_HEADER = "authorization"


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# local_login_enabled 开关（eag_system_config，默认 "1" 开启）
# ---------------------------------------------------------------------------
def get_local_login_enabled(db: Database) -> bool:
    row = db.query_one(
        "SELECT config_value FROM eag_system_config WHERE config_key=:k",
        {"k": LOCAL_LOGIN_KEY},
    )
    if row is None:
        return True
    return str(row["config_value"]).strip() in ("1", "true", "yes")


def set_local_login_enabled(db: Database, enabled: bool) -> None:
    # dev 环境禁止关闭（外网连不到 Pluto，admin 是唯一入口；login-topic §5）
    if not enabled and settings.auth_mode == "dev":
        raise ForbiddenError("dev 环境不得关闭本地登录（唯一登录入口）")
    db.execute(
        """
        INSERT INTO eag_system_config (config_key, config_value)
        VALUES (:k, :v)
        ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)
        """,
        {"k": LOCAL_LOGIN_KEY, "v": "1" if enabled else "0"},
    )
    db.commit()


# ---------------------------------------------------------------------------
# admin 本地登录
# ---------------------------------------------------------------------------
# 登录失败退避（review P1-3）：进程内滑窗计数——同一来源 10 分钟内失败
# 达 5 次即拒绝到窗口结束。单副本形态够用；多副本需换共享存储（roadmap）。
_FAIL_WINDOW_S = 600.0
_FAIL_LIMIT = 5
_fail_log: dict[str, list[float]] = {}


def _prune_failures(key: str) -> list[float]:
    now_ts = time.monotonic()
    fresh = [t for t in _fail_log.get(key, []) if now_ts - t < _FAIL_WINDOW_S]
    if fresh:
        _fail_log[key] = fresh
    else:
        _fail_log.pop(key, None)
    return fresh


def client_ip(request: Request) -> str:
    """取调用方 IP（经反代时取 X-Forwarded-For 首跳）。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def admin_login(db: Database, username: str, password: str, *, ip: str = "") -> str:
    """本地密码登录 → 明文 token（只此一次）。失败统一 401 不区分原因；
    同一来源连续失败达阈值后 429（防爆破，review P1-3）。"""
    key = ip or "unknown"
    if len(_prune_failures(key)) >= _FAIL_LIMIT:
        raise RateLimitedError("失败次数过多，请稍后再试")
    if not get_local_login_enabled(db):
        raise ForbiddenError("本地登录已关闭，请使用员工账号（Pluto）登录")
    if username != ADMIN_USERNAME or not settings.admin_password:
        _fail_log.setdefault(key, _prune_failures(key)).append(time.monotonic())
        raise UnauthenticatedError("用户名或密码错误")
    if not hmac.compare_digest(password, settings.admin_password):
        _fail_log.setdefault(key, _prune_failures(key)).append(time.monotonic())
        raise UnauthenticatedError("用户名或密码错误")
    _fail_log.pop(key, None)
    return create_admin_session(db, username)


def create_admin_session(db: Database, username: str) -> str:
    raw = secrets.token_hex(32)
    expires = now() + timedelta(hours=settings.admin_session_expire_hours)
    db.execute(
        """
        INSERT INTO eag_admin_session (token_hash, username, expires_at)
        VALUES (:h, :u, :e)
        """,
        {"h": _hash_token(raw), "u": username, "e": expires},
    )
    db.commit()
    return raw


# ---------------------------------------------------------------------------
# Pluto 员工：懒同步 upsert + 会话
# ---------------------------------------------------------------------------
def user_roles(db: Database, user_id: str) -> list[str]:
    return [
        r["role"] for r in db.query_all(
            "SELECT role FROM eag_user_role WHERE user_id=:u", {"u": user_id},
        )
    ]


def upsert_console_user(
    db: Database, profile: dict, raw_profile: dict,
) -> dict:
    """按 pluto_sub 懒同步（login-topic §4.2）：存在刷新 profile，不存在
    创建（user_id=badge_id）并赋默认角色。badge_id 缺失由调用方拒登。"""
    user_id = (profile.get("badge_id") or "").strip()
    if not user_id:
        raise ValueError("badge_id 缺失，拒绝登录（请联系管理员补全工号）")
    import json

    db.execute(
        """
        INSERT INTO eag_console_user
            (user_id, pluto_sub, preferred_username, email, wecom_id,
             display_name, department, raw_pluto_profile,
             profile_sync_status, status)
        VALUES (:uid, :sub, :pun, :email, :wecom,
                :dn, :dept, :raw, 'synced', 'active')
        ON DUPLICATE KEY UPDATE
            pluto_sub=VALUES(pluto_sub),
            preferred_username=VALUES(preferred_username),
            email=VALUES(email),
            wecom_id=VALUES(wecom_id),
            display_name=VALUES(display_name),
            department=VALUES(department),
            raw_pluto_profile=VALUES(raw_pluto_profile),
            profile_sync_status='synced'
        """,
        {
            "uid": user_id,
            "sub": profile.get("pluto_sub") or user_id,
            "pun": profile.get("preferred_username") or "",
            "email": profile.get("email") or "",
            "wecom": profile.get("wecom_id") or "",
            "dn": profile.get("display_name") or "",
            "dept": profile.get("department") or "",
            "raw": json.dumps(raw_profile, ensure_ascii=False),
        },
    )
    if not user_roles(db, user_id):
        db.execute(
            "INSERT INTO eag_user_role (user_id, role) VALUES (:u, :r)",
            {"u": user_id, "r": ROLE_USER},
        )
    db.commit()
    user = db.query_one(
        "SELECT * FROM eag_console_user WHERE user_id=:u", {"u": user_id},
    )
    if user is None:  # 防御：理论上不可能
        raise ValueError("用户同步失败")
    if user["status"] == "disabled":
        raise ForbiddenError("账号已被禁用，请联系管理员")
    touch_last_login(db, user["user_id"])
    return user


def touch_last_login(db: Database, user_id: str) -> None:
    """记录最后成功登录时间（permission-control v1：用户 Tab 展示）。

    展示性字段：失败回滚不阻断登录主链路。"""
    try:
        db.execute(
            "UPDATE eag_console_user SET last_login_at=:t WHERE user_id=:u",
            {"t": now(), "u": user_id},
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def create_console_session(
    db: Database, user: dict, token_response: dict,
) -> str:
    raw = secrets.token_hex(32)
    expires = now() + timedelta(hours=settings.console_session_expire_hours)
    db.execute(
        """
        INSERT INTO eag_console_session
            (token_hash, user_id, pluto_sub, pluto_access_token,
             pluto_refresh_token, expires_at)
        VALUES (:h, :u, :sub, :at, :rt, :e)
        """,
        {
            "h": _hash_token(raw),
            "u": user["user_id"],
            "sub": user["pluto_sub"],
            "at": token_response.get("access_token") or "",
            "rt": token_response.get("refresh_token") or "",
            "e": expires,
        },
    )
    db.commit()
    return raw


# ---------------------------------------------------------------------------
# 统一鉴权（双轨）
# ---------------------------------------------------------------------------
@dataclass
class AuthContext:
    """统一鉴权上下文：cap_user_name（=badge_id/admin）供既有路由/审计使用。"""

    kind: str  # "admin" | "console"
    user_id: str
    display_name: str
    roles: list[str]

    @property
    def cap_user_name(self) -> str:
        return self.user_id

    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles or self.kind == "admin"


def _context_from_admin_row(row: dict) -> AuthContext:
    return AuthContext(
        kind="admin",
        user_id=row["username"],
        display_name="管理员",
        roles=[ROLE_ADMIN],
    )


def _context_from_console_row(db: Database, row: dict) -> AuthContext:
    user = db.query_one(
        "SELECT user_id, display_name, status FROM eag_console_user "
        "WHERE user_id=:u",
        {"u": row["user_id"]},
    )
    if user is None or user["status"] == "disabled":
        # 会话仍在但用户已删/禁用 → 拒绝（下次会话清理或登出收敛）
        raise ForbiddenError("账号已被禁用，请联系管理员")
    return AuthContext(
        kind="console",
        user_id=row["user_id"],
        display_name=user["display_name"] or row["user_id"],
        roles=user_roles(db, row["user_id"]) or [ROLE_USER],
    )


def console_session_from_token(db: Database, raw: str) -> dict | None:
    """按明文 token 取 Pluto 员工会话；过期即删并视为无效。"""
    if not raw:
        return None
    h = _hash_token(raw)
    console = db.query_one(
        "SELECT * FROM eag_console_session WHERE token_hash=:h", {"h": h},
    )
    if console is None:
        return None
    if console["expires_at"] and console["expires_at"] < now():
        db.execute(
            "DELETE FROM eag_console_session WHERE token_hash=:h",
            {"h": h},
        )
        db.commit()
        return None
    return console


def auth_context_from_token(db: Database, raw: str) -> AuthContext | None:
    """按明文 token 查双轨会话；过期即删并视为未认证。"""
    if not raw:
        return None
    h = _hash_token(raw)
    admin = db.query_one(
        "SELECT * FROM eag_admin_session WHERE token_hash=:h", {"h": h},
    )
    if admin is not None:
        if admin["expires_at"] and admin["expires_at"] < now():
            db.execute(
                "DELETE FROM eag_admin_session WHERE token_hash=:h", {"h": h},
            )
            db.commit()
            return None
        return _context_from_admin_row(admin)
    console = console_session_from_token(db, raw)
    if console is not None:
        return _context_from_console_row(db, console)
    return None


def bearer_token(request: Request) -> str:
    auth = request.headers.get(_ADMIN_TOKEN_HEADER, "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def bearer_from_headers(headers: dict[str, str]) -> str:
    """从已规范化的头字典（小写 key）取 Bearer 明文；缺失返回空串。"""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def auth_context_from_request(db: Database, request: Request) -> AuthContext | None:
    """无 Bearer 头时返回 None（不做 DB 查询），由调用方走既有通道。"""
    return auth_context_from_token(db, bearer_token(request))


def logout(db: Database, raw: str) -> bool:
    """按 token_hash 删会话（login-topic §6）；幂等，返回是否确有会话被删。"""
    if not raw:
        return False
    h = _hash_token(raw)
    found = False
    for table in ("eag_admin_session", "eag_console_session"):
        if db.query_one(
            f"SELECT token_hash FROM {table} WHERE token_hash=:h", {"h": h},
        ):
            found = True
            db.execute(f"DELETE FROM {table} WHERE token_hash=:h", {"h": h})
    db.commit()
    return found


async def get_unified_admin_auth(request: Request) -> AuthContext:
    """FastAPI 依赖：Bearer 双轨会话二选一，无/失效 → 401。

    路由层按 AuthContext.roles 放行；既有治理 scope 依赖经
    permission.scopes 桥接（会话角色 → scope）。"""
    db = Database()
    try:
        ctx = auth_context_from_request(db, request)
    finally:
        db.close()
    if ctx is None:
        raise UnauthenticatedError("缺少或无效的登录会话（Authorization: Bearer）")
    return ctx
