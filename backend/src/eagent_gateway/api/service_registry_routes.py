"""Service registration, credentials, lifecycle and rollout endpoints."""
from contextlib import closing
from typing import NoReturn

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field

from ..catalog import service as catalog
from ..errors import ConflictError, DependencyError, NotFoundError, ValidationError
from ..permission.scopes import require_agent_manage
from ..persistence.db import Database
from .pagination import Page, optional_page
from .list_pages import list_page
from .route_helpers import SLUG_PATTERN, _user
from .serializers import _service_out

router = APIRouter(prefix="/api/v1")


def _map_registration_error(exc: catalog.RegistrationError) -> NoReturn:
    """把注册/刷新失败映射为统一错误（fail-closed: 分类明确，不笼统 400）。"""
    if isinstance(exc, (catalog.SlugConflictError, catalog.ServiceInUseError)):
        raise ConflictError(str(exc)) from exc
    if isinstance(exc, catalog.CardFetchError):
        raise DependencyError(str(exc)) from exc
    if isinstance(exc, catalog.CardContractError):
        raise ValidationError(str(exc)) from exc
    raise ValidationError(str(exc)) from exc


# ===========================================================================
# A2A 服务注册
# ===========================================================================
class ServiceIn(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    displayName: str
    agentCardUrl: str
    authSecretRef: dict[str, str] | None = None
    tags: str | None = None


class ServiceUpdateIn(BaseModel):
    model_config = {"extra": "forbid"}
    displayName: str | None = None
    tags: str | None = None
    agentCardUrl: str | None = None
    authSecretRef: dict[str, str] | None = None
    description: str | None = None
    baseUrl: str | None = None
    appKey: str | None = None
    skills: list[dict] | None = None
    inputsSchema: dict | None = None
    connectionTest: dict | None = None
    clearCredential: bool = False


class ServiceCredentialIn(BaseModel):
    """服务级出站凭证 headers（intranet-a2a-stream-adapter）。

    如 {"apikey": "..."}：注册/更新时 AES-GCM 加密落库，出站调用注入。"""
    headers: dict[str, str] | None = None


class ProbeIn(BaseModel):
    agentCardUrl: str


def _card_summary(card: dict) -> dict:
    """Agent Card 预览摘要（注册弹窗「连接测试」展示用）。"""
    return {
        "name": card.get("name"),
        "description": card.get("description"),
        "url": catalog.normalize_card(card)["supportedInterfaces"][0]["url"],
        "version": card.get("version"),
        "protocolVersion": "1.0",
        "capabilities": card.get("capabilities") or {},
        "skills": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "description": s.get("description", ""),
                "tags": s.get("tags", []),
            }
            for s in card.get("skills") or []
        ],
    }


@router.post("/services:probe", dependencies=[Depends(require_agent_manage)])
async def probe_service(body: ProbeIn):
    """连接探测：拉取 Agent Card + 契约校验 + 兼容性判定，不落库。

    注册弹窗「测试连接」先用它预览远端卡片内容（名称/能力/技能面），
    确认无误再正式注册。失败语义与注册一致（拉取失败 502 / 契约不合规 400）。"""
    try:
        card, base = await catalog.fetch_agent_card(body.agentCardUrl)
    except catalog.RegistrationError as exc:
        _map_registration_error(exc)
    compat, reason = catalog.check_compatibility(card)
    return {
        "baseUrl": base,
        "card": _card_summary(card),
        "compatibility": compat,
        "compatibilityReason": reason,
    }


@router.post("/services", dependencies=[Depends(require_agent_manage)])
async def register_service(
    body: ServiceIn, cap_user_name: str | None = Header(None),
):
    with closing(Database()) as db:
        try:
            svc = await catalog.register_service(
                db,
                slug=body.slug,
                display_name=body.displayName,
                agent_card_url=body.agentCardUrl,
                auth_secret_ref=body.authSecretRef,
                tags=body.tags,
                created_by=_user(cap_user_name),
            )
            return _service_out(svc)
        except catalog.RegistrationError as exc:
            _map_registration_error(exc)


@router.put("/services/{service_id}/credentials", dependencies=[Depends(require_agent_manage)])
def update_service_credentials(
    service_id: str, body: ServiceCredentialIn,
    cap_user_name: str | None = Header(None),
):
    """更新服务级出站凭证 headers；headers=null 表示清除。

    值 AES-GCM 加密落库（EAGENT_GATEWAY_MASTER_KEY），响应不回显密文。"""
    with closing(Database()) as db:
        try:
            svc = catalog.update_outbound_headers(
                db, service_id, body.headers, updated_by=_user(cap_user_name),
            )
        except catalog.RegistrationError as exc:
            _map_registration_error(exc)
        return _service_out(svc)


@router.patch("/services/{service_id}")
async def update_service(
    service_id: str,
    body: ServiceUpdateIn,
    user: str = Depends(require_agent_manage),
):
    """按注册来源更新配置；标识与来源不可变，密钥缺省保留。"""
    from ..gateway.errors import GatewayConfigError
    with closing(Database()) as db:
        current = catalog.get_service(db, service_id)
        if not current:
            raise NotFoundError("服务不存在")
        changes = body.model_dump(exclude_unset=True)
        hiagent = current.get("source_type") == "HIAGENT"
        allowed = ({"displayName", "description", "baseUrl", "appKey", "skills", "inputsSchema", "connectionTest", "clearCredential"}
                   if hiagent else {"displayName", "tags", "agentCardUrl", "authSecretRef", "clearCredential"})
        if set(changes) - allowed:
            raise ValidationError("更新字段与服务注册来源不匹配")
        if body.clearCredential and (body.appKey or body.authSecretRef):
            raise ValidationError("清除凭证与填写新凭证不能同时选择")
        if not hiagent and body.agentCardUrl is not None and not body.agentCardUrl.strip():
            raise ValidationError("服务地址不能为空")
        try:
            if hiagent:
                from ..hiagent import service as hia_service
                svc = await hia_service.update_hiagent_agent(db, service_id, changes, updated_by=user)
            else:
                svc = await catalog.update_service(
                    db, service_id, display_name=body.displayName, tags=body.tags,
                    agent_card_url=body.agentCardUrl, outbound_headers=body.authSecretRef,
                    clear_credential=body.clearCredential, updated_by=user,
                )
            from ..a2a_server import mounts
            if hiagent:
                mounts.remount_hiagent(db, svc)
            for master in db.query_all(
                "SELECT DISTINCT m.* FROM eag_master_agent m JOIN eag_master_binding b ON b.master_id=m.master_id WHERE b.service_id=:sid",
                {"sid": service_id},
            ):
                mounts.remount_master(db, master)
            return _service_out(svc)
        except catalog.RegistrationError as exc:
            if hiagent:
                from ..hiagent.service import HiagentRegistrationValidationError, HiagentRegistrationError
                if isinstance(exc, HiagentRegistrationValidationError):
                    raise ValidationError(str(exc)) from exc
                if isinstance(exc, HiagentRegistrationError):
                    raise DependencyError(str(exc)) from exc
            _map_registration_error(exc)
        except GatewayConfigError as exc:
            raise ValidationError(exc.message) from exc


@router.delete("/services/{service_id}")
def delete_service(
    service_id: str,
    confirm: str = Query(..., description="必须与待删除服务的 slug 一致"),
    force: bool = Query(False, description="若被总控绑定，是否强制解除绑定"),
    user: str = Depends(require_agent_manage),
):
    """级联删除服务（带 Slug 防呆 + 活跃策略拦截 + 总控解绑与运行时 remount）。

    A2A_REMOTE 来源：级联删除注册与绑定；HIAGENT 来源：额外清理 side 表/
    会话映射/合成 Card Revision 并摘除 /a2a/{slug} 端点。两者均不可恢复。"""
    with closing(Database()) as db:
        svc = catalog.get_service(db, service_id)
        if not svc:
            raise NotFoundError("服务不存在")
        if svc.get("source_type") == "HIAGENT":
            if confirm != svc["slug"]:
                raise ValidationError(
                    f"confirm 参数必须为 slug（{svc['slug']}）以确认删除",
                )
            from ..hiagent import service as hia_service
            from ..a2a_server import mounts

            try:
                result = hia_service.delete_hiagent_agent(
                    db, service_id, force=force, deleted_by=user,
                )
            except catalog.RegistrationError as exc:
                _map_registration_error(exc)
            result["endpoint_unmounted"] = mounts.unmount_master(svc["slug"])
            return result
        try:
            return catalog.delete_service(
                db,
                service_id,
                confirm=confirm,
                force=force,
                deleted_by=user,
            )
        except catalog.RegistrationError as exc:
            _map_registration_error(exc)


@router.get("/services", dependencies=[Depends(require_agent_manage)])
def list_services(scope: str | None = None, cap_user_name: str | None = Header(None), paging: Page | None = Depends(optional_page)):
    """服务列表。默认按用户可见集合过滤（deny-by-default：无映射=不可见）；
    管理台治理面传 scope=admin 取全集（绑定编辑/权限矩阵需要完整视图，
    该路由在 strict 模式下受 X-Admin-Token 保护）。"""
    with closing(Database()) as db:
        from ..permission import service as perm_svc

        if isinstance(paging, Page):
            return list_page(db, "services", paging, scope=scope, cap_user_name=cap_user_name)
        if scope == "admin":
            return [_service_out(r) for r in catalog.list_services(db)]
        # 注意: 空列表必须原样传入（deny-by-default），不能 `or None`（会退化为不过滤）
        return [_service_out(r) for r in catalog.list_services(
            db, perm_svc.visible_service_ids(db, _user(cap_user_name)),
        )]


# Registered after enhancement endpoints to preserve route priority.
detail_router = APIRouter(prefix="/api/v1")


@detail_router.get("/traffic-policies", dependencies=[Depends(require_agent_manage)])
def list_traffic_policies():
    """DEV-10: 生效中的流量策略列表。"""
    with closing(Database()) as db:
        from ..router import traffic as traffic_svc

        return {"policies": traffic_svc.list_policies(db)}


@detail_router.get("/services/{service_id}/traffic-policy", dependencies=[Depends(require_agent_manage)])
def get_traffic_policy(service_id: str):
    with closing(Database()) as db:
        from ..router import traffic as traffic_svc

        policy = traffic_svc.get_policy(db, service_id)
        return policy or {"service_id": service_id, "status": "NONE"}


class TrafficPolicyIn(BaseModel):
    entries: list[dict]


@detail_router.put("/services/{service_id}/traffic-policy")
def put_traffic_policy(
    service_id: str, body: TrafficPolicyIn,
    user: str = Depends(require_agent_manage),
):
    """设置多版本流量比例（整体替换）。权重合计必须 100，至少两个
    PUBLISHED 版本；操作全程审计。"""
    with closing(Database()) as db:
        if not catalog.get_service(db, service_id):
            raise NotFoundError("服务不存在")
        from ..router import traffic as traffic_svc

        return traffic_svc.set_policy(
            db, service_id=service_id, entries=body.entries, created_by=user,
        )


@detail_router.delete("/services/{service_id}/traffic-policy")
def delete_traffic_policy(
    service_id: str, user: str = Depends(require_agent_manage),
):
    """一键回切：停用策略，恢复默认解析链（审计留痕）。"""
    with closing(Database()) as db:
        from ..router import traffic as traffic_svc

        return traffic_svc.disable_policy(db, service_id=service_id, user=user)


class CanaryIn(BaseModel):
    candidateReleaseId: str
    whitelist: list[str] | None = None
    ratio: float = 0.1
    thresholds: dict | None = None


@detail_router.post("/services/{service_id}/canary")
def create_canary(
    service_id: str, body: CanaryIn,
    user: str = Depends(require_agent_manage),
):
    """DEV-11: 开灰度。白名单用户恒走候选版，其余按稳定哈希比例分流；
    基线自动取当前默认解析链版本。与流量策略互斥。"""
    with closing(Database()) as db:
        if not catalog.get_service(db, service_id):
            raise NotFoundError("服务不存在")
        from ..router import canary as canary_svc

        return canary_svc.create_canary(
            db, service_id=service_id,
            candidate_release_id=body.candidateReleaseId,
            whitelist=body.whitelist, ratio=body.ratio,
            thresholds=body.thresholds, created_by=user,
        )


@detail_router.get("/services/{service_id}/canary", dependencies=[Depends(require_agent_manage)])
def get_canary(service_id: str):
    """灰度状态 + 双组指标（来自真实调用账本）。"""
    with closing(Database()) as db:
        from ..router import canary as canary_svc

        row = canary_svc.get_running(db, service_id)
        if not row:
            return {"service_id": service_id, "status": "NONE"}
        out = canary_svc._row_out(row)
        out["metrics"] = canary_svc.metrics(db, row)
        return out


@detail_router.post("/services/{service_id}/canary:evaluate")
def evaluate_canary(
    service_id: str, user: str = Depends(require_agent_manage),
):
    """阈值守护: 候选样本足且错误率超限自动 ABORT；达标给可全量标记。"""
    with closing(Database()) as db:
        from ..router import canary as canary_svc

        row = canary_svc.get_running(db, service_id)
        if not row:
            raise NotFoundError("该服务没有进行中的灰度")
        return canary_svc.evaluate(db, row["canary_id"], user=user)


@detail_router.post("/services/{service_id}/canary:promote")
def promote_canary(
    service_id: str, user: str = Depends(require_agent_manage),
):
    """达标全量: 比例改 1.0，全部流量走候选版（保持观察态）。"""
    with closing(Database()) as db:
        from ..router import canary as canary_svc

        row = canary_svc.get_running(db, service_id)
        if not row:
            raise NotFoundError("该服务没有进行中的灰度")
        return canary_svc.promote(db, row["canary_id"], user=user)


@detail_router.post("/services/{service_id}/canary:abort")
def abort_canary(
    service_id: str, user: str = Depends(require_agent_manage),
):
    """人工紧急停止: 分组路由即刻失效，恢复默认解析链。"""
    with closing(Database()) as db:
        from ..router import canary as canary_svc

        row = canary_svc.get_running(db, service_id)
        if not row:
            raise NotFoundError("该服务没有进行中的灰度")
        return canary_svc.stop(db, row["canary_id"], user=user, abort=True)


@detail_router.get("/services/{service_id}", dependencies=[Depends(require_agent_manage)])
def get_service(service_id: str):
    with closing(Database()) as db:
        svc = catalog.get_service(db, service_id)
        if not svc:
            raise NotFoundError("服务不存在")
        card = catalog.current_card(db, service_id)
        health = db.query_all(
            "SELECT * FROM eag_a2a_health_observation"
            " WHERE service_id=:sid ORDER BY observed_at DESC LIMIT 20",
            {"sid": service_id},
        )
        return {"service": _service_out(svc), "card": card, "health": health}


@detail_router.get("/services/{service_id}/configuration", dependencies=[Depends(require_agent_manage)])
def get_service_configuration(service_id: str):
    """管理表单回填配置；凭证只返回配置标志，不返回明文或密文。"""
    with closing(Database()) as db:
        svc = catalog.get_service(db, service_id)
        if not svc:
            raise NotFoundError("服务不存在")
        result = {"serviceId": service_id, "sourceType": svc.get("source_type", "A2A_REMOTE"),
                  "slug": svc["slug"], "displayName": svc["display_name"],
                  "agentCardUrl": svc["agent_card_url"], "tags": svc.get("tags") or "",
                  "credentialConfigured": bool(svc.get("auth_secret_ref"))}
        if svc.get("source_type") == "HIAGENT":
            from ..hiagent.service import get_hiagent_config
            from ..hiagent.card import parse_agent_config
            side = get_hiagent_config(db, service_id)
            if not side:
                raise NotFoundError("HiAgent 配置不存在")
            cfg = parse_agent_config(side.get("agent_config_json"))
            result.update({"baseUrl": side.get("base_url") or "", "credentialConfigured": bool(side.get("app_key_enc")),
                           "description": cfg.get("description") or "", "skills": cfg.get("skills") or [],
                           "inputsSchema": cfg.get("inputsSchema", {"type": "object", "properties": {}, "additionalProperties": False}),
                           "connectionTest": cfg.get("connectionTest", {"query": "你好", "inputs": {}})})
        return result


@detail_router.post("/services/{service_id}:refresh", dependencies=[Depends(require_agent_manage)])
async def refresh_service(service_id: str):
    with closing(Database()) as db:
        svc = catalog.get_service(db, service_id)
        if svc and svc.get("source_type") == "HIAGENT":
            # HIAGENT 来源：Card 由平台生成，刷新 = 按最新 side 表配置重建
            from ..hiagent import service as hia_service

            refreshed = hia_service.refresh_hiagent_card(db, service_id)
            if not refreshed:
                raise NotFoundError("服务不存在")
            return refreshed
        try:
            return _service_out(await catalog.refresh_card(db, service_id))
        except catalog.RegistrationError as exc:
            _map_registration_error(exc)


@detail_router.post("/services/{service_id}:probe", dependencies=[Depends(require_agent_manage)])
async def probe_service_by_id(service_id: str):
    with closing(Database()) as db:
        svc = catalog.get_service(db, service_id)
        if svc and svc.get("source_type") == "HIAGENT":
            # HIAGENT 来源：探测 = 连通性测试（create_conversation + 发「你好」）
            from ..hiagent import service as hia_service

            observation = await hia_service.probe_service(db, service_id)
            if not observation:
                raise NotFoundError("服务不存在")
            return observation
        observation = await catalog.probe_health(db, service_id)
        if not observation:
            raise NotFoundError("服务不存在")
        return observation


@detail_router.post("/services/{service_id}:enable", dependencies=[Depends(require_agent_manage)])
def enable_service(service_id: str):
    with closing(Database()) as db:
        svc = catalog.set_enabled(db, service_id, True)
        if not svc:
            raise NotFoundError("服务不存在")
        if svc.get("source_type") == "HIAGENT":
            from ..a2a_server import mounts

            mounts.mount_hiagent(db, svc)
        return _service_out(svc)


@detail_router.post("/services/{service_id}:disable", dependencies=[Depends(require_agent_manage)])
def disable_service(service_id: str):
    with closing(Database()) as db:
        svc = catalog.set_enabled(db, service_id, False)
        if not svc:
            raise NotFoundError("服务不存在")
        if svc.get("source_type") == "HIAGENT":
            from ..a2a_server import mounts

            mounts.unmount_master(svc["slug"])
        return _service_out(svc)
