"""Memory, observability, FAQ and external-tool endpoints."""
from contextlib import closing

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from ..errors import ForbiddenError
from ..permission.scopes import require_agent_manage
from ..persistence.db import Database
from .pagination import Page, optional_page
from .list_pages import list_page

router = APIRouter(prefix="/api/v1")


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
    with closing(Database()) as db:
        return obs_monitor.ingest_remote_logs(db, body.entries, source="http")


@router.get("/observability/remote-logs")
def query_remote_logs(
    serviceId: str | None = None, traceId: str | None = None,
    limit: int = 100,
):
    with closing(Database()) as db:
        from .. import monitoring as obs_monitor

        return {"logs": obs_monitor.query_remote_logs(
            db, service_id=serviceId, trace_id=traceId, limit=limit,
        )}


@router.get("/observability/alerts", dependencies=[Depends(require_agent_manage)])
def list_alerts(serviceId: str | None = None, limit: int = 100, paging: Page | None = Depends(optional_page)):
    """告警事件列表（FIRED 未关闭在前）。"""
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "alerts", paging, serviceId=serviceId)
        sql = ("SELECT * FROM eag_alert_event")
        params: dict = {"lim": min(max(limit, 1), 500)}
        if serviceId:
            sql += " WHERE service_id=:sid"
            params["sid"] = serviceId
        sql += (" ORDER BY status='FIRED' DESC, created_at DESC LIMIT :lim")
        return {"alerts": db.query_all(sql, params)}


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
    with closing(Database()) as db:
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


@router.post("/memory", status_code=201, dependencies=[Depends(require_agent_manage)])
def add_memory(body: MemoryIn, cap_user_name: str | None = Header(None)):
    """新增记忆条目（脱敏后落库）。user scope 只允许写给本人。"""
    with closing(Database()) as db:
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


@router.delete("/memory", dependencies=[Depends(require_agent_manage)])
def clear_memory(
    scope: str, ownerId: str, cap_user_name: str | None = Header(None),
):
    """清除记忆条目（用户清自己 / 管理员清 agent 与 shared）。"""
    with closing(Database()) as db:
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


# ===========================================================================
# 人效提升工具（DEV-17）：FAQ 挖掘 / 知识缺口 / 审批入库
# ===========================================================================
@router.post("/efficiency/faqs:generate")
def generate_faqs(masterId: str | None = None,
                  user: str = Depends(require_agent_manage)):
    """从真实运行记录挖高频问答对 → 候选 FAQ（逐字保留，不生成改写）。"""
    with closing(Database()) as db:
        from .. import efficiency

        return efficiency.generate_faqs(db, master_id=masterId)


@router.get("/efficiency/faqs", dependencies=[Depends(require_agent_manage)])
def list_faqs(status: str | None = None, paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        from .. import efficiency

        if isinstance(paging, Page):
            return list_page(db, "faqs", paging, status=status)
        return {"faqs": efficiency.list_faqs(db, status=status)}


class FaqDecisionIn(BaseModel):
    approve: bool
    kbId: str | None = None  # 通过且给定时自动进入入库规划


@router.post("/efficiency/faqs/{faq_id}:decide")
async def decide_faq(faq_id: str, body: FaqDecisionIn,
                     user: str = Depends(require_agent_manage)):
    """审批候选 FAQ；approve 且带 kbId 时合成 Markdown 源走入库规划。"""
    with closing(Database()) as db:
        from .. import efficiency

        return await efficiency.decide_faq(
            db, faq_id, user=user, approve=body.approve, kb_id=body.kbId,
        )


@router.post("/efficiency/gaps:scan")
def scan_gaps(masterId: str | None = None,
              user: str = Depends(require_agent_manage)):
    """扫 KB 拒答（no_reliable_hit）聚类进知识缺口清单。"""
    with closing(Database()) as db:
        from .. import efficiency

        return efficiency.scan_gaps(db, master_id=masterId)


@router.get("/efficiency/gaps", dependencies=[Depends(require_agent_manage)])
def list_gaps(status: str = "OPEN", paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        from .. import efficiency

        if isinstance(paging, Page):
            return list_page(db, "gaps", paging, status=status)
        return {"gaps": efficiency.list_gaps(db, status=status)}


@router.post("/efficiency/gaps/{gap_id}:resolve")
def resolve_gap(gap_id: str, user: str = Depends(require_agent_manage)):
    """标记缺口已解决（知识补齐后人工关闭）。"""
    with closing(Database()) as db:
        from .. import efficiency

        return efficiency.resolve_gap(db, gap_id)


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
    with closing(Database()) as db:
        from ..external import adapter

        return adapter.create_tool(
            db, kind=body.kind, name=body.name, description=body.description,
            endpoint=body.endpoint, method=body.method, headers=body.headers,
            body_template=body.bodyTemplate, params=body.params,
            master_id=body.masterId, created_by=user,
        )


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
    with closing(Database()) as db:
        from ..external import adapter

        changes = body.model_dump(exclude_unset=True)
        remap = {"bodyTemplate": "body_template", "masterId": "master_id"}
        changes = {remap.get(k, k): v for k, v in changes.items()}
        return adapter.update_tool(db, tool_id, changes, user=user)


@router.get("/external-tools", dependencies=[Depends(require_agent_manage)])
def list_external_tools(paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        from ..external import adapter

        if isinstance(paging, Page):
            return list_page(db, "external-tools", paging)
        return {"tools": adapter.list_tools(db)}


@router.delete("/external-tools/{tool_id}")
def delete_external_tool(tool_id: str,
                         user: str = Depends(require_agent_manage)):
    with closing(Database()) as db:
        from ..external import adapter

        return adapter.delete_tool(db, tool_id)


@router.post("/external-tools/{tool_id}:enable")
def enable_external_tool(tool_id: str,
                         user: str = Depends(require_agent_manage)):
    with closing(Database()) as db:
        from ..external import adapter

        return adapter.set_enabled(db, tool_id, True)


@router.post("/external-tools/{tool_id}:disable")
def disable_external_tool(tool_id: str,
                          user: str = Depends(require_agent_manage)):
    with closing(Database()) as db:
        from ..external import adapter

        return adapter.set_enabled(db, tool_id, False)
