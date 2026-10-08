# -*- coding: utf-8 -*-
"""网关状态总览（higress-mcp-gateway 03 T11）。

对单个 profile 汇总：连通性探测（T04）+ McpBridge 线上 registry 数 +
路由/consumer 数 + 最近写入时间。只读；无 K8s 权限由上层 403。
"""
from __future__ import annotations

from ..persistence.db import Database
from . import dao, mcp_service
from .k8s_client import GatewayK8sClient, client_for_profile


def _latest_write_at(db: Database, profile_id: int) -> str | None:
    row = db.query_one(
        """SELECT
             (SELECT MAX(updated_at) FROM eag_gateway_mcp_service
              WHERE profile_id=:id) AS a,
             (SELECT MAX(updated_at) FROM eag_gateway_route
              WHERE profile_id=:id) AS b,
             (SELECT MAX(updated_at) FROM eag_gateway_consumer
              WHERE profile_id=:id) AS c""",
        {"id": profile_id},
    )
    latest = max([t for t in (row["a"], row["b"], row["c"]) if t],
                 default=None)
    return str(latest) if latest else None


def profile_overview(
    db: Database, profile: dict, client: GatewayK8sClient | None = None,
) -> dict:
    if client is None:
        client = client_for_profile(profile)
    connectivity = client.connectivity_check()
    services = dao.list_mcp_services(db, profile["id"])
    routes = dao.list_routes(db, profile["id"])
    consumers = dao.list_consumers(db, profile["id"])
    live_registries: int | None = None
    if connectivity.get("connected"):
        bridge = client.get_mcpbridge(mcp_service.DEFAULT_BRIDGE)
        if bridge is not None:
            live_registries = len(
                (bridge.get("spec") or {}).get("registries") or [],
            )
    _, audit_total = dao.list_audits(db, profile["id"], page=1, page_size=1)
    return {
        "profile": {
            "id": profile["id"],
            "name": profile["name"],
            "higress_ns": profile["higress_ns"],
            "kube_context": profile["kube_context"],
            "higress_version": profile.get("higress_version"),
            "enabled": bool(profile["enabled"]),
        },
        "connectivity": connectivity,
        "counts": {
            "mcpServices": len(services),
            "routes": len(routes),
            "consumers": len(consumers),
            "liveRegistries": live_registries,
            "audits": audit_total,
        },
        "latestWriteAt": _latest_write_at(db, profile["id"]),
    }


def overview_for_all_profiles(db: Database) -> list[dict]:
    return [
        profile_overview(db, p) for p in dao.list_profiles(db)
        if p["enabled"]
    ]
