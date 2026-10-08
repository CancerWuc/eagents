# -*- coding: utf-8 -*-
"""调用目标解析（from_nekiro/02 §6、03 §4.6）。

解析顺序（fail-closed，不 fallback 到注册表最新地址）:
1. binding 显式 pin 的 release_id
2. 工作空间 ENABLED 安装的 release（master 工作空间 → 默认工作空间）
3. 命中的 release 必须是 PUBLISHED，且对应 Installation 是 ENABLED

返回 (target, None) 或 (None, reason)；reason 在真正被调用时映射为
明确错误码（RELEASE_NOT_PUBLISHED / INSTALLATION_DISABLED …）。
"""
from __future__ import annotations

from ..config import settings
from ..errors import (
    ConflictError,
    NotFoundError,
    ServiceDisabledError,
)
from ..persistence.db import Database

RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
RELEASE_NOT_PUBLISHED = "RELEASE_NOT_PUBLISHED"
RELEASE_SUSPENDED = "RELEASE_SUSPENDED"
RELEASE_REVOKED = "RELEASE_REVOKED"
INSTALLATION_DISABLED = "INSTALLATION_DISABLED"
INSTALLATION_UNINSTALLED = "INSTALLATION_UNINSTALLED"
RELEASE_NOT_INSTALLED = "RELEASE_NOT_INSTALLED"


def get_release(db: Database, release_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_agent_release WHERE release_id=:rid", {"rid": release_id},
    )


def workspace_chain(master_id: str | None) -> list[str]:
    """工作空间优先级: master 自己的工作空间 → 全局默认工作空间。"""
    chain = []
    if master_id:
        chain.append(master_id)
    default = settings.default_workspace_id
    if default and default not in chain:
        chain.append(default)
    return chain


def resolve_target(
    db: Database,
    *,
    service_id: str,
    release_id: str | None = None,
    master_id: str | None = None,
    cap_user_name: str | None = None,
) -> tuple[dict | None, str | None]:
    """解析 (release_row, installation_row)；失败返回 (None, reason)。

    target dict: {"release": row, "installation": row|None, "workspace_id": str}

    优先级: 显式 pin > 灰度分组（DEV-11，需 cap_user_name）> 流量策略
    （DEV-10）> 默认工作空间链。
    """
    workspaces = workspace_chain(master_id)

    def _installation_for(release_row: dict) -> tuple[dict | None, str | None]:
        for ws in workspaces:
            inst = db.query_one(
                "SELECT * FROM eag_installation WHERE workspace_id=:ws"
                " AND release_id=:rid ORDER BY status",
                {"ws": ws, "rid": release_row["release_id"]},
            )
            if inst:
                return inst, None
        return None, RELEASE_NOT_INSTALLED

    def _check_release_state(rel: dict) -> str | None:
        if rel["state"] == "DRAFT":
            return RELEASE_NOT_PUBLISHED
        if rel["state"] == "SUSPENDED":
            return RELEASE_SUSPENDED
        if rel["state"] == "REVOKED":
            return RELEASE_REVOKED
        return None

    if release_id:
        rel = get_release(db, release_id)
        if not rel or rel["service_id"] != service_id:
            return None, RELEASE_NOT_FOUND
        reason = _check_release_state(rel)
        if reason:
            return None, reason
        inst, reason = _installation_for(rel)
        if reason:
            # 细分: 曾安装过但被禁用/卸载
            history = db.query_one(
                "SELECT status FROM eag_installation WHERE workspace_id=:ws"
                " AND release_id=:rid",
                {"ws": workspaces[0] if workspaces else "", "rid": release_id},
            )
            if history:
                if history["status"] == "DISABLED":
                    return None, INSTALLATION_DISABLED
                if history["status"] == "UNINSTALLED":
                    return None, INSTALLATION_UNINSTALLED
            return None, reason
        if inst["status"] != "ENABLED":
            return None, INSTALLATION_DISABLED
        return {"release": rel, "installation": inst,
                "workspace_id": inst["workspace_id"]}, None

    # DEV-11: 灰度分组优先于流量策略（白名单/稳定哈希比例分用户）；
    # 选中 release 走下方 pin 分支的同一套校验，灰度用户也拿不到坏版本
    if cap_user_name:
        from . import canary as canary_mod

        running = canary_mod.get_running(db, service_id)
        if running:
            picked = canary_mod.select_release_id(running, cap_user_name)
            return resolve_target(
                db, service_id=service_id, release_id=picked,
                master_id=master_id,
            )

    # DEV-10: 未 pin 时优先看有没有 ACTIVE 流量策略（多版本按比例分流；
    # 策略条目全部不可用时落回默认链——策略绝不让服务不可用）
    from . import traffic as traffic_mod

    policy = traffic_mod.get_policy(db, service_id)
    if policy:
        target, pol_reason = traffic_mod.pick_weighted_target(
            db, policy, master_id=master_id,
        )
        if target is not None:
            return target, None
        state_reason = pol_reason  # 策略失效原因带入默认链失败口径

    # 未 pin: 按工作空间链取 ENABLED 安装
    state_reason: str | None = None  # 安装存在但 Release 不可用时的具体原因
    for ws in workspaces:
        installs = db.query_all(
            "SELECT * FROM eag_installation WHERE workspace_id=:ws"
            " AND service_id=:sid AND status='ENABLED'"
            " ORDER BY created_at DESC",
            {"ws": ws, "sid": service_id},
        )
        for inst in installs:
            rel = get_release(db, inst["release_id"])
            if not rel:
                continue
            reason = _check_release_state(rel)
            if reason:
                state_reason = state_reason or reason
                continue  # 该安装指向的 release 已不可用，继续找其他工作空间
            return {"release": rel, "installation": inst,
                    "workspace_id": ws}, None
    return None, state_reason or RELEASE_NOT_INSTALLED


REASON_ERRORS: dict[str, tuple[str, str]] = {
    # reason -> (errors 类构建器名, 用户可见说明)
    RELEASE_NOT_FOUND: ("NOT_FOUND", "Release 不存在"),
    RELEASE_NOT_PUBLISHED: ("CONFLICT", "Release 未发布，不能调用"),
    RELEASE_SUSPENDED: ("CONFLICT", "Release 已暂停，不能调用"),
    RELEASE_REVOKED: ("CONFLICT", "Release 已撤销，不能调用"),
    INSTALLATION_DISABLED: ("CONFLICT", "安装已禁用，不能调用"),
    INSTALLATION_UNINSTALLED: ("CONFLICT", "安装已卸载，不能调用"),
    RELEASE_NOT_INSTALLED: ("CONFLICT", "服务未安装到工作空间，不能调用"),
}


def reason_to_error(reason: str) -> Exception:
    """把解析失败原因映射为统一错误（真正调用时报出，不静默跳过）。"""
    code, message = REASON_ERRORS.get(
        reason, ("CONFLICT", f"调用目标不可用: {reason}"),
    )
    mapping = {
        "NOT_FOUND": NotFoundError,
        "CONFLICT": ConflictError,
        "SERVICE_DISABLED": ServiceDisabledError,
    }
    cls = mapping.get(code, ConflictError)
    return cls(message)
