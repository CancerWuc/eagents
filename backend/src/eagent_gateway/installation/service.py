# -*- coding: utf-8 -*-
"""Installation 管理（from_nekiro/02，P0-2）。

- 工作空间显式安装某个精确 Release，并记录接受的权限。
- 运行时只调用 ENABLED 安装对应的 PUBLISHED Release。
- 卸载不删历史（UNINSTALLED + uninstalled_at）。
"""
from __future__ import annotations

import json

from ..errors import ConflictError, NotFoundError
from ..persistence.db import Database, new_id, now
from ..release import service as release_svc

INSTALLATION_STATUSES = ("ENABLED", "DISABLED", "UNINSTALLED")


def get_installation(db: Database, installation_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_installation WHERE installation_id=:iid",
        {"iid": installation_id},
    )


def require_installation(db: Database, installation_id: str) -> dict:
    inst = get_installation(db, installation_id)
    if not inst:
        raise NotFoundError("Installation 不存在")
    return inst


def list_installations(db: Database, workspace_id: str) -> list[dict]:
    return db.query_all(
        """SELECT i.*, r.agent_version, r.state AS release_state,
                  s.slug AS service_slug
             FROM eag_installation i
             JOIN eag_agent_release r ON r.release_id = i.release_id
             JOIN eag_a2a_service_registration s ON s.service_id = i.service_id
            WHERE i.workspace_id=:ws ORDER BY i.created_at DESC""",
        {"ws": workspace_id},
    )


def install(
    db: Database,
    *,
    workspace_id: str,
    service_id: str,
    release_id: str,
    accepted_permissions: list[str] | None = None,
) -> dict:
    """安装/重新启用某个 Release（幂等: UNINSTALLED/DISABLED → ENABLED）。"""
    rel = release_svc.require_release(db, release_id)
    if rel["service_id"] != service_id:
        raise ConflictError("Release 不属于该服务")
    if rel["state"] != "PUBLISHED":
        raise ConflictError(
            f"Release 状态为 {rel['state']}，只有 PUBLISHED 的 Release 能安装",
        )
    existing = db.query_one(
        "SELECT * FROM eag_installation WHERE workspace_id=:ws"
        " AND release_id=:rid",
        {"ws": workspace_id, "rid": release_id},
    )
    ts = now()
    if existing:
        if existing["status"] == "UNINSTALLED":
            # 历史卸载记录复用行，重新安装
            db.execute(
                """
                UPDATE eag_installation
                   SET status='ENABLED', accepted_permissions=:perms,
                       uninstalled_at=NULL, updated_at=:ts
                 WHERE installation_id=:iid
                """,
                {"perms": _perms_json(accepted_permissions), "ts": ts,
                 "iid": existing["installation_id"]},
            )
        elif existing["status"] == "DISABLED":
            db.execute(
                """
                UPDATE eag_installation
                   SET status='ENABLED', accepted_permissions=:perms,
                       updated_at=:ts
                 WHERE installation_id=:iid
                """,
                {"perms": _perms_json(accepted_permissions), "ts": ts,
                 "iid": existing["installation_id"]},
            )
        else:
            raise ConflictError("该 Release 已安装且处于启用状态")
        db.commit()
        return require_installation(db, existing["installation_id"])

    installation_id = new_id("ins_")
    db.execute(
        """
        INSERT INTO eag_installation
            (installation_id, workspace_id, service_id, release_id, status,
             accepted_permissions, created_at, updated_at)
        VALUES (:iid, :ws, :sid, :rid, 'ENABLED', :perms, :ts, :ts)
        """,
        {
            "iid": installation_id, "ws": workspace_id, "sid": service_id,
            "rid": release_id, "perms": _perms_json(accepted_permissions),
            "ts": ts,
        },
    )
    db.commit()
    return require_installation(db, installation_id)


def _perms_json(accepted_permissions: list[str] | None) -> str | None:
    if not accepted_permissions:
        return None
    return json.dumps(accepted_permissions, ensure_ascii=False)


def set_status(db: Database, installation_id: str, status: str) -> dict:
    if status not in ("ENABLED", "DISABLED"):
        raise ConflictError("status 仅允许 ENABLED / DISABLED")
    inst = require_installation(db, installation_id)
    if inst["status"] == "UNINSTALLED":
        raise ConflictError("已卸载的安装不能改状态，请重新安装")
    db.execute(
        "UPDATE eag_installation SET status=:st, updated_at=:ts"
        " WHERE installation_id=:iid",
        {"st": status, "ts": now(), "iid": installation_id},
    )
    db.commit()
    return require_installation(db, installation_id)


def uninstall(db: Database, installation_id: str) -> dict:
    """卸载（保留历史行）。"""
    require_installation(db, installation_id)
    db.execute(
        "UPDATE eag_installation SET status='UNINSTALLED', uninstalled_at=:ts,"
        " updated_at=:ts WHERE installation_id=:iid",
        {"ts": now(), "iid": installation_id},
    )
    db.commit()
    return require_installation(db, installation_id)
