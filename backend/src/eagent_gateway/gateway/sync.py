# -*- coding: utf-8 -*-
"""四步写序列编排（higress-mcp-gateway 02 §6，03 T10）。

一次「注册 MCP 服务」按依赖顺序写四类资源：
  1. McpBridge 追加 registry（llm-llm-<slug>.internal）
  2. ai-proxy WasmPlugin 追加 provider + matchRules（若配 provider）
  3. key-auth WasmPlugin 追加 consumer（若启用鉴权）
  4. ai-route ConfigMap upsert（引用 provider + consumer）

约束：
- 每步写入后读回复核；任一步失败 → 逆序回滚已写资源（best-effort），
  回滚失败的部分在审计 payload 里标注 partial（人工补做路径）。
- DB 行在全部 K8s 步骤成功后落库；第 4 步失败会连同 DB 行一并回滚。
"""
from __future__ import annotations

import logging

from .. import errors
from ..persistence.db import Database
from . import consumer as consumer_mod
from . import crypto, dao, mcp_service, resources, route as route_mod
from .k8s_client import GatewayK8sClient

logger = logging.getLogger("eagent.gateway.sync")


def register_mcp_service(
    db: Database, profile: dict, *, name: str, domain: str, port: int = 443,
    protocol: str = "https", registry_type: str = "dns",
    provider_config: dict | None = None,
    route: dict | None = None, operator: str,
    client: GatewayK8sClient | None = None,
) -> dict:
    """一键注册（T10）：服务 + provider + consumer + 路由 原子化落地。

    route 子对象为空时仅做第 1（+2）步（等价 mcp_service.create_service）。
    """
    if client is None:
        from .k8s_client import client_for_profile
        client = client_for_profile(profile)
    slug = dao.slugify(name)
    if dao.get_mcp_service_by_name(db, profile["id"], name):
        raise errors.ConflictError(f"MCP 服务名已存在: {name}")
    want_auth = bool((route or {}).get("authEnabled", True))

    applied: list[tuple[str, object]] = []  # (label, rollback 可调用)

    def _rollback_all() -> dict:
        """逆序回滚已应用步骤；返回回滚结果明细（partial 标注）。"""
        report: dict = {}
        for label, rollback in reversed(applied):
            try:
                rollback()
                report[label] = "rolled_back"
            except Exception:  # noqa: BLE001
                report[label] = "PARTIAL: rollback_failed_manual_fix"
                logger.exception("gateway sync rollback failed: %s", label)
        return report

    try:
        # ---- 第 1 步：McpBridge registry ----
        entry = resources.build_registry_entry({
            "name": name, "domain": domain, "port": port,
            "protocol": protocol, "registry_type": registry_type,
        })
        bridge = mcp_service._ensure_bridge(client)
        resources.upsert_registry(bridge, entry)
        client.replace_mcpbridge(bridge)
        after = client.get_mcpbridge(mcp_service.DEFAULT_BRIDGE)
        if after is None or resources.find_registry(after, entry["name"]) is None:
            raise errors.DependencyError("McpBridge 写入后复核失败")

        def _rb_registry():
            b = client.get_mcpbridge(mcp_service.DEFAULT_BRIDGE)
            if b is not None and resources.find_registry(b, entry["name"]):
                client.replace_mcpbridge(
                    resources.remove_registry(b, entry["name"]),
                )
        applied.append(("mcpbridge", _rb_registry))

        # ---- 第 2 步：ai-proxy provider + matchRules ----
        if provider_config:
            if not provider_config.get("type") \
                    or not provider_config.get("apiUrl"):
                raise errors.ValidationError(
                    "provider 需提供 type 与 apiUrl（02 基准 §5）",
                )
            wasm = client.get_wasmplugin("ai-proxy.internal")
            if wasm is None:
                raise errors.DependencyError(
                    "ai-proxy.internal WasmPlugin 不存在，"
                    "请先在 Higress 侧启用 ai-proxy 插件",
                )
            provider = resources.build_provider_entry(slug, provider_config)
            rule = resources.build_match_rule(slug)
            resources.upsert_provider(wasm, provider, rule)
            client.replace_wasmplugin(wasm)

            def _rb_provider():
                w = client.get_wasmplugin("ai-proxy.internal")
                if w is not None:
                    client.replace_wasmplugin(
                        resources.remove_provider(w, slug),
                    )
            applied.append(("ai-proxy", _rb_provider))

        # ---- 第 3 步：key-auth consumer（若启用鉴权）----
        consumer_name = (route or {}).get("consumerName")
        if want_auth and route is not None:
            crypto.assert_master_key_configured()
            consumer_name = consumer_name or f"consumer-{slug}"
            if dao.get_consumer_by_name(db, profile["id"], consumer_name):
                raise errors.ConflictError(
                    f"consumer 已存在: {consumer_name}",
                )
            plaintext = crypto.generate_credential()
            centry = resources.build_consumer_entry(consumer_name, plaintext)
            consumer_mod._write_key_auth(
                client, lambda w: resources.upsert_consumer(w, centry),
            )

            def _rb_consumer():
                consumer_mod._write_key_auth(
                    client,
                    lambda w: resources.remove_consumer(w, consumer_name),
                )
            applied.append(("key-auth", _rb_consumer))
        else:
            plaintext = None

        # ---- DB 行（第 4 步的 allowedConsumers 依赖库内绑定关系）----
        svc = dao.create_mcp_service(
            db, profile_id=profile["id"], name=name, domain=domain,
            port=port, protocol=protocol, registry_type=registry_type,
            provider_config=provider_config, created_by=operator,
        )
        consumer_row = None
        if plaintext is not None:
            consumer_row = dao.create_consumer(
                db, profile_id=profile["id"], name=consumer_name,
                credential=crypto.encrypt_credential(plaintext),
                route_ids=[], created_by=operator,
            )

        # ---- 第 4 步：ai-route ConfigMap ----
        route_row = None
        if route is not None:
            route_row = dao.create_route(
                db, profile_id=profile["id"],
                # 缺省（None/未传）与服务同名：payload 名 = route-<slug>、
                # CM 名 = ai-route-route-<slug>，与 console 现状命名一致（02 §3）
                name=route.get("routeName") or name,
                match_type=route.get("matchType", "PRE"),
                match_value=route.get("matchValue", "/"),
                upstream_mcp=svc["id"], weight=route.get("weight", 100),
                auth_enabled=want_auth, auth_type="key-auth",
                retry_enabled=route.get("retryEnabled", True),
                retry_attempts=route.get("retryAttempts", 3),
                created_by=operator,
            )
            if consumer_row is not None:
                dao.update_consumer(
                    db, consumer_row["id"], route_ids=[route_row["id"]],
                )
            try:
                cm = route_mod.write_route_cm(
                    db, profile, route_row, operator=operator, client=client,
                )
            except Exception:  # noqa: BLE001
                # 第 4 步失败：DB 行回滚 + K8s 三步逆序回滚
                dao.delete_route(db, route_row["id"])
                if consumer_row is not None:
                    dao.delete_consumer(db, consumer_row["id"])
                dao.delete_mcp_service(db, svc["id"])
                raise
    except errors.EAgentError as exc:
        report = _rollback_all()
        dao.record_audit(
            db, profile_id=profile["id"], resource_type="sync",
            action="create", payload_before=None,
            payload_after={
                "service": name, "failed": exc.code, "rollback": report,
            },
            result="failed", operator=operator,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        report = _rollback_all()
        dao.record_audit(
            db, profile_id=profile["id"], resource_type="sync",
            action="create", payload_before=None,
            payload_after={
                "service": name, "failed": type(exc).__name__,
                "rollback": report,
            },
            result="failed", operator=operator,
        )
        raise errors.DependencyError("注册序列执行失败（已回滚）") from exc

    dao.record_audit(
        db, profile_id=profile["id"], resource_type="sync",
        action="create", payload_before=None,
        payload_after={
            "service": mcp_service._service_out(svc),
            **({"consumer": consumer_name} if consumer_name else {}),
            **({"route": route_row["name"]} if route_row else {}),
        },
        result="success", operator=operator,
    )
    out: dict = {"service": mcp_service._service_out(svc)}
    if consumer_row is not None:
        cout = consumer_mod._consumer_out(db, consumer_row)
        if plaintext is not None:
            cout["credential"] = plaintext  # 仅本次响应回显一次
        out["consumer"] = cout
    if route_row is not None:
        out["route"] = route_mod._route_out(db, route_row)
    return out
