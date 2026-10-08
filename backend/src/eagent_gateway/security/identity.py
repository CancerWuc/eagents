# -*- coding: utf-8 -*-
"""a2a 身份头（platform-spec/a2a-topic/01 §3）。

本模块是通用头编解码工具，允许 `cap-user-name`（工号）与 `real-name`
（真实姓名）缺失；是否拒绝由调用边界决定。A2A 运行面要求有效 Session
或可信 `cap-user-name` 至少一种，管理面与其他内部路径保留各自规则。

canonical 写法是连字符；服务端兼容下划线（`cap_user_name` / `real_name`），
但任何出站注入一律发连字符，且无身份时不注入空头、不伪造。身份头的可信
来源是可信入口注入；A2A 直连不直接信任客户端自报身份。

身份头、token、密钥不入日志；本模块只做头提取，不做任何记录。
"""
from __future__ import annotations

CAP_USER_HEADER = "cap-user-name"
REAL_NAME_HEADER = "real-name"

# 服务端兼容的下划线写法（提取时逐个尝试；canonical 排在前）
_CAP_USER_KEYS = (CAP_USER_HEADER, "cap_user_name")
_REAL_NAME_KEYS = (REAL_NAME_HEADER, "real_name")


def outbound_header_value(value: str) -> str | bytes:
    """生成可由 httpx 原样发送的身份 Header 值。

    httpx 对 ``str`` Header 强制 ASCII 编码；目录姓名允许中文，因此非
    ASCII 值改用 UTF-8 bytes。ASCII 工号和姓名保持字符串以兼容既有调用。
    """
    return value if value.isascii() else value.encode("utf-8")


def extract_identity(headers) -> tuple[str, str | None]:  # noqa: ANN001
    """按规范口径提取用户身份，返回 (cap_user_name, real_name)。

    headers 为任意支持 .get(key, "") 的映射（Starlette Headers 大小写
    不敏感、publisher 提取后的小写 dict 均适用）。两者均可缺失：缺失时
    分别返回 "" 与 None，由具体调用边界决定拒绝或兼容处理。
    """
    cap_user = ""
    for key in _CAP_USER_KEYS:
        value = str(headers.get(key) or "").strip()
        if value:
            cap_user = value
            break
    real_name = None
    for key in _REAL_NAME_KEYS:
        value = str(headers.get(key) or "").strip()
        if value:
            real_name = value
            break
    return cap_user, real_name
