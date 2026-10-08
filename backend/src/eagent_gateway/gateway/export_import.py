# -*- coding: utf-8 -*-
"""配置导出/导入（higress-mcp-gateway 03 T15，跨环境迁移场景）。

- 导出：profile 全量配置 JSON；consumer 密钥永不导出（库内为密文且换环境
  应重新生成）；provider apiTokens 默认脱敏（***），include_secrets=true
  显式携带（管理员迁移动作）。
- 导入：校验引用完整性（路由上游、consumer 绑定都指向 payload 内对象）
  后按四步写序列执行；consumer 一律生成新密钥并在响应中一次性回显。
"""
from __future__ import annotations

from .. import errors
from ..persistence.db import Database
from . import dao, sync

EXPORT_VERSION = 1
_REDACTED = "***"


def export_profile(
    db: Database, profile: dict, *, include_secrets: bool = False,
) -> dict:
    services = []
    for s in dao.list_mcp_services(db, profile["id"]):
        pcfg = dao.jload(s.get("provider_config"))
        if pcfg and not include_secrets:
            pcfg = {**pcfg, "apiTokens": [_REDACTED] * len(
                pcfg.get("apiTokens") or [],
            )}
        services.append({
            "name": s["name"], "domain": s["domain"], "port": s["port"],
            "protocol": s["protocol"], "registryType": s["registry_type"],
            "providerConfig": pcfg,
        })
    routes = []
    for r in dao.list_routes(db, profile["id"]):
        routes.append({
            "name": r["name"], "matchType": r["match_type"],
            "matchValue": r["match_value"],
            "upstreamName": (dao.get_mcp_service(db, r["upstream_mcp"])
                             or {}).get("name"),
            "weight": r["weight"], "authEnabled": bool(r["auth_enabled"]),
            "retryEnabled": bool(r["retry_enabled"]),
            "retryAttempts": r["retry_attempts"],
        })
    consumers = []
    for c in dao.list_consumers(db, profile["id"]):
        rids = dao.jload(c.get("route_ids")) or []
        consumers.append({
            "name": c["name"],
            "routes": [
                rn for rn in (
                    (dao.get_route(db, rid) or {}).get("name")
                    for rid in rids
                ) if rn
            ],
        })
    return {
        "exportVersion": EXPORT_VERSION,
        "kind": "eagent-gateway-config",
        "source": {
            "name": profile["name"],
            "higressNs": profile["higress_ns"],
            "higressVersion": profile.get("higress_version"),
        },
        "secretsIncluded": bool(include_secrets),
        "mcpServices": services,
        "routes": routes,
        "consumers": consumers,
    }


def _validate_payload(payload: dict) -> None:
    if not isinstance(payload, dict) or payload.get("kind") \
            != "eagent-gateway-config":
        raise errors.ValidationError(
            "导入文件不是 eagent 网关配置导出（kind 不符）",
        )
    services = payload.get("mcpServices") or []
    routes = payload.get("routes") or []
    consumers = payload.get("consumers") or []
    svc_names = {s["name"] for s in services if isinstance(s, dict)}
    route_names = {r["name"] for r in routes if isinstance(r, dict)}
    for r in routes:
        if not isinstance(r, dict) or not r.get("name") \
                or not r.get("upstreamName"):
            raise errors.ValidationError("路由缺少 name/upstreamName")
        if r["upstreamName"] not in svc_names:
            raise errors.ValidationError(
                f"路由 {r['name']} 的上游 {r['upstreamName']} 不在"
                " mcpServices 中（引用不完整）",
            )
    for c in consumers:
        if not isinstance(c, dict) or not c.get("name"):
            raise errors.ValidationError("consumer 缺少 name")
        for rn in c.get("routes") or []:
            if rn not in route_names:
                raise errors.ValidationError(
                    f"consumer {c['name']} 绑定的路由 {rn} 不在 routes 中"
                    "（引用不完整）",
                )


def import_profile(
    db: Database, profile: dict, payload: dict, *, operator: str,
    client=None,
) -> dict:
    """导入到指定 profile：逐服务走四步写序列（consumer 新生成密钥）。"""
    _validate_payload(payload)
    services = payload.get("mcpServices") or []
    routes_by_upstream: dict[str, list[dict]] = {}
    for r in payload.get("routes") or []:
        routes_by_upstream.setdefault(r["upstreamName"], []).append(r)
    consumers_by_route: dict[str, list[dict]] = {}
    for c in payload.get("consumers") or []:
        for rn in c.get("routes") or []:
            consumers_by_route.setdefault(rn, []).append(c)

    created: list[dict] = []
    failed: list[dict] = []
    for svc in services:
        pcfg = svc.get("providerConfig")
        if pcfg and (_REDACTED in (pcfg.get("apiTokens") or [])):
            raise errors.ValidationError(
                f"服务 {svc['name']} 的 apiTokens 为脱敏值；"
                "导出时需 include_secrets=true 或导入时重新提供",
            )
        svc_routes = routes_by_upstream.get(svc["name"], [])
        try:
            if not svc_routes:
                # 无路由的服务：只做第 1（+2）步（registry/provider）
                out = sync.register_mcp_service(
                    db, profile,
                    name=svc["name"], domain=svc["domain"],
                    port=svc.get("port", 443),
                    protocol=svc.get("protocol", "https"),
                    registry_type=svc.get("registryType", "dns"),
                    provider_config=pcfg,
                    route=None, operator=operator, client=client,
                )
                created.append({"service": svc["name"]})
                continue
            for r in svc_routes:
                bound = consumers_by_route.get(r["name"], [])
                consumer_name = bound[0]["name"] if bound else None
                out = sync.register_mcp_service(
                    db, profile,
                    name=svc["name"], domain=svc["domain"],
                    port=svc.get("port", 443),
                    protocol=svc.get("protocol", "https"),
                    registry_type=svc.get("registryType", "dns"),
                    provider_config=pcfg,
                    route={
                        "routeName": r["name"],
                        "matchType": r.get("matchType", "PRE"),
                        "matchValue": r.get("matchValue", "/"),
                        "weight": r.get("weight", 100),
                        "authEnabled": r.get("authEnabled", True),
                        "retryEnabled": r.get("retryEnabled", True),
                        "retryAttempts": r.get("retryAttempts", 3),
                        "consumerName": consumer_name,
                    },
                    operator=operator, client=client,
                )
                created.append({
                    "service": svc["name"],
                    "route": r["name"],
                    "consumer": out.get("consumer", {}).get("name"),
                    "credential": out.get("consumer", {}).get("credential"),
                })
        except errors.EAgentError as exc:
            failed.append({"service": svc["name"], "error": exc.code})
    # 未绑定路由的 consumer 单独创建（新密钥）
    from . import consumer as consumer_mod
    for c in payload.get("consumers") or []:
        if c.get("routes"):
            continue
        if dao.get_consumer_by_name(db, profile["id"], c["name"]):
            continue
        try:
            out = consumer_mod.create_consumer(
                db, profile, name=c["name"], operator=operator, client=client,
            )
            created.append({
                "consumer": c["name"], "credential": out["credential"],
            })
        except errors.EAgentError as exc:
            failed.append({"consumer": c["name"], "error": exc.code})
    return {"created": created, "failed": failed}
