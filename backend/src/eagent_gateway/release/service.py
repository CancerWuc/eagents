# -*- coding: utf-8 -*-
"""Agent Release 管理（from_nekiro/02，P0-2）。

- Release 一次创建即不可变（内容字段不允许更新），只有状态流转:
  DRAFT → PUBLISHED ⇄ SUSPENDED，任意 → REVOKED（终态）。
- publish 前置校验: Card Revision COMPATIBLE 且服务 ENABLED。
- 不删除历史; 撤销后不再允许新安装/调用。
"""
from __future__ import annotations

from ..a2a_server.protocol import endpoint as card_endpoint

from ..catalog import service as catalog
from ..errors import ConflictError, NotFoundError, ValidationError
from ..persistence.db import Database, new_id, now

RELEASE_STATES = ("DRAFT", "PUBLISHED", "SUSPENDED", "REVOKED")


def get_release(db: Database, release_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_agent_release WHERE release_id=:rid", {"rid": release_id},
    )


def require_release(db: Database, release_id: str) -> dict:
    rel = get_release(db, release_id)
    if not rel:
        raise NotFoundError("Release 不存在")
    return rel


def list_releases(db: Database, service_id: str) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_agent_release WHERE service_id=:sid"
        " ORDER BY created_at DESC",
        {"sid": service_id},
    )


def create_release(
    db: Database,
    *,
    service_id: str,
    agent_version: str | None = None,
    created_by: str = "admin",
) -> dict:
    """基于服务当前 Card Revision 创建 DRAFT Release（from_nekiro/02 §6.1）。"""
    svc = catalog.get_service(db, service_id)
    if not svc:
        raise NotFoundError("服务不存在")
    card = catalog.current_card(db, service_id)
    if not card:
        raise ConflictError("服务没有可用的 Card Revision，无法创建 Release")
    version = (agent_version or card["card"].get("version") or "").strip()
    if not version:
        raise ValidationError("Card 未提供 version，需显式指定 agentVersion")
    if db.query_one(
        "SELECT 1 AS x FROM eag_agent_release WHERE service_id=:sid"
        " AND agent_version=:v",
        {"sid": service_id, "v": version},
    ):
        raise ConflictError(f"该服务已存在版本 {version} 的 Release")
    release_id = new_id("rel_")
    ts = now()
    db.execute(
        """
        INSERT INTO eag_agent_release
            (release_id, service_id, agent_version, card_revision_id,
             card_digest, endpoint, state, created_by, created_at)
        VALUES (:rid, :sid, :v, :crid, :dg, :ep, 'DRAFT', :u, :ts)
        """,
        {
            "rid": release_id, "sid": service_id, "v": version,
            "crid": svc["current_revision_id"], "dg": card["content_digest"],
            "ep": card_endpoint(card["card"]),
            "u": created_by, "ts": ts,
        },
    )
    db.commit()
    return require_release(db, release_id)


def _transition(
    db: Database, release_id: str, *, target: str,
    allowed_from: tuple[str, ...], timestamp_column: str,
) -> dict:
    rel = require_release(db, release_id)
    if rel["state"] not in allowed_from:
        raise ConflictError(
            f"Release 状态为 {rel['state']}，不允许流转到 {target}",
        )
    db.execute(
        f"UPDATE eag_agent_release SET state=:st, {timestamp_column}=:ts"
        " WHERE release_id=:rid",
        {"st": target, "ts": now(), "rid": release_id},
    )
    db.commit()
    return require_release(db, release_id)


def publish_release(db: Database, release_id: str) -> dict:
    """DRAFT/SUSPENDED → PUBLISHED; 前置校验兼容性与服务状态（§6.2）。"""
    rel = require_release(db, release_id)
    if rel["state"] in ("DRAFT", "SUSPENDED"):
        svc = catalog.get_service(db, rel["service_id"])
        if not svc or not svc["enabled"]:
            raise ConflictError("服务不存在或已禁用，不能发布 Release")
        revision = db.query_one(
            "SELECT compatibility FROM eag_a2a_card_revision"
            " WHERE revision_id=:rid",
            {"rid": rel["card_revision_id"]},
        )
        if not revision or revision["compatibility"] != "COMPATIBLE":
            raise ConflictError(
                "Release 对应的 Card Revision 不是 COMPATIBLE，不能发布",
            )
    return _transition(
        db, release_id, target="PUBLISHED",
        allowed_from=("DRAFT", "SUSPENDED"), timestamp_column="published_at",
    )


def suspend_release(db: Database, release_id: str) -> dict:
    return _transition(
        db, release_id, target="SUSPENDED",
        allowed_from=("PUBLISHED",), timestamp_column="suspended_at",
    )


def revoke_release(db: Database, release_id: str) -> dict:
    """撤销为终态，保留历史，不再允许新安装/调用。"""
    return _transition(
        db, release_id, target="REVOKED",
        allowed_from=("DRAFT", "PUBLISHED", "SUSPENDED"),
        timestamp_column="revoked_at",
    )
