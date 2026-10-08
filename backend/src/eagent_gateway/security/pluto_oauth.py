# -*- coding: utf-8 -*-
"""Pluto OAuth2 客户端（admin-auth 01；login-topic §3，蓝本 qagent_manager）。

授权码模式全流程：authorize URL / code 换 token / introspect / userinfo /
refresh。与蓝本的差异：
- 异步 httpx（eagent 全栈 async）+ 显式超时（PLUTO_TIMEOUT_S，默认 15s）；
- 失败日志只记状态码，**不记响应体**——token 端点响应里就是令牌（安全红线）；
- 工号（badge_id）兜底链末端补全做可插拔钩子：eagent 库为独立库，无 cap
  跨库映射（蓝本直查 `cap`.`user_wechat_mapping`）；钩子默认空实现，取不到
  工号由调用方拒登（400），宁可拒登也不落库（login-topic §3.3）。

本模块只做协议交互与字段提取，不做任何会话/落库操作。
"""
from __future__ import annotations

import logging
import secrets
import urllib.parse

import httpx

from ..config import settings

logger = logging.getLogger("eagent.pluto")

# userinfo 目标字段的兜底链（login-topic §3.3：Pluto 字段名不稳定）
_SUB_KEYS = ("sub", "user_id", "id")
_BADGE_KEYS = ("badgeId", "badge_id", "username", "preferred_username")
_WECOM_KEYS = ("wecom_id", "wecomId")
_DISPLAY_KEYS = ("name", "display_name", "realName")
_PREFERRED_KEYS = ("preferred_username", "username")


def generate_state() -> str:
    """防 CSRF 的 state（login-topic §3.2：token_hex(16)）。"""
    return secrets.token_hex(16)


def _first(userinfo: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = userinfo.get(key)
        if value:
            return str(value)
    return ""


def pluto_configured() -> bool:
    """Pluto 四要素齐备才可发起员工登录；缺失时前端只给 admin 登录。"""
    return bool(
        settings.pluto_access_key and settings.pluto_access_secret
        and settings.pluto_authorize_url and settings.pluto_token_url
        and settings.pluto_userinfo_url and settings.pluto_redirect_uri
    )


def build_authorize_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.pluto_access_key,
        "redirect_uri": settings.pluto_redirect_uri,
        "scope": "openid",
        "state": state,
    }
    return f"{settings.pluto_authorize_url}?{urllib.parse.urlencode(params)}"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.pluto_timeout_s,
        follow_redirects=False,
    )


async def exchange_code(code: str) -> dict:
    """code → {access_token, refresh_token, ...}（表单编码，login-topic §3.2）。"""
    async with _client() as client:
        resp = await client.post(
            settings.pluto_token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.pluto_redirect_uri,
                "client_id": settings.pluto_access_key,
                "client_secret": settings.pluto_access_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        # 只记状态码：token 响应体含令牌，不入日志
        logger.error("Pluto token exchange failed: status=%s", resp.status_code)
        raise ValueError(f"Pluto token exchange failed: HTTP {resp.status_code}")
    return resp.json()


async def introspect_token(access_token: str) -> dict:
    async with _client() as client:
        resp = await client.post(
            settings.pluto_introspect_url,
            data={"token": access_token},
            auth=(settings.pluto_access_key, settings.pluto_access_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        logger.error("Pluto introspect failed: status=%s", resp.status_code)
        raise ValueError(f"Pluto introspect failed: HTTP {resp.status_code}")
    return resp.json()


async def fetch_userinfo(access_token: str) -> dict:
    async with _client() as client:
        resp = await client.get(
            settings.pluto_userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        logger.error("Pluto userinfo failed: status=%s", resp.status_code)
        raise ValueError(f"Pluto userinfo failed: HTTP {resp.status_code}")
    return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    async with _client() as client:
        resp = await client.post(
            settings.pluto_token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.pluto_access_key,
                "client_secret": settings.pluto_access_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        logger.error("Pluto refresh failed: status=%s", resp.status_code)
        raise ValueError(f"Pluto refresh failed: HTTP {resp.status_code}")
    return resp.json()


def extract_pluto_sub(userinfo: dict) -> str:
    return _first(userinfo, _SUB_KEYS)


def extract_user_profile(userinfo: dict) -> dict:
    """多字段兼容提取（login-topic §3.3）。

    badge_id（工号）**不兜底到 pluto_sub**（review P1-1）：user_id=badge_id
    的可归责语义要求工号必须真实；取不到时 badge_id 为空串，由调用方走
    CAP 补全钩子，仍取不到 → 400 拒登（宁可拒登也不落库）。"""
    return {
        "pluto_sub": extract_pluto_sub(userinfo),
        "preferred_username": _first(userinfo, _PREFERRED_KEYS),
        "email": str(userinfo.get("email") or ""),
        "wecom_id": _first(userinfo, _WECOM_KEYS),
        "display_name": _first(userinfo, _DISPLAY_KEYS),
        "department": str(userinfo.get("department") or ""),
        "badge_id": _first(userinfo, _BADGE_KEYS),
    }


async def lookup_badge_id_by_wecom_id(wecom_id: str) -> str:
    """工号补全钩子（蓝本经 CAP 跨库映射表；eagent 独立库默认无此映射）。

    返回 "" 表示无法补全，调用方拒登。后续如接入公司 CAP 数据源
    （HTTP 或映射表），在此实现并保持签名不变。"""
    return ""
