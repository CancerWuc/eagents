# -*- coding: utf-8 -*-
"""网关配置 REST API（higress-mcp-gateway 01 §5.2，03 T05）。

- 前缀 /api/v1/gateway；scope gateway:manage（未配置映射 = 单管理员放行）。
- 管理面鉴权沿用 GatewayAuthMiddleware（strict 模式 X-Admin-Token）；
  身份头 cap-user-name 作为审计 operator。
- 列表接口的线上反显（live*）在 K8s 不可达时优雅降级为 null + liveError，
  写接口的 K8s 失败按 03 §4.2 映射（403/404/409）。
- 密钥只在创建/轮换响应中回显一次明文；列表只显尾 4 位。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .. import config, errors
from ..persistence.db import Database
from ..permission.scopes import require_gateway_manage
from . import (
    consumer as consumer_mod,
    dao,
    export_import,
    mcp_service,
    route as route_mod,
    status as status_mod,
    sync,
)
from .k8s_client import client_for_profile
from ..api.pagination import Page, optional_page, reject_mixed_pagination
from ..api.list_pages import list_page

router = APIRouter(prefix="/api/v1/gateway")


def _profile_or_404(db: Database, profile_id: int) -> dict:
    p = dao.get_profile(db, profile_id)
    if not p:
        raise errors.NotFoundError("网关环境不存在")
    return p


def _profile_out(p: dict) -> dict:
    return {
        "id": p["id"],
        "name": p["name"],
        "higress_ns": p["higress_ns"],
        "kube_context": p["kube_context"],
        "higress_version": p.get("higress_version"),
        "enabled": bool(p["enabled"]),
        "created_at": str(p["created_at"]),
        "updated_at": str(p["updated_at"]),
    }


def _live_client(db: Database, profile: dict):
    """写路径用：enabled=0 拒绝操作（ServiceDisabled），否则返回 K8s client。"""
    if not profile["enabled"]:
        raise errors.ServiceDisabledError("该网关环境已停用")
    return client_for_profile(profile)


# ===========================================================================
# profile（网关环境）管理
# ===========================================================================
class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    higressNs: str = Field(min_length=1, max_length=64)
    kubeContext: str = Field(default="", max_length=128)
    higressVersion: str | None = Field(default=None, max_length=16)


class ProfileUpdateIn(BaseModel):
    higressNs: str | None = Field(default=None, min_length=1, max_length=64)
    kubeContext: str | None = Field(default=None, max_length=128)
    higressVersion: str | None = Field(default=None, max_length=16)
    enabled: bool | None = None


@router.get("/profiles")
def list_profiles(user: str = Depends(require_gateway_manage)):
    db = Database()
    try:
        return {"profiles": [_profile_out(p) for p in dao.list_profiles(db)]}
    finally:
        db.close()


@router.post("/profiles", status_code=201)
def create_profile(
    body: ProfileIn, user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        if dao.get_profile_by_name(db, body.name):
            raise errors.ConflictError(f"网关环境已存在: {body.name}")
        p = dao.create_profile(
            db, name=body.name, higress_ns=body.higressNs,
            kube_context=body.kubeContext, higress_version=body.higressVersion,
        )
        dao.record_audit(
            db, profile_id=p["id"], resource_type="profile",
            action="create", payload_before=None,
            payload_after=_profile_out(p), result="success", operator=user,
        )
        return _profile_out(p)
    finally:
        db.close()


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: int, user: str = Depends(require_gateway_manage)):
    db = Database()
    try:
        return _profile_out(_profile_or_404(db, profile_id))
    finally:
        db.close()


@router.put("/profiles/{profile_id}")
def update_profile(
    profile_id: int, body: ProfileUpdateIn,
    user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        _profile_or_404(db, profile_id)
        updated = dao.update_profile(
            db, profile_id, higress_ns=body.higressNs,
            kube_context=body.kubeContext,
            higress_version=body.higressVersion, enabled=body.enabled,
        )
        dao.record_audit(
            db, profile_id=profile_id, resource_type="profile",
            action="update", payload_before=None,
            payload_after=_profile_out(updated), result="success",
            operator=user,
        )
        return _profile_out(updated)
    finally:
        db.close()


@router.delete("/profiles/{profile_id}")
def delete_profile(
    profile_id: int, user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        p = _profile_or_404(db, profile_id)
        children = dao.count_profile_children(db, profile_id)
        if children:
            raise errors.ConflictError(
                f"该环境仍有 {children} 个受管资源（MCP 服务/路由/consumer），"
                "请先清空后再删除环境",
            )
        dao.delete_profile(db, profile_id)
        dao.record_audit(
            db, profile_id=profile_id, resource_type="profile",
            action="delete", payload_before=_profile_out(p),
            payload_after={"deleted": p["name"]}, result="success",
            operator=user,
        )
        return {"deleted": p["name"]}
    finally:
        db.close()


@router.get("/profiles/{profile_id}/status")
def profile_status(
    profile_id: int, user: str = Depends(require_gateway_manage),
):
    """连通性探测 + 资源计数汇总（T04/T11）；探测到的版本回写 profile。"""
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        out = status_mod.profile_overview(db, profile)
        ver = (out.get("connectivity") or {}).get("higressVersion")
        if ver and not profile.get("higress_version"):
            dao.update_profile(db, profile_id, higress_version=ver)
        return out
    finally:
        db.close()


# ===========================================================================
# MCP 服务（T07：McpBridge registry；带 route/provider 时走四步写序列 T10）
# ===========================================================================
class ProviderIn(BaseModel):
    type: str = Field(min_length=1, max_length=32)
    protocol: str = "openai"
    apiUrl: str = Field(min_length=1, max_length=255)
    apiTokens: list[str] = Field(default_factory=list)


class RouteSpecIn(BaseModel):
    routeName: str | None = Field(default=None, max_length=128)
    matchType: str = "PRE"
    matchValue: str = Field(default="/", max_length=512)
    weight: int = Field(default=100, ge=0, le=100)
    authEnabled: bool = True
    retryEnabled: bool = True
    retryAttempts: int = Field(default=3, ge=0, le=10)
    consumerName: str | None = Field(default=None, max_length=128)


class McpServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    domain: str = Field(min_length=1, max_length=255)
    port: int = Field(default=443, ge=1, le=65535)
    protocol: str = "https"
    registryType: str = "dns"
    provider: ProviderIn | None = None
    route: RouteSpecIn | None = None


@router.get("/profiles/{profile_id}/mcp-services")
def list_mcp_services(
    profile_id: int, user: str = Depends(require_gateway_manage),
    paging: Page | None = Depends(optional_page),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        if isinstance(paging, Page):
            out = list_page(db, "gateway-services", paging, profile_id=profile_id)
            out.update(services=out["items"], liveRegistries=None, liveError=None)
            try:
                from .mcp_service import DEFAULT_BRIDGE
                bridge = client_for_profile(profile).get_mcpbridge(DEFAULT_BRIDGE)
                out["liveRegistries"] = ((bridge or {}).get("spec") or {}).get("registries") or []
            except errors.EAgentError as exc:
                out["liveError"] = exc.message
            return out
        out = {"services": [], "liveRegistries": None, "liveError": None}
        out["services"] = mcp_service.list_services(db, profile)["services"]
        try:
            client = client_for_profile(profile)
            out["liveRegistries"] = mcp_service.list_services(
                db, profile, client,
            )["liveRegistries"]
        except errors.EAgentError as exc:
            out["liveError"] = exc.message
        return out
    finally:
        db.close()


@router.post("/profiles/{profile_id}/mcp-services", status_code=201)
def register_mcp_service(
    profile_id: int, body: McpServiceIn,
    user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        _live_client(db, profile)
        return sync.register_mcp_service(
            db, profile,
            name=body.name, domain=body.domain, port=body.port,
            protocol=body.protocol, registry_type=body.registryType,
            provider_config=body.provider.model_dump() if body.provider
            else None,
            route=body.route.model_dump() if body.route else None,
            operator=user,
        )
    finally:
        db.close()


@router.delete("/profiles/{profile_id}/mcp-services/{name}")
def delete_mcp_service(
    profile_id: int, name: str, user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        _live_client(db, profile)
        return mcp_service.delete_service(
            db, profile, name, operator=user,
        )
    finally:
        db.close()


# ===========================================================================
# 路由（T08：ai-route ConfigMap）
# ===========================================================================
class RouteIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    matchType: str = "PRE"
    matchValue: str = Field(min_length=1, max_length=512)
    upstreamName: str = Field(min_length=1, max_length=128)
    weight: int = Field(default=100, ge=0, le=100)
    authEnabled: bool = True
    authType: str = "key-auth"
    retryEnabled: bool = True
    retryAttempts: int = Field(default=3, ge=0, le=10)
    consumers: list[str] | None = None


class RouteUpdateIn(BaseModel):
    matchType: str | None = None
    matchValue: str | None = Field(default=None, min_length=1, max_length=512)
    upstreamName: str | None = None
    weight: int | None = Field(default=None, ge=0, le=100)
    authEnabled: bool | None = None
    authType: str | None = None
    retryEnabled: bool | None = None
    retryAttempts: int | None = Field(default=None, ge=0, le=10)
    consumers: list[str] | None = None


@router.get("/profiles/{profile_id}/routes")
def list_routes(profile_id: int, user: str = Depends(require_gateway_manage), paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        if isinstance(paging, Page):
            out = list_page(db, "gateway-routes", paging, profile_id=profile_id)
            out.update(routes=out["items"], liveRouteCms=None, liveError=None)
            try:
                from .k8s_client import AI_ROUTE_LABEL
                out["liveRouteCms"] = client_for_profile(profile).list_configmaps_by_label(AI_ROUTE_LABEL, "ai-route")
            except errors.EAgentError as exc:
                out["liveError"] = exc.message
            return out
        out: dict = {
            "routes": [], "liveRouteCms": None, "liveError": None,
        }
        out["routes"] = route_mod.list_routes(db, profile)["routes"]
        try:
            client = client_for_profile(profile)
            out["liveRouteCms"] = route_mod.list_routes(
                db, profile, client,
            )["liveRouteCms"]
        except errors.EAgentError as exc:
            out["liveError"] = exc.message
        return out
    finally:
        db.close()


@router.post("/profiles/{profile_id}/routes", status_code=201)
def create_route(
    profile_id: int, body: RouteIn,
    user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        _live_client(db, profile)
        return route_mod.create_route(
            db, profile, name=body.name, match_type=body.matchType,
            match_value=body.matchValue, upstream_name=body.upstreamName,
            weight=body.weight, auth_enabled=body.authEnabled,
            auth_type=body.authType, retry_enabled=body.retryEnabled,
            retry_attempts=body.retryAttempts,
            consumer_names=body.consumers, operator=user,
        )
    finally:
        db.close()


@router.put("/profiles/{profile_id}/routes/{name}")
def update_route(
    profile_id: int, name: str, body: RouteUpdateIn,
    user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        client = _live_client(db, profile)
        if body.consumers is not None:
            route = dao.get_route_by_name(db, profile_id, name)
            if not route:
                raise errors.NotFoundError(f"路由不存在: {name}")
            route_mod.bind_consumers(
                db, profile, route, body.consumers, client=client,
            )
        out = route_mod.update_route(
            db, profile, name, match_type=body.matchType,
            match_value=body.matchValue, upstream_name=body.upstreamName,
            weight=body.weight, auth_enabled=body.authEnabled,
            auth_type=body.authType, retry_enabled=body.retryEnabled,
            retry_attempts=body.retryAttempts, operator=user, client=client,
        )
        return out
    finally:
        db.close()


@router.delete("/profiles/{profile_id}/routes/{name}")
def delete_route(
    profile_id: int, name: str, user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        _live_client(db, profile)
        return route_mod.delete_route(db, profile, name, operator=user)
    finally:
        db.close()


# ===========================================================================
# consumer（T09/T13：key-auth 密钥管理）
# ===========================================================================
class ConsumerIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    credential: str | None = Field(default=None, max_length=256)
    routes: list[str] | None = None


@router.get("/profiles/{profile_id}/consumers")
def list_consumers(
    profile_id: int, user: str = Depends(require_gateway_manage),
    paging: Page | None = Depends(optional_page),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        if isinstance(paging, Page):
            out = list_page(db, "gateway-consumers", paging, profile_id=profile_id)
            out.update(consumers=out["items"], liveConsumers=None, liveError=None)
            try:
                from .consumer import KEY_AUTH_PLUGIN
                from .resources import consumer_names
                wasm = client_for_profile(profile).get_wasmplugin(KEY_AUTH_PLUGIN)
                out["liveConsumers"] = consumer_names(wasm) if wasm else None
            except errors.EAgentError as exc:
                out["liveError"] = exc.message
            return out
        out: dict = {"consumers": [], "liveConsumers": None, "liveError": None}
        out["consumers"] = consumer_mod.list_consumers(db, profile)[
            "consumers"
        ]
        try:
            client = client_for_profile(profile)
            out["liveConsumers"] = consumer_mod.list_consumers(
                db, profile, client,
            )["liveConsumers"]
        except errors.EAgentError as exc:
            out["liveError"] = exc.message
        return out
    finally:
        db.close()


@router.post("/profiles/{profile_id}/consumers", status_code=201)
def create_consumer(
    profile_id: int, body: ConsumerIn,
    user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        _live_client(db, profile)
        return consumer_mod.create_consumer(
            db, profile, name=body.name, credential=body.credential,
            route_names=body.routes, operator=user,
        )
    finally:
        db.close()


@router.post("/profiles/{profile_id}/consumers/{name}/rotate")
def rotate_consumer(
    profile_id: int, name: str,
    user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        _live_client(db, profile)
        return consumer_mod.rotate_consumer(
            db, profile, name, operator=user,
            grace_seconds=config.settings.gateway_key_grace_seconds,
        )
    finally:
        db.close()


@router.delete("/profiles/{profile_id}/consumers/{name}")
def delete_consumer(
    profile_id: int, name: str, user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        _live_client(db, profile)
        return consumer_mod.delete_consumer(db, profile, name, operator=user)
    finally:
        db.close()


# ===========================================================================
# 审计 / 导出 / 导入（T14/T15）
# ===========================================================================
@router.get("/profiles/{profile_id}/audits", dependencies=[Depends(reject_mixed_pagination)])
def list_audits(
    profile_id: int, resourceType: str | None = None,
    result: str | None = None, page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=50, ge=1, le=200),
    user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        _profile_or_404(db, profile_id)
        rows, total = dao.list_audits(
            db, profile_id, resource_type=resourceType, result=result,
            page=page, page_size=pageSize,
        )
        for r in rows:
            r["created_at"] = str(r["created_at"])
        return {"items": rows, "audits": rows, "page": page, "pageSize": pageSize,
                "total": total}
    finally:
        db.close()


@router.get("/profiles/{profile_id}/export")
def export_config(
    profile_id: int, include_secrets: bool = False,
    user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        return export_import.export_profile(
            db, profile, include_secrets=include_secrets,
        )
    finally:
        db.close()


@router.post("/profiles/{profile_id}/import")
def import_config(
    profile_id: int, payload: dict,
    user: str = Depends(require_gateway_manage),
):
    db = Database()
    try:
        profile = _profile_or_404(db, profile_id)
        client = _live_client(db, profile)
        return export_import.import_profile(
            db, profile, payload, operator=user, client=client,
        )
    finally:
        db.close()
