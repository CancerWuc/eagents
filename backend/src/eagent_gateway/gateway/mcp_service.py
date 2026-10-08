# -*- coding: utf-8 -*-
"""MCP 服务注册（higress-mcp-gateway 03 T07）。

读：DB 镜像 + McpBridge 线上 registries 反显（共存期可见 console 加的）。
写：McpBridge.spec.registries 追加/移除（registry 名 llm-llm-<slug>.internal），
每步写入后读回复核；DB 行在 K8s 成功后落库，失败回滚 K8s 并记审计。
"""
from __future__ import annotations

from .. import errors
from ..persistence.db import Database
from . import crypto, dao, resources
from .k8s_client import GatewayK8sClient

DEFAULT_BRIDGE = "default"


def _ensure_bridge(client: GatewayK8sClient) -> dict:
    """取 default McpBridge；缺失则建空实例（受管对象，01 §4）。"""
    bridge = client.get_mcpbridge(DEFAULT_BRIDGE)
    if bridge is None:
        bridge = {
            "apiVersion": "networking.higress.io/v1",
            "kind": "McpBridge",
            "metadata": {"name": DEFAULT_BRIDGE, "namespace": client.ns},
            "spec": {"registries": []},
        }
        client.replace_mcpbridge(bridge)
    return bridge


def _service_out(svc: dict) -> dict:
    return {
        "id": svc["id"],
        "profile_id": svc["profile_id"],
        "name": svc["name"],
        "slug": dao.slugify(svc["name"]),
        "domain": svc["domain"],
        "port": svc["port"],
        "protocol": svc["protocol"],
        "registry_type": svc["registry_type"],
        "registry_name": dao.registry_name(dao.slugify(svc["name"])),
        "provider_config": dao.jload(svc.get("provider_config")),
        "status": svc["status"],
        "created_by": svc["created_by"],
        "created_at": str(svc["created_at"]),
        "updated_at": str(svc["updated_at"]),
    }


def list_services(
    db: Database, profile: dict, client: GatewayK8sClient | None = None,
) -> dict:
    rows = [_service_out(s) for s in dao.list_mcp_services(db, profile["id"])]
    live: list[dict] | None = None
    if client is not None:
        bridge = client.get_mcpbridge(DEFAULT_BRIDGE)
        if bridge is not None:
            live = (bridge.get("spec") or {}).get("registries") or []
    return {"services": rows, "liveRegistries": live}


def create_service(
    db: Database, profile: dict, *, name: str, domain: str, port: int = 443,
    protocol: str = "https", registry_type: str = "dns",
    provider_config: dict | None = None, operator: str,
    client: GatewayK8sClient | None = None,
) -> dict:
    """注册单个 MCP 上游（仅 McpBridge registry；四步一键注册走 sync.py）。"""
    if client is None:
        from .k8s_client import client_for_profile
        client = client_for_profile(profile)
    dao.slugify(name)  # 校验可生成合法 slug
    if dao.get_mcp_service_by_name(db, profile["id"], name):
        raise errors.ConflictError(f"MCP 服务名已存在: {name}")
    entry = resources.build_registry_entry({
        "name": name, "domain": domain, "port": port,
        "protocol": protocol, "registry_type": registry_type,
    })
    registry = entry["name"]

    wrote_bridge = False
    try:
        bridge = _ensure_bridge(client)
        resources.upsert_registry(bridge, entry)
        client.replace_mcpbridge(bridge)
        wrote_bridge = True
        after = client.get_mcpbridge(DEFAULT_BRIDGE)
        if after is None or resources.find_registry(after, registry) is None:
            raise errors.DependencyError("McpBridge 写入后复核失败")
    except errors.EAgentError as exc:
        if wrote_bridge:
            try:
                bridge = client.get_mcpbridge(DEFAULT_BRIDGE)
                if bridge is not None:
                    client.replace_mcpbridge(
                        resources.remove_registry(bridge, registry),
                    )
            except Exception:  # noqa: BLE001
                pass
        dao.record_audit(
            db, profile_id=profile["id"], resource_type="mcpbridge",
            action="create", payload_before=None,
            payload_after={"registry": entry, "rolledBack": wrote_bridge},
            result="failed", operator=operator,
        )
        raise exc
    except Exception as exc:  # noqa: BLE001
        dao.record_audit(
            db, profile_id=profile["id"], resource_type="mcpbridge",
            action="create", payload_before=None,
            payload_after={"registry": entry}, result="failed",
            operator=operator,
        )
        raise errors.DependencyError("McpBridge 写入失败") from exc

    svc = dao.create_mcp_service(
        db, profile_id=profile["id"], name=name, domain=domain, port=port,
        protocol=protocol, registry_type=registry_type,
        provider_config=provider_config, created_by=operator,
    )
    dao.record_audit(
        db, profile_id=profile["id"], resource_type="mcpbridge",
        action="create", payload_before=None,
        payload_after={"service": _service_out(svc), "registry": entry},
        result="success", operator=operator,
    )
    return _service_out(svc)


def delete_service(
    db: Database, profile: dict, name: str, *, operator: str,
    client: GatewayK8sClient | None = None,
) -> dict:
    """移除 MCP 服务：先确认无路由引用（有则 409），再摘 registry 与 provider。"""
    if client is None:
        from .k8s_client import client_for_profile
        client = client_for_profile(profile)
    svc = dao.get_mcp_service_by_name(db, profile["id"], name)
    if not svc:
        raise errors.NotFoundError(f"MCP 服务不存在: {name}")
    refs = dao.routes_referencing_mcp(db, svc["id"])
    if refs:
        raise errors.ConflictError(
            f"仍有 {len(refs)} 条路由引用该服务"
            f"（{', '.join(r['name'] for r in refs[:5])}），先删除路由",
        )
    slug = dao.slugify(name)
    registry = dao.registry_name(slug)
    before = _service_out(svc)

    bridge = client.get_mcpbridge(DEFAULT_BRIDGE)
    if bridge is not None and resources.find_registry(bridge, registry):
        client.replace_mcpbridge(resources.remove_registry(bridge, registry))
    # ai-proxy provider/matchRule 一并清理（若曾配置）
    provider_cfg = dao.jload(svc.get("provider_config"))
    if provider_cfg:
        wasm = client.get_wasmplugin("ai-proxy.internal")
        if wasm is not None:
            client.replace_wasmplugin(resources.remove_provider(wasm, slug))

    dao.delete_mcp_service(db, svc["id"])
    dao.record_audit(
        db, profile_id=profile["id"], resource_type="mcpbridge",
        action="delete", payload_before=before,
        payload_after={"removed": registry}, result="success",
        operator=operator,
    )
    return {"deleted": name, "registry": registry}
