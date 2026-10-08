# -*- coding: utf-8 -*-
"""Higress 资源 payload 构造与变更（higress-mcp-gateway 02 基准）。

⚠️ 唯一格式来源是 02-higress-config-format-baseline.md（实测 dump 脱敏样例），
本模块的所有构造函数字段与 02 §1/§3/§4/§5 逐字段对齐——golden 测试
（tests/test_gateway_unit.py）会对照基准断言，改动前先改基准文档。

变更函数（upsert_*/remove_*）是纯 dict 操作：先经 k8s_client 读到线上
对象，套用变更后 replace（携带 resourceVersion，双写冲突 → 409）。
"""
from __future__ import annotations

import json

from . import dao
from .k8s_client import AI_ROUTE_LABEL, AI_ROUTE_LABEL_VALUE


# ===========================================================================
# McpBridge registry（02 §1）
# ===========================================================================
def build_registry_entry(svc: dict) -> dict:
    """MCP 服务行 → McpBridge.spec.registries 条目。"""
    return {
        "name": dao.registry_name(dao.slugify(svc["name"])),
        "domain": svc["domain"],
        "port": svc["port"],
        "protocol": svc["protocol"],
        "type": svc["registry_type"],
    }


def upsert_registry(bridge: dict, entry: dict) -> dict:
    spec = bridge.setdefault("spec", {})
    registries = spec.setdefault("registries", [])
    for i, item in enumerate(registries):
        if item.get("name") == entry["name"]:
            registries[i] = entry
            return bridge
    registries.append(entry)
    return bridge


def remove_registry(bridge: dict, registry: str) -> dict:
    spec = bridge.get("spec") or {}
    registries = spec.get("registries") or []
    spec["registries"] = [r for r in registries if r.get("name") != registry]
    return bridge


def find_registry(bridge: dict, registry: str) -> dict | None:
    for r in (bridge.get("spec") or {}).get("registries") or []:
        if r.get("name") == registry:
            return r
    return None


# ===========================================================================
# ai-route ConfigMap（02 §3）
# ===========================================================================
def build_ai_route_payload(
    route: dict, *, provider: str, allowed_consumers: list[str],
) -> dict:
    """路由行 → ai-route ConfigMap data.data 的 JSON payload。

    - upstreams[].provider = ai-proxy provider id（llm-<slug>，02 §6 引用链）
    - authConfig.allowedConsumers 为空时视为未开启鉴权（enabled=False）
    - proxyNextUpstream 固定形态：conditions 同基准（02 §3）
    """
    auth_on = bool(route["auth_enabled"]) and bool(allowed_consumers)
    payload = {
        "name": dao.route_payload_name(dao.slugify(route["name"])),
        "pathPredicate": {
            "matchType": route["match_type"],
            "matchValue": route["match_value"],
        },
        "upstreams": [{"provider": provider, "weight": route["weight"]}],
        "authConfig": {
            "enabled": auth_on,
            "allowedCredentialTypes": ["key-auth"],
            **({"allowedConsumers": allowed_consumers} if auth_on else {}),
        },
        "proxyNextUpstream": {
            "enabled": bool(route["retry_enabled"]),
            "attempts": route["retry_attempts"],
            "timeout": 120,
            "conditions": ["error", "timeout", "non_idempotent"],
        },
    }
    if not auth_on:
        # authConfig 键结构与基准保持一致（enabled/allowedCredentialTypes）
        payload["authConfig"] = {
            "enabled": False,
            "allowedCredentialTypes": ["key-auth"],
        }
    return payload


def ai_route_cm(slug: str, payload: dict) -> dict:
    """ai-route ConfigMap 结构（name/labels/data）。"""
    return {
        "name": dao.route_cm_name(slug),
        "labels": {
            AI_ROUTE_LABEL: AI_ROUTE_LABEL_VALUE,
            "higress.io/resource-definer": "higress",
        },
        "data": {"data": json.dumps(payload, ensure_ascii=False)},
    }


def parse_ai_route_payload(cm_data: dict) -> dict | None:
    raw = (cm_data or {}).get("data")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ===========================================================================
# 派生 Ingress（02 基准 §3a，2026-08-16 实测补录）
# ===========================================================================
# higress-console 的真实翻译链：ai-route CM（数据模型）→ Ingress
# `ai-route-route-<slug>.internal`（controller 实际消费）+ key-auth
# matchRules（按 ingress 授权 consumer）。只写 CM 不建 Ingress 时路由不生效
# （E2E 实测：请求仍兜底到既有 "/" 前缀路由）。

_PATH_TYPE = {"PRE": "Prefix", "EXACT": "Exact", "REGEX": "ImplementationSpecific"}


def route_ingress_name(slug: str) -> str:
    return f"{dao.route_cm_name(slug)}.internal"


def build_route_ingress(
    route: dict, *, upstream_slug: str, upstream_port: int = 443,
) -> dict:
    """路由行 → 派生 Ingress（形态对照线上 console 产物逐字段复刻）。"""
    match_type = route["match_type"]
    annotations = {
        "higress.io/destination":
            f"{dao.match_rule_service(upstream_slug)}:{upstream_port}",
    }
    if route["retry_enabled"]:
        annotations["higress.io/enable-proxy-next-upstream"] = "true"
        annotations["higress.io/proxy-next-upstream"] = \
            "error,timeout,non_idempotent"
        annotations["higress.io/proxy-next-upstream-timeout"] = "120"
        annotations["higress.io/proxy-next-upstream-tries"] = \
            str(route["retry_attempts"])
    else:
        annotations["higress.io/enable-proxy-next-upstream"] = "false"
    if match_type == "REGEX":
        annotations["higress.io/use-regex"] = "true"
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": route_ingress_name(dao.slugify(route["name"])),
            "labels": {
                "higress.io/domain_higress-default-domain": "true",
                "higress.io/internal": "true",
                "higress.io/resource-definer": "higress",
            },
            "annotations": annotations,
        },
        "spec": {
            "ingressClassName": "higress",
            "rules": [{
                "http": {"paths": [{
                    "path": route["match_value"],
                    "pathType": _PATH_TYPE.get(match_type, "Prefix"),
                    "backend": {"resource": {
                        "apiGroup": "networking.higress.io",
                        "kind": "McpBridge", "name": "default",
                    }},
                }]},
            }],
            "tls": [{"secretName": "default"}],
        },
    }


def build_key_auth_route_rule(
    ingress: str, allowed_consumers: list[str] | None,
) -> dict:
    """key-auth matchRule：鉴权开启 = allow 名单；关闭 = configDisable。"""
    if allowed_consumers:
        return {
            "config": {"allow": list(allowed_consumers)},
            "configDisable": False,
            "ingress": [ingress],
        }
    return {"config": {}, "configDisable": True, "ingress": [ingress]}


def upsert_key_auth_route_rule(
    wasm: dict, ingress: str, rule: dict,
) -> dict:
    spec = wasm.setdefault("spec", {})
    rules = spec.setdefault("matchRules", [])
    for i, item in enumerate(rules):
        if item.get("ingress") == [ingress]:
            rules[i] = rule
            return wasm
    rules.append(rule)
    return wasm


def remove_key_auth_route_rule(wasm: dict, ingress: str) -> dict:
    spec = wasm.get("spec") or {}
    spec["matchRules"] = [
        r for r in (spec.get("matchRules") or [])
        if r.get("ingress") != [ingress]
    ]
    return wasm


# ===========================================================================
# key-auth WasmPlugin consumer（02 §4）
# ===========================================================================
def build_consumer_entry(name: str, credential: str) -> dict:
    """consumer + 明文密钥 → key-auth defaultConfig.consumers 条目。"""
    return {
        "name": name,
        "credentials": [f"Bearer {credential}"],
        "keys": ["Authorization"],
        "in_header": True,
        "in_query": False,
    }


def upsert_consumer(wasm: dict, entry: dict) -> dict:
    cfg = wasm.setdefault("spec", {}).setdefault("defaultConfig", {})
    consumers = cfg.setdefault("consumers", [])
    for i, item in enumerate(consumers):
        if item.get("name") == entry["name"]:
            consumers[i] = entry
            return wasm
    consumers.append(entry)
    return wasm


def remove_consumer(wasm: dict, name: str) -> dict:
    cfg = (wasm.get("spec") or {}).get("defaultConfig") or {}
    consumers = cfg.get("consumers") or []
    cfg["consumers"] = [c for c in consumers if c.get("name") != name]
    return wasm


def find_consumer(wasm: dict, name: str) -> dict | None:
    for c in ((wasm.get("spec") or {}).get("defaultConfig") or {}) \
            .get("consumers") or []:
        if c.get("name") == name:
            return c
    return None


def consumer_names(wasm: dict) -> list[str]:
    return [
        c.get("name")
        for c in ((wasm.get("spec") or {}).get("defaultConfig") or {})
        .get("consumers") or []
    ]


# ===========================================================================
# ai-proxy WasmPlugin provider + matchRules（02 §5）
# ===========================================================================
def build_provider_entry(slug: str, provider_config: dict) -> dict:
    """ai-proxy defaultConfig.providers 条目（type/protocol/apiUrl/apiTokens）。"""
    return {
        "id": dao.provider_id(slug),
        "type": provider_config["type"],
        "protocol": provider_config.get("protocol", "openai"),
        "apiUrl": provider_config["apiUrl"],
        "apiTokens": list(provider_config.get("apiTokens") or []),
    }


def build_match_rule(slug: str) -> dict:
    """ai-proxy matchRules 条目：service = <registry 名>.dns。"""
    return {
        "config": {"activeProviderId": dao.provider_id(slug)},
        "service": [dao.match_rule_service(slug)],
    }


def upsert_provider(wasm: dict, provider: dict, match_rule: dict) -> dict:
    spec = wasm.setdefault("spec", {})
    cfg = spec.setdefault("defaultConfig", {})
    providers = cfg.setdefault("providers", [])
    for i, item in enumerate(providers):
        if item.get("id") == provider["id"]:
            providers[i] = provider
            break
    else:
        providers.append(provider)
    rules = spec.setdefault("matchRules", [])
    for i, item in enumerate(rules):
        if item.get("service") == match_rule["service"]:
            rules[i] = match_rule
            return wasm
    rules.append(match_rule)
    return wasm


def remove_provider(wasm: dict, slug: str) -> dict:
    spec = wasm.get("spec") or {}
    cfg = (spec.get("defaultConfig") or {})
    pid = dao.provider_id(slug)
    cfg["providers"] = [
        p for p in (cfg.get("providers") or []) if p.get("id") != pid
    ]
    svc = dao.match_rule_service(slug)
    spec["matchRules"] = [
        r for r in (spec.get("matchRules") or [])
        if r.get("service") != [svc]
    ]
    return wasm
