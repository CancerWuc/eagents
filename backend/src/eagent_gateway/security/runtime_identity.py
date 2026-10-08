# -*- coding: utf-8 -*-
"""A2A 运行面身份解析。

运行面接受两种身份来源：有效的 eAgent 管理台会话，以及可信入口注入的
``cap-user-name``。外部身份不依赖 eAgent 会话，但必须已登记在
``eag_console_user`` 且处于 active 状态；姓名始终取本地用户目录。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import ForbiddenError
from ..persistence.db import Database
from . import console_auth
from .identity import extract_identity


@dataclass(frozen=True)
class RuntimePrincipal:
    """进入 A2A Executor 后使用的权威、请求级用户身份。"""

    user_id: str
    display_name: str
    source: str  # console_session | trusted_external


class IdentityRequiredError(PermissionError):
    """请求没有任何可用的运行面身份。"""


class SubjectNotAvailableError(PermissionError):
    """外部声明用户未登记或已禁用。"""


def resolve_runtime_principal(
    db: Database, headers: dict[str, str],
) -> RuntimePrincipal:
    """按固定优先级解析 A2A 身份。

    有效 eAgent Session 优先，避免管理台用户用冲突身份头覆盖本人；Bearer
    未命中本地 Session 时不阻断可信外部身份，因为外部系统可能携带自己的
    Authorization。外部 ``real-name`` 不参与身份决策。
    """
    raw = console_auth.bearer_from_headers(headers)
    session = None
    rejected_session = False
    if raw:
        try:
            session = console_auth.auth_context_from_token(db, raw)
        except ForbiddenError:
            # 被禁用的会话不是有效 Session；如同时存在可信外部身份，继续走
            # 用户目录并按当前目录状态判定；没有外部身份时返回统一 403。
            session = None
            rejected_session = True
    if session is not None:
        return RuntimePrincipal(
            user_id=session.user_id,
            display_name=session.display_name or session.user_id,
            source="console_session",
        )

    cap_user, _external_real_name = extract_identity(headers)
    if not cap_user:
        if rejected_session:
            raise SubjectNotAvailableError("当前主体未登记、已禁用或不可用")
        raise IdentityRequiredError("缺少可信用户身份")
    user = db.query_one(
        "SELECT user_id, display_name, status FROM eag_console_user "
        "WHERE user_id=:u",
        {"u": cap_user},
    )
    if user is None or user["status"] != "active":
        raise SubjectNotAvailableError("当前主体未登记、已禁用或不可用")
    return RuntimePrincipal(
        user_id=user["user_id"],
        display_name=user["display_name"] or user["user_id"],
        source="trusted_external",
    )
