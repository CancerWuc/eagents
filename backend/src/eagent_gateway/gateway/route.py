# -*- coding: utf-8 -*-
"""ai-route 路由管理（higress-mcp-gateway 03 T08）。

- 生成/更新/删除 ai-route-route-<slug> ConfigMap，payload 严格照 02 §3
  （pathPredicate/upstreams/authConfig/proxyNextUpstream）。
- allowedConsumers 由绑定了该路由的 consumer 集合推导（consumer.route_ids）。
- consumer 变更（绑定/解绑/删除/轮换）后调用 refresh_route_cm 同步重写。
"""
from __future__ import annotations

from .. import errors
from ..persistence.db import Database
from . import dao, resources
from .k8s_client import GatewayK8sClient


def _route_out(db: Database, r: dict) -> dict:
    upstream = dao.get_mcp_service(db, r["upstream_mcp"])
    consumers = [
        c for c in dao.list_consumers(db, r["profile_id"])
        if r["id"] in (dao.jload(c.get("route_ids")) or [])
    ]
    return {
        "id": r["id"],
        "profile_id": r["profile_id"],
        "name": r["name"],
        "slug": dao.slugify(r["name"]),
        "match_type": r["match_type"],
        "match_value": r["match_value"],
        "upstream_mcp": r["upstream_mcp"],
        "upstream_name": upstream["name"] if upstream else None,
        "weight": r["weight"],
        "auth_enabled": bool(r["auth_enabled"]),
        "auth_type": r["auth_type"],
        "retry_enabled": bool(r["retry_enabled"]),
        "retry_attempts": r["retry_attempts"],
        "status": r["status"],
        "consumers": [c["name"] for c in consumers],
        "cm_name": dao.route_cm_name(dao.slugify(r["name"])),
        "created_by": r["created_by"],
        "created_at": str(r["created_at"]),
        "updated_at": str(r["updated_at"]),
    }


def allowed_consumers(db: Database, profile_id: int, route_id: int) -> list[str]:
    return [
        c["name"] for c in dao.list_consumers(db, profile_id)
        if route_id in (dao.jload(c.get("route_ids")) or [])
    ]


def write_route_cm(
    db: Database, profile: dict, route: dict, *, operator: str,
    client: GatewayK8sClient,
) -> dict:
    """写路由三件套（console 翻译链的完整复刻，02 基准 §3/§3a）：
    ai-route ConfigMap（数据模型）+ 派生 Ingress（controller 消费）+
    key-auth matchRule（按 ingress 授权 consumer）。"""
    slug = dao.slugify(route["name"])
    upstream = dao.get_mcp_service(db, route["upstream_mcp"])
    if not upstream:
        raise errors.ValidationError("路由引用的 MCP 服务不存在")
    consumers = allowed_consumers(db, profile["id"], route["id"])
    payload = resources.build_ai_route_payload(
        route,
        provider=dao.provider_id(dao.slugify(upstream["name"])),
        allowed_consumers=consumers,
    )
    # 1) ai-route ConfigMap
    cm = resources.ai_route_cm(slug, payload)
    client.upsert_configmap(cm["name"], cm["labels"], cm["data"])
    after = client.get_configmap(cm["name"])
    if after is None:
        raise errors.DependencyError("ai-route ConfigMap 写入后复核失败")
    # 2) 派生 Ingress（路由真正生效的载体）
    ingress = resources.build_route_ingress(
        route, upstream_slug=dao.slugify(upstream["name"]),
        upstream_port=upstream["port"],
    )
    client.upsert_ingress(ingress)
    # 3) key-auth matchRule（allowedConsumers 授权）
    rule = resources.build_key_auth_route_rule(
        ingress["metadata"]["name"],
        consumers if route["auth_enabled"] else None,
    )
    wasm = client.get_wasmplugin("key-auth.internal")
    if wasm is not None:
        client.replace_wasmplugin(
            resources.upsert_key_auth_route_rule(
                wasm, ingress["metadata"]["name"], rule,
            ),
        )
    return resources.parse_ai_route_payload(
        after.data if hasattr(after, "data") else (after.get("data") or {}),
    ) or {}


def refresh_route_cm(
    db: Database, profile: dict, route: dict, *,
    client: GatewayK8sClient,
) -> None:
    """consumer 变更后重写路由 CM（无需审计：调用方记 consumer 维度审计）。"""
    write_route_cm(db, profile, route, operator="system", client=client)


def list_routes(
    db: Database, profile: dict, client: GatewayK8sClient | None = None,
) -> dict:
    rows = [_route_out(db, r) for r in dao.list_routes(db, profile["id"])]
    live: list[str] | None = None
    if client is not None:
        # 只读反显线上 ai-route CM 名单（不逐个拉取，开销一次 list）
        from .k8s_client import AI_ROUTE_LABEL
        try:
            items = client.list_configmaps_by_label(
                AI_ROUTE_LABEL, "ai-route",
            )
            live = items
        except Exception:  # noqa: BLE001
            live = None
    return {"routes": rows, "liveRouteCms": live}


def create_route(
    db: Database, profile: dict, *, name: str, match_type: str,
    match_value: str, upstream_name: str, weight: int = 100,
    auth_enabled: bool = True, auth_type: str = "key-auth",
    retry_enabled: bool = True, retry_attempts: int = 3,
    consumer_names: list[str] | None = None, operator: str,
    client: GatewayK8sClient | None = None,
) -> dict:
    if client is None:
        from .k8s_client import client_for_profile
        client = client_for_profile(profile)
    if match_type not in dao.MATCH_TYPES:
        raise errors.ValidationError(
            f"matchType 必须为 {list(dao.MATCH_TYPES)}",
        )
    if dao.get_route_by_name(db, profile["id"], name):
        raise errors.ConflictError(f"路由名已存在: {name}")
    upstream = dao.get_mcp_service_by_name(db, profile["id"], upstream_name)
    if not upstream:
        raise errors.NotFoundError(f"MCP 服务不存在: {upstream_name}")

    consumers: list[dict] = []
    for cn in consumer_names or []:
        c = dao.get_consumer_by_name(db, profile["id"], cn)
        if not c:
            raise errors.NotFoundError(f"consumer 不存在: {cn}")
        consumers.append(c)

    route = dao.create_route(
        db, profile_id=profile["id"], name=name, match_type=match_type,
        match_value=match_value, upstream_mcp=upstream["id"], weight=weight,
        auth_enabled=auth_enabled, auth_type=auth_type,
        retry_enabled=retry_enabled, retry_attempts=retry_attempts,
        created_by=operator,
    )
    # 绑定 consumer（route_ids）后再生成 CM（allowedConsumers 需就位）
    for c in consumers:
        rids = dao.jload(c.get("route_ids")) or []
        dao.update_consumer(db, c["id"], route_ids=[*rids, route["id"]])

    try:
        wrote = write_route_cm(
            db, profile, route, operator=operator, client=client,
        )
    except errors.EAgentError:
        # 回滚：解绑 consumer + 删路由行，保持库/集群一致
        for c in consumers:
            rids = dao.jload(c.get("route_ids")) or []
            dao.update_consumer(
                db, c["id"],
                route_ids=[i for i in rids if i != route["id"]],
            )
        dao.delete_route(db, route["id"])
        dao.record_audit(
            db, profile_id=profile["id"], resource_type="route",
            action="create", payload_before=None,
            payload_after={"name": name}, result="failed", operator=operator,
        )
        raise

    dao.record_audit(
        db, profile_id=profile["id"], resource_type="route",
        action="create", payload_before=None,
        payload_after={"route": _route_out(db, route), "cm": wrote},
        result="success", operator=operator,
    )
    return _route_out(db, dao.get_route(db, route["id"]))


def update_route(
    db: Database, profile: dict, name: str, *, match_type: str | None = None,
    match_value: str | None = None, upstream_name: str | None = None,
    weight: int | None = None, auth_enabled: bool | None = None,
    auth_type: str | None = None, retry_enabled: bool | None = None,
    retry_attempts: int | None = None, operator: str,
    client: GatewayK8sClient | None = None,
) -> dict:
    if client is None:
        from .k8s_client import client_for_profile
        client = client_for_profile(profile)
    route = dao.get_route_by_name(db, profile["id"], name)
    if not route:
        raise errors.NotFoundError(f"路由不存在: {name}")
    before = _route_out(db, route)
    upstream_mcp = route["upstream_mcp"]
    if upstream_name is not None:
        upstream = dao.get_mcp_service_by_name(db, profile["id"], upstream_name)
        if not upstream:
            raise errors.NotFoundError(f"MCP 服务不存在: {upstream_name}")
        upstream_mcp = upstream["id"]
    if match_type is not None and match_type not in dao.MATCH_TYPES:
        raise errors.ValidationError(
            f"matchType 必须为 {list(dao.MATCH_TYPES)}",
        )
    updated = dao.update_route(
        db, route["id"], match_type=match_type, match_value=match_value,
        upstream_mcp=upstream_mcp, weight=weight, auth_enabled=auth_enabled,
        auth_type=auth_type, retry_enabled=retry_enabled,
        retry_attempts=retry_attempts,
    )
    try:
        wrote = write_route_cm(
            db, profile, updated, operator=operator, client=client,
        )
    except errors.EAgentError:
        dao.update_route(  # 写失败回滚 DB 行到旧值
            db, route["id"], match_type=before["match_type"],
            match_value=before["match_value"],
            upstream_mcp=before["upstream_mcp"], weight=before["weight"],
            auth_enabled=before["auth_enabled"], auth_type=before["auth_type"],
            retry_enabled=before["retry_enabled"],
            retry_attempts=before["retry_attempts"],
        )
        dao.record_audit(
            db, profile_id=profile["id"], resource_type="route",
            action="update", payload_before=before,
            payload_after={"name": name}, result="failed", operator=operator,
        )
        raise
    dao.record_audit(
        db, profile_id=profile["id"], resource_type="route",
        action="update", payload_before=before,
        payload_after={"route": _route_out(db, updated), "cm": wrote},
        result="success", operator=operator,
    )
    return _route_out(db, dao.get_route(db, route["id"]))


def delete_route(
    db: Database, profile: dict, name: str, *, operator: str,
    client: GatewayK8sClient | None = None,
) -> dict:
    if client is None:
        from .k8s_client import client_for_profile
        client = client_for_profile(profile)
    route = dao.get_route_by_name(db, profile["id"], name)
    if not route:
        raise errors.NotFoundError(f"路由不存在: {name}")
    before = _route_out(db, route)
    slug = dao.slugify(name)
    cm_name = dao.route_cm_name(slug)
    ingress_name = resources.route_ingress_name(slug)
    # 删除顺序与创建相反：先摘 key-auth matchRule → 删 Ingress → 删 CM
    wasm = client.get_wasmplugin("key-auth.internal")
    if wasm is not None:
        client.replace_wasmplugin(
            resources.remove_key_auth_route_rule(wasm, ingress_name),
        )
    client.delete_ingress(ingress_name)
    client.delete_configmap(cm_name)
    # 解绑 consumer 的 route_ids
    for c in dao.list_consumers(db, profile["id"]):
        rids = dao.jload(c.get("route_ids")) or []
        if route["id"] in rids:
            dao.update_consumer(
                db, c["id"],
                route_ids=[i for i in rids if i != route["id"]],
            )
    dao.delete_route(db, route["id"])
    dao.record_audit(
        db, profile_id=profile["id"], resource_type="route",
        action="delete", payload_before=before,
        payload_after={"removed": cm_name, "ingress": ingress_name},
        result="success", operator=operator,
    )
    return {
        "deleted": name, "configmap": cm_name, "ingress": ingress_name,
    }


def bind_consumers(
    db: Database, profile: dict, route: dict, names: list[str], *,
    client: GatewayK8sClient,
) -> None:
    """把路由的 consumer 绑定集合整体替换为 names，并重写 CM。"""
    for cn in names:
        if not dao.get_consumer_by_name(db, profile["id"], cn):
            raise errors.NotFoundError(f"consumer 不存在: {cn}")
    for c in dao.list_consumers(db, profile["id"]):
        rids = dao.jload(c.get("route_ids")) or []
        should = c["name"] in names
        has = route["id"] in rids
        if should != has:
            new_rids = ([*rids, route["id"]] if should
                        else [i for i in rids if i != route["id"]])
            dao.update_consumer(db, c["id"], route_ids=new_rids)
    refresh_route_cm(db, profile, route, client=client)
