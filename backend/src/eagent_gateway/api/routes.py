# -*- coding: utf-8 -*-
"""管理台 REST（§9.1 控制面）。身份: header cap-user-name（工号）。

错误响应统一为 platform-error.v1: {code, message, traceId}
（from_nekiro/01-contract-first.md P0-1）。
"""
import json
from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field

from ..catalog import service as catalog
from ..errors import (
    ConflictError,
    DependencyError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from ..permission.scopes import (
    require_admin,
    require_agent_manage,
    require_console_user,
)
from ..permission.page_permissions import require_page, require_selector_access
from ..persistence.db import Database, new_id, now
from ..router import RouterContext
from ..runtime import master_service
from ..runtime.kb_tool import build_kb_tools
from ..runtime.model_factory import mask_model_config, parse_model_config
from ..runtime.prompt_middleware import get_published_prompt
from ..security.prompt_injection import wrap_inbound
from .pagination import Page, optional_page
from .list_pages import list_page, teams_page

router = APIRouter(prefix="/api/v1")


@router.get("/selectors/services")
def service_selector(for_page: str = Depends(require_selector_access)) -> dict:
    db = Database()
    try:
        rows = db.query_all(
            "SELECT service_id, slug, display_name, source_type, enabled "
            "FROM eag_a2a_service_registration ORDER BY display_name",
        )
        return {"items": [
            {"serviceId": r["service_id"], "slug": r["slug"],
             "displayName": r["display_name"], "sourceType": r["source_type"],
             "enabled": bool(r["enabled"])} for r in rows
        ]}
    finally:
        db.close()


@router.get("/selectors/agents")
def agent_selector(for_page: str = Depends(require_selector_access)) -> dict:
    db = Database()
    try:
        rows = db.query_all(
            "SELECT master_id, slug, display_name, enabled "
            "FROM eag_master_agent ORDER BY display_name",
        )
        return {"items": [
            {"masterId": r["master_id"], "slug": r["slug"],
             "displayName": r["display_name"], "enabled": bool(r["enabled"])}
            for r in rows
        ]}
    finally:
        db.close()


@router.get("/selectors/releases")
def release_selector(
    serviceId: str = Query(..., min_length=1, max_length=64),
    for_page: str = Depends(require_selector_access),
) -> dict:
    db = Database()
    try:
        rows = db.query_all(
            "SELECT release_id, agent_version, state FROM eag_agent_release "
            "WHERE service_id=:sid ORDER BY created_at DESC",
            {"sid": serviceId},
        )
        return {"items": [
            {"releaseId": r["release_id"], "agentVersion": r["agent_version"],
             "state": r["state"]} for r in rows
        ]}
    finally:
        db.close()


@router.get("/selectors/knowledge-bases")
def knowledge_base_selector(
    for_page: str = Depends(require_selector_access),
) -> dict:
    db = Database()
    try:
        rows = db.query_all(
            "SELECT kb_id, slug, display_name, status FROM eag_knowledge_base "
            "ORDER BY display_name",
        )
        return {"items": [
            {"kbId": r["kb_id"], "slug": r["slug"],
             "displayName": r["display_name"], "status": r["status"]}
            for r in rows
        ]}
    finally:
        db.close()


@router.get("/selectors/agents/{master_id}/loop-context",
            dependencies=[Depends(require_page("loop", "view"))])
def agent_loop_context(master_id: str) -> dict:
    db = Database()
    try:
        master = db.query_one(
            "SELECT master_id, current_prompt_version_id FROM eag_master_agent "
            "WHERE master_id=:mid", {"mid": master_id},
        )
        if not master:
            raise NotFoundError("Agent Team 不存在")
        bindings = db.query_all(
            "SELECT tool_name FROM eag_master_binding WHERE master_id=:mid",
            {"mid": master_id},
        )
        prompt = None
        if master.get("current_prompt_version_id"):
            prompt = db.query_one(
                "SELECT prompt_version_id, version_no, prompt_text FROM "
                "eag_master_prompt_version WHERE prompt_version_id=:pid",
                {"pid": master["current_prompt_version_id"]},
            )
        return {"toolNames": [b["tool_name"] for b in bindings],
                "currentPrompt": prompt}
    finally:
        db.close()

# slug 直接进 URL 路径（总控 → /a2a/{slug}，服务 → 工具名域），必须是
# URL 安全的 ASCII。与 KB 契约同一口径（contracts/schemas/kb.v1.schema.json）。
# ⚠️ 仅拦新写入：库中已有的非法 slug（如中文）不回溯改写，需人工订正。
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,63}$"


def _user(cap_user_name: str | None) -> str:
    return cap_user_name or "anonymous"


def _public_master(master: dict | None) -> dict | None:
    """对外脱敏（master-model-config 专题）：model_config_json 的 api_key
    不落响应，替换为 model_config（base_url/model/尾4位，api_key_set）。"""
    if not master:
        return master
    m = dict(master)
    m["model_config"] = mask_model_config(
        parse_model_config(m.pop("model_config_json", None)),
    )
    return m


def _require_master(db: Database, master_id: str) -> dict:
    """子资源写操作前先确认总控存在，不存在直接 404。

    不查的后果不是"少个报错"而是脏数据：PUT bindings / POST prompts 都是
    INSERT，master 不存在时会静默写出孤儿行，级联删除也带不走它们
    （实际发生过：总控删掉后又被一次 PUT 把绑定写了回来）。
    UPDATE 类的则是 0 行受影响却返回 200 + null，调用方以为成功了。
    """
    master = master_service.get_master(db, master_id)
    if not master:
        raise NotFoundError("Agent Team 不存在")
    return master


def _validate_grant_users(db: Database, user_ids: list[str]) -> None:
    """个人直授只能分配给已建档且启用的员工用户。"""
    missing: list[str] = []
    disabled: list[str] = []
    for user_id in user_ids:
        row = db.query_one(
            "SELECT status FROM eag_console_user WHERE user_id=:u",
            {"u": user_id},
        )
        if row is None:
            missing.append(user_id)
        elif row["status"] != "active":
            disabled.append(user_id)
    if missing:
        raise ValidationError(
            "用户不存在，请先让员工登录建档: " + ", ".join(missing),
        )
    if disabled:
        raise ValidationError(
            "用户已禁用，不能分配个人直授: " + ", ".join(disabled),
        )


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
# A2A 服务注册（能力②）
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


def _service_out(row: dict | None) -> dict | None:
    """服务行响应脱敏: auth_secret_ref 是 AES-GCM 密文，不出 API 面，
    只回显是否已配置。"""
    if row is None:
        return None
    out = dict(row)
    out["outbound_credential_configured"] = bool(out.pop("auth_secret_ref", None))
    return out


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
    db = Database()
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
    finally:
        db.close()


@router.put("/services/{service_id}/credentials", dependencies=[Depends(require_agent_manage)])
def update_service_credentials(
    service_id: str, body: ServiceCredentialIn,
    cap_user_name: str | None = Header(None),
):
    """更新服务级出站凭证 headers；headers=null 表示清除。

    值 AES-GCM 加密落库（EAGENT_GATEWAY_MASTER_KEY），响应不回显密文。"""
    db = Database()
    try:
        try:
            svc = catalog.update_outbound_headers(
                db, service_id, body.headers, updated_by=_user(cap_user_name),
            )
        except catalog.RegistrationError as exc:
            _map_registration_error(exc)
        return _service_out(svc)
    finally:
        db.close()


@router.patch("/services/{service_id}")
async def update_service(
    service_id: str,
    body: ServiceUpdateIn,
    user: str = Depends(require_agent_manage),
):
    """按注册来源更新配置；标识与来源不可变，密钥缺省保留。"""
    from ..gateway.errors import GatewayConfigError
    db = Database()
    try:
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
    finally:
        db.close()


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
    db = Database()
    try:
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
    finally:
        db.close()


@router.get("/services", dependencies=[Depends(require_agent_manage)])
def list_services(scope: str | None = None, cap_user_name: str | None = Header(None), paging: Page | None = Depends(optional_page)):
    """服务列表。默认按用户可见集合过滤（deny-by-default：无映射=不可见）；
    管理台治理面传 scope=admin 取全集（绑定编辑/权限矩阵需要完整视图，
    该路由在 strict 模式下受 X-Admin-Token 保护）。"""
    db = Database()
    try:
        from ..permission import service as perm_svc

        if isinstance(paging, Page):
            return list_page(db, "services", paging, scope=scope, cap_user_name=cap_user_name)
        if scope == "admin":
            return [_service_out(r) for r in catalog.list_services(db)]
        # 注意: 空列表必须原样传入（deny-by-default），不能 `or None`（会退化为不过滤）
        return [_service_out(r) for r in catalog.list_services(
            db, perm_svc.visible_service_ids(db, _user(cap_user_name)),
        )]
    finally:
        db.close()


# ===========================================================================
# 观测增强（DEV-12）：远程日志纳管 + 告警查询
# ===========================================================================
class RemoteLogIn(BaseModel):
    entries: list[dict]


@router.post("/observability/remote-logs")
def ingest_remote_logs(
    body: RemoteLogIn, x_internal_token: str = Header(default=""),
):
    """远程运行服务日志纳管（HTTP 摄取；MQ 消费者复用同一写入层）。

    X-Internal-Token 与 EAGENT_REMOTE_LOG_TOKEN 匹配才可写入（fail-closed，
    未配置令牌 = 端点拒绝）。消息经模式化脱敏后落库。
    """
    import hmac as _hmac

    from ..config import settings as cfg
    from .. import monitoring as obs_monitor

    if not cfg.remote_log_token or not _hmac.compare_digest(
        x_internal_token, cfg.remote_log_token,
    ):
        raise ForbiddenError(
            "远程日志摄取需要配置并携带 X-Internal-Token"
            "（EAGENT_REMOTE_LOG_TOKEN）",
        )
    db = Database()
    try:
        return obs_monitor.ingest_remote_logs(db, body.entries, source="http")
    finally:
        db.close()


@router.get("/observability/remote-logs")
def query_remote_logs(
    serviceId: str | None = None, traceId: str | None = None,
    limit: int = 100,
):
    db = Database()
    try:
        from .. import monitoring as obs_monitor

        return {"logs": obs_monitor.query_remote_logs(
            db, service_id=serviceId, trace_id=traceId, limit=limit,
        )}
    finally:
        db.close()


@router.get("/observability/alerts", dependencies=[Depends(require_agent_manage)])
def list_alerts(serviceId: str | None = None, limit: int = 100, paging: Page | None = Depends(optional_page)):
    """告警事件列表（FIRED 未关闭在前）。"""
    db = Database()
    try:
        if isinstance(paging, Page):
            return list_page(db, "alerts", paging, serviceId=serviceId)
        sql = ("SELECT * FROM eag_alert_event")
        params: dict = {"lim": min(max(limit, 1), 500)}
        if serviceId:
            sql += " WHERE service_id=:sid"
            params["sid"] = serviceId
        sql += (" ORDER BY status='FIRED' DESC, created_at DESC LIMIT :lim")
        return {"alerts": db.query_all(sql, params)}
    finally:
        db.close()


# ===========================================================================
# 分层记忆（DEV-01）：user / agent / shared 记忆条目管理
# ===========================================================================
class MemoryIn(BaseModel):
    scope: str
    ownerId: str
    content: str
    ttlSeconds: int | None = None
    sourceTraceId: str | None = None


@router.get("/memory", dependencies=[Depends(require_agent_manage)])
def list_memory(
    scope: str, ownerId: str, limit: int = 50,
    cap_user_name: str | None = Header(None),
    paging: Page | None = Depends(optional_page),
):
    """记忆条目列表（TTL 过期惰性过滤）。user scope 只允许查自己
    （cap-user-name 即 owner），agent/shared 需管理权限。"""
    db = Database()
    try:
        from ..runtime import memory as runtime_memory
        from ..permission.scopes import SCOPE_AGENT_MANAGE, user_scopes

        if scope == "user" and ownerId != (cap_user_name or ""):
            raise ForbiddenError("用户记忆只允许查询本人（cap-user-name）")
        if scope != "user" and SCOPE_AGENT_MANAGE not in user_scopes(
            cap_user_name or "",
        ):
            raise ForbiddenError("缺少权限 scope: agent:manage")
        if isinstance(paging, Page):
            return list_page(db, "memory", paging, scope=scope, ownerId=ownerId)
        rows = runtime_memory.list_memories(
            db, scope=scope, owner_id=ownerId, limit=limit,
        )
        return {"memories": rows}
    finally:
        db.close()


@router.post("/memory", status_code=201, dependencies=[Depends(require_agent_manage)])
def add_memory(body: MemoryIn, cap_user_name: str | None = Header(None)):
    """新增记忆条目（脱敏后落库）。user scope 只允许写给本人。"""
    db = Database()
    try:
        from ..runtime import memory as runtime_memory
        from ..permission.scopes import SCOPE_AGENT_MANAGE, user_scopes

        if body.scope == "user":
            if body.ownerId != (cap_user_name or ""):
                raise ForbiddenError("用户记忆只允许写本人（cap-user-name）")
        elif SCOPE_AGENT_MANAGE not in user_scopes(cap_user_name or ""):
            raise ForbiddenError("缺少权限 scope: agent:manage")
        return runtime_memory.add_memory(
            db, scope=body.scope, owner_id=body.ownerId,
            content=body.content, source_trace_id=body.sourceTraceId,
            ttl_s=body.ttlSeconds,
        )
    finally:
        db.close()


@router.delete("/memory", dependencies=[Depends(require_agent_manage)])
def clear_memory(
    scope: str, ownerId: str, cap_user_name: str | None = Header(None),
):
    """清除记忆条目（用户清自己 / 管理员清 agent 与 shared）。"""
    db = Database()
    try:
        from ..runtime import memory as runtime_memory
        from ..permission.scopes import SCOPE_AGENT_MANAGE, user_scopes

        if scope == "user":
            if ownerId != (cap_user_name or ""):
                raise ForbiddenError("用户记忆只允许清除本人（cap-user-name）")
        elif SCOPE_AGENT_MANAGE not in user_scopes(cap_user_name or ""):
            raise ForbiddenError("缺少权限 scope: agent:manage")
        deleted = runtime_memory.clear_memories(
            db, scope=scope, owner_id=ownerId,
        )
        return {"deleted": deleted}
    finally:
        db.close()


# ===========================================================================
# 人效提升工具（DEV-17）：FAQ 挖掘 / 知识缺口 / 审批入库
# ===========================================================================
@router.post("/efficiency/faqs:generate")
def generate_faqs(masterId: str | None = None,
                  user: str = Depends(require_agent_manage)):
    """从真实运行记录挖高频问答对 → 候选 FAQ（逐字保留，不生成改写）。"""
    db = Database()
    try:
        from .. import efficiency

        return efficiency.generate_faqs(db, master_id=masterId)
    finally:
        db.close()


@router.get("/efficiency/faqs", dependencies=[Depends(require_agent_manage)])
def list_faqs(status: str | None = None, paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        from .. import efficiency

        if isinstance(paging, Page):
            return list_page(db, "faqs", paging, status=status)
        return {"faqs": efficiency.list_faqs(db, status=status)}
    finally:
        db.close()


class FaqDecisionIn(BaseModel):
    approve: bool
    kbId: str | None = None  # 通过且给定时自动进入入库规划


@router.post("/efficiency/faqs/{faq_id}:decide")
async def decide_faq(faq_id: str, body: FaqDecisionIn,
                     user: str = Depends(require_agent_manage)):
    """审批候选 FAQ；approve 且带 kbId 时合成 Markdown 源走入库规划。"""
    db = Database()
    try:
        from .. import efficiency

        return await efficiency.decide_faq(
            db, faq_id, user=user, approve=body.approve, kb_id=body.kbId,
        )
    finally:
        db.close()


@router.post("/efficiency/gaps:scan")
def scan_gaps(masterId: str | None = None,
              user: str = Depends(require_agent_manage)):
    """扫 KB 拒答（no_reliable_hit）聚类进知识缺口清单。"""
    db = Database()
    try:
        from .. import efficiency

        return efficiency.scan_gaps(db, master_id=masterId)
    finally:
        db.close()


@router.get("/efficiency/gaps", dependencies=[Depends(require_agent_manage)])
def list_gaps(status: str = "OPEN", paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        from .. import efficiency

        if isinstance(paging, Page):
            return list_page(db, "gaps", paging, status=status)
        return {"gaps": efficiency.list_gaps(db, status=status)}
    finally:
        db.close()


@router.post("/efficiency/gaps/{gap_id}:resolve")
def resolve_gap(gap_id: str, user: str = Depends(require_agent_manage)):
    """标记缺口已解决（知识补齐后人工关闭）。"""
    db = Database()
    try:
        from .. import efficiency

        return efficiency.resolve_gap(db, gap_id)
    finally:
        db.close()


# ===========================================================================
# 外部服务适配（DEV-05）：外部 KB / REST 只读工具注册
# ===========================================================================
class ExternalToolIn(BaseModel):
    kind: str  # kb_query | rest | hiagent（hiagent 预留位，注册即拒）
    name: str = Field(min_length=3, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    endpoint: str
    method: str = "POST"
    headers: dict | None = None  # AES-GCM 加密落库，永不回显
    bodyTemplate: str | None = None
    params: list[dict] | None = None
    masterId: str | None = None  # 空 = 全局


@router.post("/external-tools", status_code=201)
def create_external_tool(body: ExternalToolIn,
                         user: str = Depends(require_agent_manage)):
    db = Database()
    try:
        from ..external import adapter

        return adapter.create_tool(
            db, kind=body.kind, name=body.name, description=body.description,
            endpoint=body.endpoint, method=body.method, headers=body.headers,
            body_template=body.bodyTemplate, params=body.params,
            master_id=body.masterId, created_by=user,
        )
    finally:
        db.close()


class ExternalToolUpdateIn(BaseModel):
    # 全部可选：只有出现的键才更新（exclude_unset）；headers 出现即替换
    description: str | None = None
    endpoint: str | None = None
    method: str | None = None
    headers: dict | None = None
    bodyTemplate: str | None = None
    params: list[dict] | None = None
    masterId: str | None = None


@router.put("/external-tools/{tool_id}")
def update_external_tool(
    tool_id: str, body: ExternalToolUpdateIn,
    user: str = Depends(require_agent_manage),
):
    """编辑外部工具（name/kind 不可改；headers 留空即保持原凭证——
    凭证加密落库永不回显，编辑表单不回填）。"""
    db = Database()
    try:
        from ..external import adapter

        changes = body.model_dump(exclude_unset=True)
        remap = {"bodyTemplate": "body_template", "masterId": "master_id"}
        changes = {remap.get(k, k): v for k, v in changes.items()}
        return adapter.update_tool(db, tool_id, changes, user=user)
    finally:
        db.close()


@router.get("/external-tools", dependencies=[Depends(require_agent_manage)])
def list_external_tools(paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        from ..external import adapter

        if isinstance(paging, Page):
            return list_page(db, "external-tools", paging)
        return {"tools": adapter.list_tools(db)}
    finally:
        db.close()


@router.delete("/external-tools/{tool_id}")
def delete_external_tool(tool_id: str,
                         user: str = Depends(require_agent_manage)):
    db = Database()
    try:
        from ..external import adapter

        return adapter.delete_tool(db, tool_id)
    finally:
        db.close()


@router.post("/external-tools/{tool_id}:enable")
def enable_external_tool(tool_id: str,
                         user: str = Depends(require_agent_manage)):
    db = Database()
    try:
        from ..external import adapter

        return adapter.set_enabled(db, tool_id, True)
    finally:
        db.close()


@router.post("/external-tools/{tool_id}:disable")
def disable_external_tool(tool_id: str,
                          user: str = Depends(require_agent_manage)):
    db = Database()
    try:
        from ..external import adapter

        return adapter.set_enabled(db, tool_id, False)
    finally:
        db.close()


@router.get("/traffic-policies", dependencies=[Depends(require_agent_manage)])
def list_traffic_policies():
    """DEV-10: 生效中的流量策略列表。"""
    db = Database()
    try:
        from ..router import traffic as traffic_svc

        return {"policies": traffic_svc.list_policies(db)}
    finally:
        db.close()


@router.get("/services/{service_id}/traffic-policy", dependencies=[Depends(require_agent_manage)])
def get_traffic_policy(service_id: str):
    db = Database()
    try:
        from ..router import traffic as traffic_svc

        policy = traffic_svc.get_policy(db, service_id)
        return policy or {"service_id": service_id, "status": "NONE"}
    finally:
        db.close()


class TrafficPolicyIn(BaseModel):
    entries: list[dict]


@router.put("/services/{service_id}/traffic-policy")
def put_traffic_policy(
    service_id: str, body: TrafficPolicyIn,
    user: str = Depends(require_agent_manage),
):
    """设置多版本流量比例（整体替换）。权重合计必须 100，至少两个
    PUBLISHED 版本；操作全程审计。"""
    db = Database()
    try:
        if not catalog.get_service(db, service_id):
            raise NotFoundError("服务不存在")
        from ..router import traffic as traffic_svc

        return traffic_svc.set_policy(
            db, service_id=service_id, entries=body.entries, created_by=user,
        )
    finally:
        db.close()


@router.delete("/services/{service_id}/traffic-policy")
def delete_traffic_policy(
    service_id: str, user: str = Depends(require_agent_manage),
):
    """一键回切：停用策略，恢复默认解析链（审计留痕）。"""
    db = Database()
    try:
        from ..router import traffic as traffic_svc

        return traffic_svc.disable_policy(db, service_id=service_id, user=user)
    finally:
        db.close()


class CanaryIn(BaseModel):
    candidateReleaseId: str
    whitelist: list[str] | None = None
    ratio: float = 0.1
    thresholds: dict | None = None


@router.post("/services/{service_id}/canary")
def create_canary(
    service_id: str, body: CanaryIn,
    user: str = Depends(require_agent_manage),
):
    """DEV-11: 开灰度。白名单用户恒走候选版，其余按稳定哈希比例分流；
    基线自动取当前默认解析链版本。与流量策略互斥。"""
    db = Database()
    try:
        if not catalog.get_service(db, service_id):
            raise NotFoundError("服务不存在")
        from ..router import canary as canary_svc

        return canary_svc.create_canary(
            db, service_id=service_id,
            candidate_release_id=body.candidateReleaseId,
            whitelist=body.whitelist, ratio=body.ratio,
            thresholds=body.thresholds, created_by=user,
        )
    finally:
        db.close()


@router.get("/services/{service_id}/canary", dependencies=[Depends(require_agent_manage)])
def get_canary(service_id: str):
    """灰度状态 + 双组指标（来自真实调用账本）。"""
    db = Database()
    try:
        from ..router import canary as canary_svc

        row = canary_svc.get_running(db, service_id)
        if not row:
            return {"service_id": service_id, "status": "NONE"}
        out = canary_svc._row_out(row)
        out["metrics"] = canary_svc.metrics(db, row)
        return out
    finally:
        db.close()


@router.post("/services/{service_id}/canary:evaluate")
def evaluate_canary(
    service_id: str, user: str = Depends(require_agent_manage),
):
    """阈值守护: 候选样本足且错误率超限自动 ABORT；达标给可全量标记。"""
    db = Database()
    try:
        from ..router import canary as canary_svc

        row = canary_svc.get_running(db, service_id)
        if not row:
            raise NotFoundError("该服务没有进行中的灰度")
        return canary_svc.evaluate(db, row["canary_id"], user=user)
    finally:
        db.close()


@router.post("/services/{service_id}/canary:promote")
def promote_canary(
    service_id: str, user: str = Depends(require_agent_manage),
):
    """达标全量: 比例改 1.0，全部流量走候选版（保持观察态）。"""
    db = Database()
    try:
        from ..router import canary as canary_svc

        row = canary_svc.get_running(db, service_id)
        if not row:
            raise NotFoundError("该服务没有进行中的灰度")
        return canary_svc.promote(db, row["canary_id"], user=user)
    finally:
        db.close()


@router.post("/services/{service_id}/canary:abort")
def abort_canary(
    service_id: str, user: str = Depends(require_agent_manage),
):
    """人工紧急停止: 分组路由即刻失效，恢复默认解析链。"""
    db = Database()
    try:
        from ..router import canary as canary_svc

        row = canary_svc.get_running(db, service_id)
        if not row:
            raise NotFoundError("该服务没有进行中的灰度")
        return canary_svc.stop(db, row["canary_id"], user=user, abort=True)
    finally:
        db.close()


@router.get("/services/{service_id}", dependencies=[Depends(require_agent_manage)])
def get_service(service_id: str):
    db = Database()
    try:
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
    finally:
        db.close()


@router.get("/services/{service_id}/configuration", dependencies=[Depends(require_agent_manage)])
def get_service_configuration(service_id: str):
    """管理表单回填配置；凭证只返回配置标志，不返回明文或密文。"""
    db = Database()
    try:
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
    finally:
        db.close()


@router.post("/services/{service_id}:refresh", dependencies=[Depends(require_agent_manage)])
async def refresh_service(service_id: str):
    db = Database()
    try:
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
    finally:
        db.close()


@router.post("/services/{service_id}:probe", dependencies=[Depends(require_agent_manage)])
async def probe_service_by_id(service_id: str):
    db = Database()
    try:
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
    finally:
        db.close()


@router.post("/services/{service_id}:enable", dependencies=[Depends(require_agent_manage)])
def enable_service(service_id: str):
    db = Database()
    try:
        svc = catalog.set_enabled(db, service_id, True)
        if not svc:
            raise NotFoundError("服务不存在")
        if svc.get("source_type") == "HIAGENT":
            from ..a2a_server import mounts

            mounts.mount_hiagent(db, svc)
        return _service_out(svc)
    finally:
        db.close()


@router.post("/services/{service_id}:disable", dependencies=[Depends(require_agent_manage)])
def disable_service(service_id: str):
    db = Database()
    try:
        svc = catalog.set_enabled(db, service_id, False)
        if not svc:
            raise NotFoundError("服务不存在")
        if svc.get("source_type") == "HIAGENT":
            from ..a2a_server import mounts

            mounts.unmount_master(svc["slug"])
        return _service_out(svc)
    finally:
        db.close()


# ===========================================================================
# 统一 Agent 目录 / 注册（能力③ COMPOSITE + HIAGENT 来源，hiagent-integration/01）
# ===========================================================================
class MasterIn(BaseModel):
    model_config = {"extra": "forbid"}
    slug: str = Field(pattern=SLUG_PATTERN)
    displayName: str = Field(min_length=1, max_length=128)
    description: str | None = None
    maxIters: int = Field(default=8, ge=1, le=50)
    # 来源选择（缺省 COMPOSITE 总控；HIAGENT = HIA agent 注册）
    sourceType: str = Field(default="COMPOSITE", pattern=r"^(COMPOSITE|HIAGENT)$")
    # HIAGENT 专属：实例级服务地址（空=全局默认兜底链）、app_key（加密落库
    # 永不回显）、生成式 Card 素材、service_parameters 参数键声明
    baseUrl: str | None = None
    appKey: str | None = None
    skills: list[dict] | None = None
    inputsSchema: dict | None = None
    connectionTest: dict | None = None


@router.get("/agents")
async def list_agents(paging: Page | None = Depends(optional_page)):
    """统一 Agent 目录: COMPOSITE + remote（A2A_REMOTE / HIAGENT，带 sourceType）。"""
    db = Database()
    try:
        if isinstance(paging, Page):
            return await teams_page(db, paging)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_legacy_agent_directory, db)
    finally:
        db.close()


def _legacy_agent_directory(db):
    masters = [_public_master(m) for m in master_service.list_masters(db)]
    remotes = db.query_all(
        "SELECT service_id, slug, display_name, compatibility, enabled,"
        " source_type, created_at"
        " FROM eag_a2a_service_registration ORDER BY created_at DESC",
    )
    return {"composite": masters, "remote": remotes}


@router.post("/agents", dependencies=[Depends(require_agent_manage)])
async def create_agent(body: MasterIn, cap_user_name: str | None = Header(None)):
    """统一注册入口：sourceType=COMPOSITE 建总控（原 POST /agents 语义不变）；
    HIAGENT 注册 HIA agent 为标准单 Agent A2A 服务（注册即连通性测试，
    参数校验失败 400、远端失败 502，均不落库；成功立即挂载
    /a2a/{slug}/）。"""
    if body.sourceType == "HIAGENT":
        return await _create_hiagent_agent(body, cap_user_name)
    return _create_composite(body)


def _create_composite(body: MasterIn):
    db = Database()
    try:
        if master_service.get_master_by_slug(db, body.slug):
            raise ConflictError("slug 已存在")
        # slug 跨表唯一：HIAGENT 注册服务已挂 /a2a/{slug}，不允许抢占
        if db.query_one(
            "SELECT service_id FROM eag_a2a_service_registration WHERE slug=:s",
            {"s": body.slug},
        ):
            raise ConflictError("slug 已存在（服务注册表）")
        ts = now()
        master_id = new_id("mst_")
        db.execute(
            """
            INSERT INTO eag_master_agent
                (master_id, slug, display_name, description, source_type,
                 enabled, react_config_json, created_at, updated_at)
            VALUES (:mid, :slug, :dn, :dsc, 'COMPOSITE', 1, :cfg, :ts, :ts)
            """,
            {
                "mid": master_id, "slug": body.slug, "dn": body.displayName,
                "dsc": body.description,
                "cfg": json.dumps({"max_iters": body.maxIters}),
                "ts": ts,
            },
        )
        # 建总控即种入 PUBLISHED 基线提示词（与 master 行同事务）。
        # 否则要等下次进程启动的 seed_missing_prompts 才补上，这期间：
        # ①「提示词版本」页空白，运维没有可编辑/发布的基线；
        # ② run_record.prompt_version_id 为空，运行记录无法归属到提示词版本
        #    —— Loop Engineer 的评测与优化正建立在这个归属上。
        from ..runtime.seed_prompt import seed_prompt_for

        seed_prompt_for(db, master_id)
        db.commit()
        master = master_service.get_master(db, master_id)
        # 运行期挂载 A2A 端点：新建后立即可被调用，不再需要重启进程
        from ..a2a_server import mounts

        mounts.mount_master(db, master)
        from .team_liveness import invalidate_team
        invalidate_team(master["slug"])
        return _public_master(master)
    finally:
        db.close()


async def _create_hiagent_agent(body: MasterIn, cap_user_name: str | None):
    from ..gateway.errors import GatewayConfigError
    from ..hiagent import service as hia_service

    db = Database()
    try:
        try:
            svc = await hia_service.register_hiagent_agent(
                db,
                slug=body.slug,
                display_name=body.displayName,
                description=body.description,
                base_url=body.baseUrl,
                app_key=body.appKey,
                skills=body.skills,
                inputs_schema=body.inputsSchema,
                connection_test=body.connectionTest,
                created_by=_user(cap_user_name),
            )
        except catalog.SlugConflictError as exc:
            raise ConflictError(str(exc)) from exc
        except GatewayConfigError as exc:
            # 实例级 AppKey 加密依赖网关主密钥（fail-secure）——未配置属部署
            # 配置错误，给明确的 400 指引而非 500
            raise ValidationError(
                f"{exc.message}；填写了实例级 AppKey 的注册依赖该密钥，"
                "不使用实例级 AppKey 可先完成注册",
            ) from exc
        except hia_service.HiagentRegistrationValidationError as exc:
            raise ValidationError(str(exc)) from exc
        except hia_service.HiagentRegistrationError as exc:
            # 连通性测试失败（含鉴权失败/未配置 api_key）→ 依赖错误 502
            raise DependencyError(str(exc)) from exc
        from ..a2a_server import mounts

        mounts.mount_hiagent(db, svc)
        return svc
    finally:
        db.close()


@router.get("/agents/{master_id}")
def get_master(master_id: str):
    db = Database()
    try:
        master = master_service.get_master(db, master_id)
        if not master:
            raise NotFoundError("Agent Team 不存在")
        bindings = db.query_all(
            """
            SELECT b.*, s.slug AS service_slug, s.display_name AS service_name,
                   s.enabled AS service_enabled, s.compatibility
              FROM eag_master_binding b
              JOIN eag_a2a_service_registration s USING (service_id)
             WHERE b.master_id=:mid
            """,
            {"mid": master_id},
        )
        _, prompt_version_id = get_published_prompt(db, master_id)
        prompts = db.query_all(
            """SELECT prompt_version_id, version_no, status, source,
                      created_by, created_at, eval_report_json
                 FROM eag_master_prompt_version WHERE master_id=:mid
                ORDER BY version_no DESC""",
            {"mid": master_id},
        )
        runs_stat = db.query_one(
            """SELECT COUNT(*) AS total,
                      SUM(status='SUCCEEDED') AS ok,
                      SUM(status='FAILED') AS failed
                 FROM eag_run_record WHERE master_id=:mid""",
            {"mid": master_id},
        )
        return {
            "master": _public_master(master),
            "bindings": bindings,
            "current_prompt_version_id": prompt_version_id,
            "prompts": prompts,
            "runs_stat": runs_stat,
        }
    finally:
        db.close()


# 总控的从属数据散在 12 张表，库内不建外键（schema.sql 约定「不使用外键约束；
# 服务内保证」），因此级联必须在这里显式按「叶子 → 根」顺序删，否则留下
# 无法关联归属的孤儿行。两级从属用子查询定位（子查询目标表均非删除目标，
# MySQL 允许）。顺序即列表顺序，勿随意调整。
_MASTER_CASCADE: tuple[tuple[str, str], ...] = (
    # 评测：标注 → 用例 → 数据集
    ("eag_annotation",
     "case_id IN (SELECT case_id FROM eag_eval_case WHERE dataset_id IN"
     " (SELECT dataset_id FROM eag_eval_dataset WHERE master_id=:mid))"),
    ("eag_eval_case",
     "dataset_id IN (SELECT dataset_id FROM eag_eval_dataset"
     " WHERE master_id=:mid)"),
    ("eag_eval_dataset", "master_id=:mid"),
    # Loop Engineer：候选 → 优化任务
    ("eag_prompt_candidate",
     "optimization_id IN (SELECT optimization_id FROM eag_optimization_run"
     " WHERE master_id=:mid)"),
    ("eag_optimization_run", "master_id=:mid"),
    # 性能测试：样本 → 任务、用例 → 数据集
    ("eag_perf_sample",
     "perf_test_id IN (SELECT perf_test_id FROM eag_perf_test"
     " WHERE master_id=:mid)"),
    ("eag_perf_test", "master_id=:mid"),
    ("eag_perf_case",
     "perf_dataset_id IN (SELECT perf_dataset_id FROM eag_perf_dataset"
     " WHERE master_id=:mid)"),
    ("eag_perf_dataset", "master_id=:mid"),
    # standalone 导出登记
    ("eag_standalone_export", "master_id=:mid"),
    # KB 绑定（多态外键：target_type='MASTER' 时 target_id 才是 master_id）
    ("eag_kb_binding", "target_type='MASTER' AND target_id=:mid"),
    # 审计/运行记录（不可恢复，删除前须由调用方确认）
    ("eag_invocation_event", "master_id=:mid"),
    ("eag_run_record", "master_id=:mid"),
    # 总控自身：绑定 → 提示词版本 → 主体
    ("eag_master_binding", "master_id=:mid"),
    ("eag_master_prompt_version", "master_id=:mid"),
    ("eag_master_agent", "master_id=:mid"),
)


@router.delete("/agents/{master_id}")
def delete_master(
    master_id: str,
    confirm: str = "",
    user: str = Depends(require_agent_manage),
):
    """级联删除总控 Agent 及其全部从属数据。**不可恢复**。

    fail-closed 三道闸:
      1. ?confirm= 必须等于该总控的 slug（防止误删 / 手滑打错 id）；
      2. 有 PENDING/RUNNING 的性能测试时拒绝（perf-worker 独立进程仍在引用）；
      3. 有 RUNNING 的 Loop Engineer 优化任务时拒绝（后台任务仍在写候选）。

    删除成功后同步摘除 /a2a/{slug} 子应用（a2a_server.mounts），该地址立即 404。
    多副本部署时只对本副本生效，其余副本需 mounts.sync_from_db 对账或重启。
    """
    db = Database()
    try:
        master = master_service.get_master(db, master_id)
        if not master:
            raise NotFoundError("Agent Team 不存在")
        if confirm != master["slug"]:
            raise ValidationError(
                "删除未确认：请传 ?confirm=<slug>，当前 slug 为 "
                f"{master['slug']}",
            )
        busy_perf = db.query_one(
            "SELECT perf_test_id FROM eag_perf_test WHERE master_id=:mid"
            " AND status IN ('PENDING','RUNNING') LIMIT 1",
            {"mid": master_id},
        )
        if busy_perf:
            raise ConflictError(
                "该总控存在进行中的性能测试"
                f"（{busy_perf['perf_test_id']}），请先取消后再删除",
            )
        busy_opt = db.query_one(
            "SELECT optimization_id FROM eag_optimization_run"
            " WHERE master_id=:mid AND status='RUNNING' LIMIT 1",
            {"mid": master_id},
        )
        if busy_opt:
            raise ConflictError(
                "该总控存在进行中的优化任务"
                f"（{busy_opt['optimization_id']}），请等待结束后再删除",
            )

        deleted: dict[str, int] = {}
        for table, where in _MASTER_CASCADE:
            result = db.execute(
                f"DELETE FROM {table} WHERE {where}", {"mid": master_id},
            )
            if result.rowcount:
                deleted[table] = result.rowcount
        db.execute(
            "INSERT INTO eag_audit_event (audit_id, cap_user_name, action,"
            " resource_type, resource_id, detail, created_at)"
            " VALUES (:a, :u, 'master.delete', 'master_agent', :mid, :d, :ts)",
            {"a": new_id("aud_"), "u": user, "mid": master_id,
             "d": json.dumps(
                 {"slug": master["slug"], "cascade": deleted},
                 ensure_ascii=False),
             "ts": now()},
        )
        db.commit()
        # 运行期摘除 A2A 端点：删除后该地址立即 404，不再残留到进程重启
        from ..a2a_server import mounts

        unmounted = mounts.unmount_master(master["slug"], master_id)
        from .team_liveness import invalidate_team
        invalidate_team(master["slug"])
        return {
            "deleted": master_id,
            "slug": master["slug"],
            "cascade": deleted,
            "endpoint_unmounted": unmounted,
        }
    finally:
        db.close()


class ReactConfigIn(BaseModel):
    maxIters: int = Field(ge=1, le=50)
    responseMode: Literal["SYNTHESIZE", "SINGLE_TOOL_RAW"] | None = None


@router.put("/agents/{master_id}/react-config")
def update_react_config(master_id: str, body: ReactConfigIn):
    db = Database()
    try:
        master = _require_master(db, master_id)
        config = json.loads(master.get("react_config_json") or "{}")
        config["max_iters"] = body.maxIters
        if body.responseMode is not None:
            config["response_mode"] = body.responseMode
        db.execute(
            "UPDATE eag_master_agent SET react_config_json=:cfg, updated_at=:ts"
            " WHERE master_id=:mid",
            {"cfg": json.dumps(config),
             "ts": now(), "mid": master_id},
        )
        db.commit()
        return _public_master(master_service.get_master(db, master_id))
    finally:
        db.close()


class ModelConfigIn(BaseModel):
    baseUrl: str = ""
    model: str = ""
    apiKey: str | None = None  # None/空 = 保留已有 key（不覆盖）


@router.put("/agents/{master_id}/model-config", dependencies=[Depends(require_agent_manage)])
def update_model_config(master_id: str, body: ModelConfigIn):
    """设置总控 Agent 独立模型配置（master-model-config 专题）。

    baseUrl/model 必填；apiKey 留空 = 保留已有 key。设置后 run_master 优先用
    此配置，不再走平台全局模型（脱钩单一模型）。
    """
    db = Database()
    try:
        master = master_service.get_master(db, master_id)
        if not master:
            raise NotFoundError("Agent Team 不存在")
        cur = parse_model_config(master.get("model_config_json")) or {}
        api_key = (body.apiKey or "").strip()
        if not api_key:
            api_key = (cur.get("api_key") or "").strip()
        cfg = {
            "provider": (cur.get("provider") or "").strip() or "custom",
            "base_url": body.baseUrl.strip(),
            "model": body.model.strip(),
            "api_key": api_key,
        }
        db.execute(
            "UPDATE eag_master_agent SET model_config_json=:cfg, updated_at=:ts"
            " WHERE master_id=:mid",
            {"cfg": json.dumps(cfg, ensure_ascii=False),
             "ts": now(), "mid": master_id},
        )
        db.commit()
        return _public_master(master_service.get_master(db, master_id))
    finally:
        db.close()


@router.delete("/agents/{master_id}/model-config", dependencies=[Depends(require_agent_manage)])
def clear_model_config(master_id: str):
    """清空总控 Agent 独立模型配置，回退平台全局模型。"""
    db = Database()
    try:
        if not master_service.get_master(db, master_id):
            raise NotFoundError("Agent Team 不存在")
        db.execute(
            "UPDATE eag_master_agent SET model_config_json=NULL, updated_at=:ts"
            " WHERE master_id=:mid",
            {"ts": now(), "mid": master_id},
        )
        db.commit()
        return _public_master(master_service.get_master(db, master_id))
    finally:
        db.close()


class BindingsIn(BaseModel):
    bindings: list[dict]  # {serviceId, toolName, toolDescription, releaseId?, interactionMode?}


_INTERACTION_MODES = ("TOOL", "FLOW")


@router.put("/agents/{master_id}/bindings")
def put_bindings(master_id: str, body: BindingsIn):
    """批量勾选绑定: 全量替换该 master 的绑定（幂等）。

    releaseId 可选: 显式 pin 到某个精确 Release（P0-2）；
    缺省时按工作空间安装解析。"""
    db = Database()
    try:
        _require_master(db, master_id)
        existing = {
            r["service_id"]: r["binding_id"]
            for r in db.query_all(
                "SELECT binding_id, service_id FROM eag_master_binding"
                " WHERE master_id=:mid", {"mid": master_id},
            )
        }
        wanted = {}
        for b in body.bindings:
            wanted[b["serviceId"]] = b
        # 校验显式 pin 的 release 存在且属于该服务（fail-closed，写入前拦截）
        pinned_releases = {
            b.get("releaseId"): b["serviceId"]
            for b in wanted.values() if b.get("releaseId")
        }
        for release_id, service_id in pinned_releases.items():
            rel = db.query_one(
                "SELECT service_id FROM eag_agent_release WHERE release_id=:rid",
                {"rid": release_id},
            )
            if not rel or rel["service_id"] != service_id:
                raise ValidationError(
                    f"releaseId {release_id} 不存在或不属于该服务",
                )
        # 交互方式：FLOW（原话透传 + 原样返回 + 活跃流程直达）只支持 HIAGENT 来源
        for service_id, b in wanted.items():
            mode = b.get("interactionMode") or "TOOL"
            if mode not in _INTERACTION_MODES:
                raise ValidationError(f"interactionMode 仅支持 {'/'.join(_INTERACTION_MODES)}")
            if mode == "FLOW":
                svc = db.query_one(
                    "SELECT source_type FROM eag_a2a_service_registration"
                    " WHERE service_id=:sid", {"sid": service_id},
                )
                if not svc or svc["source_type"] != "HIAGENT":
                    raise ValidationError("流程型交互方式仅支持 HiAgent 来源的服务")
            b["interactionMode"] = mode
        # 删除取消勾选
        for service_id, binding_id in existing.items():
            if service_id not in wanted:
                db.execute(
                    "DELETE FROM eag_master_binding WHERE binding_id=:bid",
                    {"bid": binding_id},
                )
        # 新增/更新
        ts = now()
        for service_id, b in wanted.items():
            if service_id in existing:
                db.execute(
                    "UPDATE eag_master_binding SET tool_name=:tn,"
                    " tool_description=:td, release_id=:rel, interaction_mode=:mode"
                    " WHERE binding_id=:bid",
                    {"tn": b.get("toolName", service_id),
                     "td": b.get("toolDescription", ""),
                     "rel": b.get("releaseId"),
                     "mode": b["interactionMode"],
                     "bid": existing[service_id]},
                )
            else:
                db.execute(
                    """
                    INSERT INTO eag_master_binding
                        (binding_id, master_id, service_id, release_id,
                         tool_name, tool_description, interaction_mode,
                         enabled, created_at)
                    VALUES (:bid, :mid, :sid, :rel, :tn, :td, :mode, 1, :ts)
                    """,
                    {"bid": new_id("bnd_"), "mid": master_id, "sid": service_id,
                     "rel": b.get("releaseId"),
                     "tn": b.get("toolName", service_id),
                     "td": b.get("toolDescription", ""),
                     "mode": b["interactionMode"], "ts": ts},
                )
        db.commit()
        # Card 的 skills 由 build 时的绑定快照生成，改绑定后重建子应用才能让对外
        # Card 同步（运行时工具清单本身是动态的，run_master 每次从库里读）
        from ..a2a_server import mounts

        master = master_service.get_master(db, master_id)
        if master:
            mounts.remount_master(db, master)
        return {"bindings": db.query_all(
            "SELECT * FROM eag_master_binding WHERE master_id=:mid",
            {"mid": master_id},
        )}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 提示词版本: 新建 DRAFT / 发布 / 回滚（热更新, §5.3）
# ---------------------------------------------------------------------------
class PromptIn(BaseModel):
    promptText: str = Field(min_length=1)
    source: str = "MANUAL"  # MANUAL | OPTIMIZER


@router.get("/agents/{master_id}/prompts")
def list_prompts(master_id: str, with_text: bool = False, paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        if isinstance(paging, Page):
            return list_page(db, "prompts", paging, master_id=master_id, with_text=with_text)
        rows = db.query_all(
            """SELECT prompt_version_id, version_no, status, source,
                      created_by, created_at FROM eag_master_prompt_version
                 WHERE master_id=:mid ORDER BY version_no DESC""",
            {"mid": master_id},
        )
        if with_text:
            full = db.query_all(
                """SELECT prompt_version_id, prompt_text FROM
                      eag_master_prompt_version WHERE master_id=:mid""",
                {"mid": master_id},
            )
            texts = {r["prompt_version_id"]: r["prompt_text"] for r in full}
            for r in rows:
                r["prompt_text"] = texts.get(r["prompt_version_id"])
        _, current = get_published_prompt(db, master_id)
        return {"prompts": rows, "current_prompt_version_id": current}
    finally:
        db.close()


@router.post("/agents/{master_id}/prompts")
def create_prompt(
    master_id: str, body: PromptIn, cap_user_name: str | None = Header(None),
):
    db = Database()
    try:
        _require_master(db, master_id)
        last = db.query_one(
            "SELECT MAX(version_no) AS v FROM eag_master_prompt_version"
            " WHERE master_id=:mid",
            {"mid": master_id},
        )
        version_no = (last["v"] or 0) + 1
        version_id = new_id("pv_")
        db.execute(
            """
            INSERT INTO eag_master_prompt_version
                (prompt_version_id, master_id, version_no, prompt_text,
                 status, created_by, source, created_at)
            VALUES (:pvid, :mid, :vno, :text, 'DRAFT', :u, :src, :ts)
            """,
            {"pvid": version_id, "mid": master_id, "vno": version_no,
             "text": body.promptText, "u": _user(cap_user_name),
             "src": body.source, "ts": now()},
        )
        db.commit()
        return {"prompt_version_id": version_id, "version_no": version_no,
                "status": "DRAFT"}
    finally:
        db.close()


@router.post("/agents/{master_id}/prompts/{version_id}:publish", dependencies=[Depends(require_agent_manage)])
def publish_prompt(
    master_id: str, version_id: str, cap_user_name: str | None = Header(None),
):
    db = Database()
    try:
        version = db.query_one(
            "SELECT * FROM eag_master_prompt_version WHERE prompt_version_id=:pvid"
            " AND master_id=:mid",
            {"pvid": version_id, "mid": master_id},
        )
        if not version:
            raise NotFoundError("版本不存在")
        ts = now()
        db.execute(
            "UPDATE eag_master_prompt_version SET status='SUPERSEDED'"
            " WHERE master_id=:mid AND status='PUBLISHED'",
            {"mid": master_id},
        )
        db.execute(
            "UPDATE eag_master_prompt_version SET status='PUBLISHED'"
            " WHERE prompt_version_id=:pvid",
            {"pvid": version_id},
        )
        db.execute(
            "UPDATE eag_master_agent SET current_prompt_version_id=:pvid,"
            " updated_at=:ts WHERE master_id=:mid",
            {"pvid": version_id, "ts": ts, "mid": master_id},
        )
        db.execute(
            "INSERT INTO eag_audit_event (audit_id, cap_user_name, action,"
            " resource_type, resource_id, created_at) VALUES (:a, :u,"
            " 'prompt.publish', 'prompt_version', :pvid, :ts)",
            {"a": new_id("aud_"), "u": _user(cap_user_name),
             "pvid": version_id, "ts": ts},
        )
        # DEV-01: 发布即沉淀 agent 级记忆（版本/时间/操作者），后续 run
        # 的提示词注入段可召回
        from ..runtime import memory as runtime_memory

        runtime_memory.add_memory(
            db, scope="agent", owner_id=master_id,
            content=(f"提示词已发布新版本 {version.get('version_no')}"
                     f"（{version_id}），发布者 {_user(cap_user_name)}"),
        )
        db.commit()
        return {"published": version_id, "hot_update": True}
    finally:
        db.close()


@router.post("/agents/{master_id}/prompts/{version_id}:rollback", dependencies=[Depends(require_agent_manage)])
def rollback_prompt(
    master_id: str, version_id: str, cap_user_name: str | None = Header(None),
):
    """回滚 = 把历史版本重新 PUBLISH（§7.4）。"""
    return publish_prompt(master_id, version_id, cap_user_name)


def _like_prefix(prefix: str) -> str:
    """把用户输入转成只按前缀匹配的 LIKE 模式（配合 ESCAPE '!'）。

    不转义的话 prefix 里的 % / _ 会被当通配符，调用方拿到的就不是它要的那批。
    """
    escaped = (
        prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    )
    return escaped + "%"


# 调用来源靠 context_id 前缀区分：管理台试运行与压测各自生成带前缀的
# context，其余（业务方经 /a2a/{slug} 打进来的）没有前缀。这不是强约定，
# 只是这两处生成 context 时的命名（见 test_run_master / perf.runner）。
RUN_SOURCE_PREFIXES = {"testrun": "testrun-", "perf": "perf-"}


@router.get("/agents/{master_id}/runs")
def master_runs(
    master_id: str,
    limit: int = 50,
    offset: int = 0,
    source: str = "all",
    only_problems: bool = False,
    paging: Page | None = Depends(optional_page),
):
    """运行记录列表（管理台运行监控 / 试运行历史共用）。

    - source: all | a2a（业务方经 A2A 端点调入）| testrun（管理台试运行）
      | perf（性能测试）。a2a 是"排除掉另外两类"，无法用单个前缀表达，
      所以这里给的是语义选项而不是裸前缀。
    - only_problems: 只看非 SUCCEEDED 的记录（排障时最常用的一屏）。
    - offset: 配合 limit 翻页；按 started_at 倒序稳定分页。
    """
    db = Database()
    try:
        if isinstance(paging, Page):
            return list_page(db, "runs", paging, master_id=master_id, source=source, only_problems=only_problems)
        sql = "SELECT * FROM eag_run_record WHERE master_id=:mid"
        params: dict = {
            "mid": master_id,
            "lim": min(max(limit, 1), 200),
            "off": max(offset, 0),
        }
        if source in RUN_SOURCE_PREFIXES:
            sql += " AND context_id LIKE :cp ESCAPE '!'"
            params["cp"] = _like_prefix(RUN_SOURCE_PREFIXES[source])
        elif source == "a2a":
            for i, prefix in enumerate(RUN_SOURCE_PREFIXES.values()):
                sql += f" AND context_id NOT LIKE :np{i} ESCAPE '!'"
                params[f"np{i}"] = _like_prefix(prefix)
        elif source not in ("all", ""):
            raise ValidationError(
                f"source 必须是 all/a2a/{'/'.join(RUN_SOURCE_PREFIXES)} 之一",
            )
        if only_problems:
            sql += " AND status <> 'SUCCEEDED'"
        sql += " ORDER BY started_at DESC LIMIT :lim OFFSET :off"
        return db.query_all(sql, params)
    finally:
        db.close()


@router.delete("/agents/{master_id}/runs", dependencies=[Depends(require_agent_manage)])
def delete_master_runs(
    master_id: str,
    context_id: str = "",
    context_prefix: str = "",
    user: str = Depends(require_agent_manage),
):
    """删除运行记录（管理台清理试运行历史用）。

    必须二选一给出范围，**不接受空选择器**——否则一次手滑就清掉该总控
    全部调用历史：
      - context_id=xxx      删掉这一段对话
      - context_prefix=xxx  删掉该前缀下的全部（如 `testrun-` 清空试运行历史）

    只删 eag_run_record。eag_invocation_event 是 append-only 审计账本
    （invocation-api 契约），不随管理台的"清理展示数据"动作一起抹掉；
    它由 EAGENT_RETENTION_DAYS 的保留期任务统一回收。
    """
    if bool(context_id) == bool(context_prefix):
        raise ValidationError(
            "必须且只能指定 context_id 或 context_prefix 之一",
        )
    db = Database()
    try:
        _require_master(db, master_id)
        if context_id:
            sql = ("DELETE FROM eag_run_record WHERE master_id=:mid"
                   " AND context_id=:cid")
            params = {"mid": master_id, "cid": context_id}
        else:
            sql = ("DELETE FROM eag_run_record WHERE master_id=:mid"
                   " AND context_id LIKE :cp ESCAPE '!'")
            params = {"mid": master_id, "cp": _like_prefix(context_prefix[:64])}
        result = db.execute(sql, params)
        db.commit()
        return {"deleted": getattr(result, "rowcount", None)}
    finally:
        db.close()


# ===========================================================================
# 试运行（管理台单点调用）
# ===========================================================================
class TestRunIn(BaseModel):
    """管理台单次试运行入参。"""

    text: str = Field(min_length=1, max_length=8000)
    # 省略则每次新建，互不串上下文；显式传入可做多轮追问（含澄清握手回填）
    contextId: str | None = Field(default=None, max_length=64)
    # 以谁的身份跑。A2A 工具受可见性约束（组可见 ∪ 个人直授），换工号即可
    # 验证授权是否配对；省略则用调用者本人。
    asUser: str | None = Field(default=None, max_length=64)
    # 是否套用 A2A 入站「不可信远端」framing。默认 false：试运行是管理台内部
    # 调用，不是远端 peer。置 true 可复现真实 A2A 入站的模型行为。
    inboundFraming: bool = False


def _toolbox_preview(db: Database, master_id: str, as_user: str) -> dict:
    """试运行前的工具箱快照：这次调用模型手上到底有哪些工具。

    单看回答很难区分「模型没选这个工具」和「这个工具压根没进工具箱」，
    前者是提示词/描述问题，后者是权限或发布链问题。这里把两者分开。
    """
    bindings = master_service.resolve_visible_bindings(db, master_id, as_user)
    targets, unavailable = master_service.resolve_tool_targets(
        db, master_id, bindings,
    )
    return {
        "a2a": [
            {
                "toolName": t["tool_name"],
                "toolDescription": t["tool_description"],
                "serviceSlug": t["slug"],
                "releaseId": t["release"]["release_id"],
                "agentVersion": t["release"]["agent_version"],
            }
            for t in targets
        ],
        "kb": [
            {"name": t.name, "description": t.description}
            for t in build_kb_tools(db, master_id)
        ],
        # {工具名: 不可用原因}，如 RELEASE_NOT_INSTALLED / INSTALLATION_DISABLED
        "unavailable": unavailable,
    }


@router.post("/agents/{master_id}:test-run")
async def test_run_master(
    master_id: str,
    body: TestRunIn,
    session_user: tuple[str, bool] = Depends(require_console_user),
):
    """在管理台里对总控发起一次真实调用，返回回答 + 路由轨迹 + 工具箱快照。

    与走 /a2a/{slug}/ 的效果等价（同一个 master_service.run_master），
    区别只在少了 A2A 协议外壳，且默认不套入站 framing。同样落
    run_record 与 invocation_event，运行监控里能查到。
    """
    db = Database()
    try:
        master = master_service.get_master(db, master_id)
        if not master:
            raise NotFoundError("Agent Team 不存在")
        if not master["enabled"]:
            raise ValidationError("总控 Agent 已停用，无法试运行")
        operator, operator_is_admin = session_user
        # 管理员可指定任意身份（asUser）预览他人工具箱；普通用户强制本人，
        # 防止身份冒用（permission-control v1）
        if operator_is_admin and (body.asUser or "").strip():
            as_user = body.asUser.strip()
        else:
            as_user = operator
        context_id = body.contextId or f"testrun-{new_id('')}"
        toolbox = _toolbox_preview(db, master_id, as_user)
        user_text = wrap_inbound(as_user, body.text) if body.inboundFraming             else body.text
        result = await master_service.run_master(
            db,
            master_id=master_id,
            cap_user_name=as_user,
            context_id=context_id,
            user_text=user_text,
            trace_ctx=RouterContext.new_root(),
        )
        return {
            "runId": result["run_id"],
            "status": result["status"],
            "errorCode": result.get("error_code"),
            "errorDetail": result.get("error_detail"),
            "output": result["output"],
            "inputRequired": bool(result.get("input_required")),
            "routedServices": result.get("routed_services") or [],
            "toolCalls": result.get("tool_calls") or [],
            "elapsedMs": result.get("elapsed_ms"),
            # 阶段耗时拆解: 总时长慢在模型还是慢在下游，一眼看得出
            "timing": result.get("timing"),
            "promptVersionId": result.get("prompt_version_id"),
            "traceId": (result.get("trace") or {}).get("traceId"),
            "contextId": context_id,
            "asUser": as_user,
            "toolbox": toolbox,
        }
    finally:
        db.close()


# ===========================================================================
# 独立部署导出（standalone-deployment 01 §7，S-06/S-07；agent:manage）
# ===========================================================================
class StandaloneExportIn(BaseModel):
    imageTag: str = Field(
        default="eagent-standalone:latest", min_length=1, max_length=128,
    )


@router.post("/agents/{master_id}:export-standalone")
def export_standalone(
    master_id: str,
    body: StandaloneExportIn | None = None,
    user: str = Depends(require_agent_manage),
):
    """导出该总控 Agent 为独立部署包（tar.gz：card + KB 快照 + 模板）。

    一期边界 fail-closed：含 A2A 子服务绑定 → 409；KB 绑定未 READY → 409；
    card.json 剔除 api_key（部署方 env 注入）；响应头带三件套 digest
    （X-Deploy-Digest / X-Card-Digest / X-Snapshot-Hash / X-Image-Tag）。
    """
    from fastapi.responses import Response

    from .. import standalone_pkg

    image_tag = (body.imageTag if body else None) or "eagent-standalone:latest"
    db = Database()
    try:
        out = standalone_pkg.export_master_standalone(
            db, master_id=master_id, image_tag=image_tag, user=user,
        )
    finally:
        db.close()
    exp = out["export"]
    return Response(
        content=out["package"],
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{out["filename"]}"',
            "X-Deploy-Digest": exp["deploy_digest"],
            "X-Card-Digest": exp["card_digest"],
            "X-Snapshot-Hash": exp["snapshot_hash"],
            "X-Image-Tag": exp["image_tag"],
            "X-Export-Id": exp["export_id"],
        },
    )


@router.get("/agents/{master_id}/standalone-exports")
def list_standalone_exports(
    master_id: str, user: str = Depends(require_agent_manage),
):
    """导出登记列表（S-07：产物与配置状态经 master/version/digest 关联可查）。"""
    from .. import standalone_pkg

    db = Database()
    try:
        return {"exports": [
            {
                "export_id": e["export_id"],
                "agent_version": e["agent_version"],
                "image_tag": e["image_tag"],
                "card_digest": e["card_digest"],
                "snapshot_hash": e["snapshot_hash"],
                "deploy_digest": e["deploy_digest"],
                "state": e["state"],
                "created_by": e["created_by"],
                "created_at": str(e["created_at"]),
            }
            for e in standalone_pkg.list_exports(db, master_id)
        ]}
    finally:
        db.close()


# ===========================================================================
# 权限管理（§6）
# ===========================================================================
class GroupIn(BaseModel):
    name: str


@router.get("/groups", dependencies=[Depends(require_admin)])
def list_groups(paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        if isinstance(paging, Page):
            return list_page(db, "groups", paging)
        groups = db.query_all("SELECT * FROM eag_user_group ORDER BY name")
        for g in groups:
            g["members"] = [
                r["cap_user_name"] for r in db.query_all(
                    "SELECT cap_user_name FROM eag_group_membership"
                    " WHERE group_id=:gid", {"gid": g["group_id"]},
                )
            ]
            g["service_ids"] = [
                r["service_id"] for r in db.query_all(
                    "SELECT v.service_id FROM eag_group_visibility v"
                    " JOIN eag_a2a_service_registration s ON s.service_id=v.service_id"
                    " WHERE v.group_id=:gid", {"gid": g["group_id"]},
                )
            ]
        return groups
    finally:
        db.close()


@router.post("/groups", dependencies=[Depends(require_admin)])
def create_group(body: GroupIn):
    db = Database()
    try:
        if db.query_one(
            "SELECT 1 AS x FROM eag_user_group WHERE name=:n", {"n": body.name},
        ):
            raise ConflictError("用户组已存在")
        gid = new_id("grp_")
        db.execute(
            "INSERT INTO eag_user_group (group_id, name, created_at)"
            " VALUES (:gid, :name, :ts)",
            {"gid": gid, "name": body.name, "ts": now()},
        )
        db.commit()
        return {"group_id": gid, "name": body.name}
    finally:
        db.close()


@router.delete("/groups/{group_id}", dependencies=[Depends(require_admin)])
def delete_group(group_id: str, operator: str = Depends(require_admin)):
    """删除用户组并清理成员、服务可见性和页面授权。"""
    db = Database()
    try:
        group = db.query_one(
            "SELECT group_id, name FROM eag_user_group WHERE group_id=:gid",
            {"gid": group_id},
        )
        if group is None:
            raise NotFoundError("用户组不存在")
        memberships = db.execute(
            "DELETE FROM eag_group_membership WHERE group_id=:gid",
            {"gid": group_id},
        ).rowcount
        visibilities = db.execute(
            "DELETE FROM eag_group_visibility WHERE group_id=:gid",
            {"gid": group_id},
        ).rowcount
        page_permissions = db.execute(
            "DELETE FROM eag_page_permission "
            "WHERE subject_type='GROUP' AND subject_id=:gid",
            {"gid": group_id},
        ).rowcount
        db.execute(
            "DELETE FROM eag_user_group WHERE group_id=:gid",
            {"gid": group_id},
        )
        db.execute(
            "INSERT INTO eag_audit_event (audit_id, cap_user_name, action,"
            " resource_type, resource_id, detail, created_at)"
            " VALUES (:a, :u, 'group.delete', 'user_group', :gid, :d, :ts)",
            {
                "a": new_id("aud_"),
                "u": operator or "admin",
                "gid": group_id,
                "d": json.dumps({
                    "name": group["name"],
                    "deleted_memberships": memberships,
                    "deleted_visibilities": visibilities,
                    "deleted_page_permissions": page_permissions,
                }, ensure_ascii=False),
                "ts": now(),
            },
        )
        db.commit()
        return {
            "deleted": group_id,
            "name": group["name"],
            "deletedMemberships": memberships,
            "deletedVisibilities": visibilities,
            "deletedPagePermissions": page_permissions,
        }
    finally:
        db.close()


class MembersIn(BaseModel):
    capUserNames: list[str]


@router.put("/groups/{group_id}/members", dependencies=[Depends(require_admin)])
def put_members(group_id: str, body: MembersIn):
    db = Database()
    try:
        seen: set[str] = set()
        names = [n.strip() for n in body.capUserNames if n.strip()]
        names = [n for n in names if not (n in seen or seen.add(n))]
        _validate_grant_users(db, names)
        db.execute(
            "DELETE FROM eag_group_membership WHERE group_id=:gid",
            {"gid": group_id},
        )
        ts = now()
        for name in names:
            db.execute(
                "INSERT INTO eag_group_membership (group_id, cap_user_name,"
                " created_at) VALUES (:gid, :u, :ts)"
                " ON DUPLICATE KEY UPDATE cap_user_name=cap_user_name",
                {"gid": group_id, "u": name, "ts": ts},
            )
        db.commit()
        return {"ok": True}
    finally:
        db.close()


class ServicesIn(BaseModel):
    serviceIds: list[str]


@router.put("/groups/{group_id}/services", dependencies=[Depends(require_admin)])
def put_group_services(group_id: str, body: ServicesIn):
    db = Database()
    try:
        db.execute(
            "DELETE FROM eag_group_visibility WHERE group_id=:gid",
            {"gid": group_id},
        )
        ts = now()
        for sid in body.serviceIds:
            db.execute(
                "INSERT INTO eag_group_visibility (group_id, service_id,"
                " created_at) VALUES (:gid, :sid, :ts)"
                " ON DUPLICATE KEY UPDATE service_id=service_id",
                {"gid": group_id, "sid": sid, "ts": ts},
            )
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.get("/users/known", dependencies=[Depends(require_admin)])
def list_known_users():
    """系统见过的工号候选池，供管理台做成员选择的自动补全。

    ⚠️ 这**不是**员工名录：库里没有用户主表，也没有对接组织架构/HR，
    cap_user_name 只是请求头里的自由文本。这里只能把散落在各表里出现过的工号
    汇总出来，用途是减少手输错字，不能当作"全部员工"或权威身份来源。
    因此管理台必须同时保留手工输入（新员工不会出现在任何表里）。

    来源按可信度排序：调用过总控 > 已在组内 > 已有直授 > 操作过管理台。
    """
    db = Database()
    try:
        rows = db.query_all(
            """
            SELECT cap_user_name, 'RUN' AS src, MAX(started_at) AS seen_at
              FROM eag_run_record GROUP BY cap_user_name
            UNION ALL
            SELECT cap_user_name, 'GROUP', MAX(created_at)
              FROM eag_group_membership GROUP BY cap_user_name
            UNION ALL
            SELECT cap_user_name, 'DIRECT', MAX(created_at)
              FROM eag_user_visibility GROUP BY cap_user_name
            UNION ALL
            SELECT cap_user_name, 'ANNOTATE', MAX(created_at)
              FROM eag_annotation GROUP BY cap_user_name
            UNION ALL
            SELECT cap_user_name, 'ADMIN', MAX(created_at)
              FROM eag_audit_event GROUP BY cap_user_name
            """,
        )
        rank = {"RUN": 0, "GROUP": 1, "DIRECT": 2, "ANNOTATE": 3, "ADMIN": 4}
        merged: dict[str, dict] = {}
        for r in rows:
            user = (r["cap_user_name"] or "").strip()
            if not user:
                continue
            entry = merged.setdefault(
                user, {"cap_user_name": user, "sources": [], "seen_at": None},
            )
            if r["src"] not in entry["sources"]:
                entry["sources"].append(r["src"])
            if r["seen_at"] and (
                entry["seen_at"] is None or r["seen_at"] > entry["seen_at"]
            ):
                entry["seen_at"] = r["seen_at"]
        users = sorted(
            merged.values(),
            key=lambda e: (min(rank.get(s, 9) for s in e["sources"]),
                           e["cap_user_name"]),
        )
        for entry in users:
            entry["sources"].sort(key=lambda s: rank.get(s, 9))
        return {"users": users}
    finally:
        db.close()


@router.get("/users/directs", dependencies=[Depends(require_admin)])
def list_user_directs(paging: Page | None = Depends(optional_page)):
    """全部个人直授记录（治理面总览）。

    个人直授只允许指向 eag_console_user 中已建档的员工；这里按直授边表
    汇总，便于管理员回看和编辑。
    """
    db = Database()
    try:
        if isinstance(paging, Page):
            return list_page(db, "directs", paging)
        rows = db.query_all(
            "SELECT v.cap_user_name, v.service_id, v.created_at,"
            " s.slug, s.display_name"
            " FROM eag_user_visibility v"
            " JOIN eag_a2a_service_registration s USING(service_id)"
            " ORDER BY v.cap_user_name, v.created_at",
        )
        grouped: dict[str, dict] = {}
        for r in rows:
            entry = grouped.setdefault(
                r["cap_user_name"],
                {"cap_user_name": r["cap_user_name"], "services": []},
            )
            entry["services"].append({
                "service_id": r["service_id"], "slug": r["slug"],
                "display_name": r["display_name"],
                "created_at": r["created_at"],
            })
        return {"users": list(grouped.values())}
    finally:
        db.close()


@router.get("/users/{cap_user_name}/services", dependencies=[Depends(require_admin)])
def get_user_services(cap_user_name: str):
    """某工号的个人直授（不含组继承）。

    PUT 是「全量替换」语义，管理台必须先拿到现有集合回填，否则保存时会把没勾上
    的旧授权静默清空。
    """
    db = Database()
    try:
        return {
            "cap_user_name": cap_user_name,
            "service_ids": [
                r["service_id"] for r in db.query_all(
                    "SELECT v.service_id FROM eag_user_visibility v"
                    " JOIN eag_a2a_service_registration s ON s.service_id=v.service_id"
                    " WHERE v.cap_user_name=:u", {"u": cap_user_name},
                )
            ],
        }
    finally:
        db.close()


@router.put("/users/{cap_user_name}/services", dependencies=[Depends(require_admin)])
def put_user_services(cap_user_name: str, body: ServicesIn):
    """全量替换该工号的个人直授（先删后插）。"""
    db = Database()
    try:
        user = cap_user_name.strip()
        if not user:
            raise ValidationError("工号不能为空")
        if body.serviceIds:
            _validate_grant_users(db, [user])
        db.execute(
            "DELETE FROM eag_user_visibility WHERE cap_user_name=:u",
            {"u": user},
        )
        ts = now()
        for sid in body.serviceIds:
            db.execute(
                "INSERT INTO eag_user_visibility (cap_user_name, service_id,"
                " created_at) VALUES (:u, :sid, :ts)"
                " ON DUPLICATE KEY UPDATE service_id=service_id",
                {"u": user, "sid": sid, "ts": ts},
            )
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# --------------------------------------------------------------------------
# 服务维度授权（admin-ui 02 §2.7）：按服务配置可使用的用户工号列表。
# 底层复用 eag_user_visibility 个人直授边表（转置视角，全量替换），
# 运行时可见性计算（组可见 ∪ 个人直授）不变，存量组数据继续生效。
# --------------------------------------------------------------------------
@router.get("/services/{service_id}/users", dependencies=[Depends(require_admin)])
def get_service_users(service_id: str):
    db = Database()
    try:
        if not db.query_one(
            "SELECT 1 AS x FROM eag_a2a_service_registration"
            " WHERE service_id=:sid", {"sid": service_id},
        ):
            raise NotFoundError("服务不存在")
        rows = db.query_all(
            "SELECT cap_user_name FROM eag_user_visibility"
            " WHERE service_id=:sid ORDER BY cap_user_name",
            {"sid": service_id},
        )
        return {"service_id": service_id, "capUserNames": [r["cap_user_name"] for r in rows]}
    finally:
        db.close()


@router.put("/services/{service_id}/users", dependencies=[Depends(require_admin)])
def put_service_users(service_id: str, body: MembersIn):
    """全量替换该服务的授权用户；空列表 = 清空授权（全员不可见，deny-by-default）。"""
    db = Database()
    try:
        if not db.query_one(
            "SELECT 1 AS x FROM eag_a2a_service_registration"
            " WHERE service_id=:sid", {"sid": service_id},
        ):
            raise NotFoundError("服务不存在")
        # 去空格、去重、保序
        seen: set[str] = set()
        names = [n.strip() for n in body.capUserNames if n.strip()]
        names = [n for n in names if not (n in seen or seen.add(n))]
        _validate_grant_users(db, names)
        db.execute(
            "DELETE FROM eag_user_visibility WHERE service_id=:sid",
            {"sid": service_id},
        )
        ts = now()
        for name in names:
            db.execute(
                "INSERT INTO eag_user_visibility (cap_user_name, service_id,"
                " created_at) VALUES (:u, :sid, :ts)"
                " ON DUPLICATE KEY UPDATE service_id=service_id",
                {"u": name, "sid": service_id, "ts": ts},
            )
        db.commit()
        return {"service_id": service_id, "capUserNames": names}
    finally:
        db.close()


@router.get("/users/me/visibility")
def my_visibility(cap_user_name: str | None = Header(None)):
    from ..permission import service as perm_svc

    db = Database()
    try:
        return {
            "cap_user_name": _user(cap_user_name),
            "services": perm_svc.visible_services_detail(db, _user(cap_user_name)),
        }
    finally:
        db.close()
