# -*- coding: utf-8 -*-
"""总控 A2A 1.0 入口：Bearer 身份、服务分组参数、非流式 Task 返回。"""
import asyncio
import logging
import uuid

from eagent_gateway.a2a_server.protocol import data_part

from a2a.server.agent_execution import AgentExecutor, RequestContext
from eagent_gateway.a2a_server.application import build_application
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Part,
    TaskState,
    AgentInterface,
)

from .streaming import ReplyArtifactStream
from ..hiagent.query_extends import (
    CARD_SUBMIT_EXTENSION, CARD_SUBMIT_MEDIA, CANVAS_CARD_MEDIA, extract_card_submission,
    FLOW_EXTENSION, FLOW_MEDIA, extract_flow_release,
)
from google.protobuf.json_format import MessageToDict
from ..config import settings
from ..errors import MasterInvocationForbiddenError
from ..persistence.db import Database
from ..router import PARENT_INVOCATION_HEADER, TRACE_HEADER, RouterContext, ledger
from ..runtime import master_service
from ..runtime import run_timing
from ..runtime import turn_tracker
from ..security import console_auth
from ..security.identity import extract_identity
from ..security.prompt_injection import wrap_inbound
from ..security.router_credential import get_credential_verifier

# DEV-04: 一次 run 最多推送多少条工具进度事件（防长循环刷屏撑爆 SSE）
MAX_PROGRESS_EVENTS = 30
logger = logging.getLogger("eagent.a2a.master")


def _master_error_data(result: dict, trace_id: str) -> dict:
    data = {
        "code": result.get("error_code") or result["status"],
        "retryable": bool(result.get("error_retryable")),
        "traceId": result.get("trace", {}).get("traceId") or trace_id,
    }
    if result.get("error_source"):
        data["source"] = result["error_source"]
    if result.get("error_service"):
        data["service"] = result["error_service"]
    return data


def _request_headers(context: RequestContext) -> dict[str, str]:
    """提取入站 HTTP 头（小写 key），供身份/血缘/凭证解析。"""
    call_context = getattr(context, "call_context", None)
    state = getattr(call_context, "state", None) or {}
    headers = state.get("headers") or {}
    out: dict[str, str] = {}
    for key, value in headers.items():
        if isinstance(key, bytes):
            key = key.decode(errors="replace")
        k = str(key).lower()
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        if isinstance(value, str) and value.strip():
            out[k] = value.strip()
    return out


def _cap_user(context):
    return context.call_context.state["principal"].user_id


def _real_name(context):
    return context.call_context.state["principal"].display_name


def _verify_router_credential(
    context: RequestContext, ctx: RouterContext,
) -> None:
    """可选入站凭证校验（from_nekiro/05 §6，P1-1）。

    显式开启（EAGENT_ROUTER_CRED_VERIFY_INBOUND=1）且配置公钥时生效；
    失败 fail-closed：不执行任何 Agent 逻辑。"""
    if not settings.router_cred_verify_inbound:
        return
    verifier = get_credential_verifier()
    if verifier is None:
        raise PermissionError("已要求校验 Router 凭证但未配置公钥")
    headers = _request_headers(context)
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise PermissionError("缺少 Router 凭证（Authorization: Bearer）")
    verifier.verify(
        auth[7:].strip(),
        # 凭证按调用方分配的 invocation/trace 签发，与入站上下文必须一致
        expected_claims={
            "traceId": ctx.trace_id,
            "invocationId": ctx.invocation_id,
        },
    )


def _enforce_root_master_invocation(
    headers: dict[str, str], ctx: RouterContext,
) -> None:
    """Master 只接受业务系统根请求，拒绝任何带父调用血缘的嵌套请求。

    受管 Master→A2A 和 Agent 回调网关都会携带
    X-EAgent-Parent-Invocation-Id。约束放在 Master 入站边界，因此同时覆盖：
    - Master A →（注册为 A2A 的）Master B
    - Master → A2A 服务 → Master

    不依赖模型/提示词；即使 trace/root 不完整，只要显式携带 parent 也拒绝。
    """
    parent = headers.get(PARENT_INVOCATION_HEADER.lower(), "").strip()
    if parent:
        raise MasterInvocationForbiddenError(trace_id=ctx.trace_id)


def _record_direction_rejection(
    db: Database, *, ctx: RouterContext, master_id: str,
    error: MasterInvocationForbiddenError,
) -> None:
    """方向拒绝进入 Invocation Ledger，保证 Trace 可审计。"""
    common = {
        "invocation_id": ctx.invocation_id,
        "trace_id": ctx.trace_id,
        "root_task_id": ctx.root_task_id,
        "parent_invocation_id": ctx.parent_invocation_id,
        "workspace_id": ctx.workspace_id,
        "master_id": master_id,
    }
    ledger.append_event(
        db, event_type=ledger.INVOCATION_STARTED, status="RUNNING", **common,
    )
    ledger.append_event(
        db, event_type=ledger.INVOCATION_FINISHED, status="REJECTED",
        error_code=error.code, **common,
    )


def build_master_card(db: Database, master: dict) -> AgentCard:
    """Card 的 skills 由已绑定服务动态生成（§9.2）。"""
    bindings = master_service.active_bindings(db, master["master_id"])
    from ..hiagent.service import get_hiagent_config
    from ..hiagent.card import parse_agent_config
    from ..hiagent.query_extends import MASTER_MEDIA, MASTER_EXTENSION, EMPTY_SCHEMA
    service_schemas = {}
    for binding in bindings:
        if binding.get("source_type") == "HIAGENT":
            config = parse_agent_config((get_hiagent_config(db, binding["service_id"]) or {}).get("agent_config_json"))
            service_schemas[binding["slug"]] = config.get("inputsSchema", EMPTY_SCHEMA)
    skills = [
        AgentSkill(
            id=f"bind-{b['binding_id'][:8]}",
            name=b["tool_name"],
            description=b["tool_description"][:500],
            tags=[b["slug"]],
        )
        for b in bindings
    ] or [
        AgentSkill(
            id="master-default",
            name="intent_routing",
            description="意图识别与多服务调度的 Agent Team",
            tags=["router"],
        ),
    ]
    base = f"{settings.public_url.rstrip('/')}/a2a/{master['slug']}"
    return AgentCard(
        name=master["display_name"],
        description=master["description"] or "eAgent Agent Team",
        supported_interfaces=[AgentInterface(url=f"{base}/rpc", protocol_binding="JSONRPC", protocol_version="1.0")],
        version="1.0.0",
        default_input_modes=["text/plain", MASTER_MEDIA, CARD_SUBMIT_MEDIA, FLOW_MEDIA],
        default_output_modes=["text/plain", "application/json", CANVAS_CARD_MEDIA],
        capabilities=AgentCapabilities(
            streaming=settings.a2a_streaming,
            extensions=[
                {"uri": MASTER_EXTENSION, "required": False, "params": {"mediaType": MASTER_MEDIA, "services": service_schemas}},
                {"uri": CARD_SUBMIT_EXTENSION, "required": False, "params": {
                    "mediaType": CARD_SUBMIT_MEDIA,
                    "description": "One nonempty text and one {cardId, inputs} Data Part; full inputs; same contextId, new messageId, no taskId or services; single-use, 48 hours.",
                    "schema": {"type": "object", "required": ["cardId", "inputs"],
                               "additionalProperties": False,
                               "properties": {"cardId": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                                              "inputs": {"type": "object"}}},
                }},
                {"uri": FLOW_EXTENSION, "required": False, "params": {
                    "mediaType": FLOW_MEDIA,
                    "description": "Optional {\"release\": true} Data Part next to the text: end the context's active flow and let the master route this turn again.",
                }},
            ],
        ),
        skills=skills,
        security_schemes={
            "externalDirectoryIdentity": {"api_key_security_scheme": {
                "location": "header", "name": "cap-user-name",
                "description": "可信入口注入、且已登记启用的 eAgent 用户工号",
            }},
            "eagentSession": {"http_auth_security_scheme": {
                "scheme": "Bearer", "description": "eAgent 管理台登录会话",
            }},
        },
        # 两项分别成组表示 OR：外部目录身份或管理台 Session。
        security_requirements=[
            {"schemes": {"externalDirectoryIdentity": {"list": []}}},
            {"schemes": {"eagentSession": {"list": []}}},
        ],
    )


class MasterAgentExecutor(AgentExecutor):
    """将 SendMessage 映射到现有总控编排。"""

    def __init__(self, master_id: str) -> None:
        self._master_id = master_id

    async def execute(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        db = Database()
        try:
            updater = TaskUpdater(
                event_queue, context.task_id, context.context_id,
            )
            stream_reply = (
                ReplyArtifactStream(updater)
                if context.call_context.state.get("method") == "SendStreamingMessage"
                else None
            )
            await updater.submit()
            await updater.start_work()
            cap_user = _cap_user(context)
            # P1-3: 入站继承平台上下文（trace/root/parent/invocation）
            request_headers = _request_headers(context)
            inbound_ctx = RouterContext.from_inbound_headers(request_headers)
            inbound_ctx.cap_user_name = cap_user
            inbound_ctx.real_name = _real_name(context)
            # 原始身份头不进入 RouterContext；这里用 Authentication 已完成
            # Session/目录校验的 Principal 工号和姓名重新盖章。
            # P1-1: 可选 Router 凭证校验（fail-closed，失败不执行 Agent 逻辑）
            # 平台调用方向硬约束：Master 只允许作为根入口，不允许任何 Agent
            # 通过受管血缘再次进入 Master。拒绝发生在模型/工具装配之前。
            try:
                _enforce_root_master_invocation(request_headers, inbound_ctx)
            except MasterInvocationForbiddenError as exc:
                _record_direction_rejection(
                    db, ctx=inbound_ctx, master_id=self._master_id, error=exc,
                )
                await updater.reject(
                    message=updater.new_agent_message(parts=[Part(text=f"[{exc.code}] {exc.message}", media_type="text/plain")]),
                )
                return
            # R2 入站注入 framing（from_hermes_a2a/02）: 不可信远端文本
            # 先中和注入模式再加前缀；白名单调用方跳过
            raw_text = context.get_user_input() or ""
            if cap_user in settings.a2a_inbound_trusted:
                user_text = raw_text
            else:
                user_text = wrap_inbound(cap_user or "anonymous", raw_text)
            # service_parameters 统一参数约定：提取 data part 信封（不进提示词，
            # 独立通道穿透到 HiAgentTool 的 Inputs）
            from ..hiagent.query_extends import parameters_from_context

            inbound_service_parameters = parameters_from_context(context, master=True)
            # Task routing was checked before the SDK added this request's NEW taskId.
            inbound_message = MessageToDict(context.message)
            submission = extract_card_submission(inbound_message)
            real_name = _real_name(context)
            run_args = dict(
                master_id=self._master_id,
                cap_user_name=cap_user,
                real_name=real_name,
                context_id=context.context_id or uuid.uuid4().hex,
                user_text=user_text,
                # Flow bindings receive the user's own words, never the framed prompt.
                raw_text=raw_text,
                flow_release=extract_flow_release(inbound_message),
                trace_ctx=inbound_ctx,
                service_parameters=inbound_service_parameters,
            )
            if stream_reply is not None:
                run_args["stream_sink"] = stream_reply
            if submission is not None:
                run_args["card_submission"] = submission
                # Explicit card dispatch does not enter a prompt or the model.
                run_args["user_text"] = raw_text
            try:
                result = await master_service.run_master(db, **run_args)
            except asyncio.CancelledError:
                if stream_reply is not None:
                    await stream_reply.clear()
                await updater.cancel()
                return
            except Exception:
                logger.exception("master A2A task failed")
                if stream_reply is not None:
                    await stream_reply.clear()
                await updater.failed(message=updater.new_agent_message(parts=[
                    Part(text="主 Agent 执行失败", media_type="text/plain"),
                ]))
                return
            # R1 防环超限: REJECTED 映射 updater.reject（不伪装成完成）
            if result["status"] == "REJECTED":
                await updater.reject(
                    message=updater.new_agent_message(
                        parts=[Part(text=result["output"], media_type="text/plain")],
                    ),
                )
                return
            if result["status"] in {"FAILED", "CANCELED"}:
                error_data = _master_error_data(result, inbound_ctx.trace_id)
                message = updater.new_agent_message(parts=[
                    Part(
                        text=result.get("output") or "执行失败",
                        media_type="text/plain",
                    ),
                    data_part(data=error_data, media_type="application/json"),
                ])
                if result["status"] == "CANCELED":
                    await updater.cancel(message=message)
                else:
                    await updater.failed(message=message)
                return
            # R3 澄清握手: [INPUT_REQUIRED] → input-required（from_hermes_a2a/02）
            if result.get("input_required"):
                await updater.requires_input(
                    message=updater.new_agent_message(
                        parts=[Part(text=result["output"], media_type="text/plain")],
                    ),
                    # 本轮响应结束；任务仍可在下一次请求中继续。

                )
                return
            metadata = {
                    "run_id": result["run_id"],
                    "status": result["status"],
                    "trace_id": inbound_ctx.trace_id,
                    # 本轮实际调用的工具链，供调用方做来源展示/审计（答案正文
                    # 里也有系统追加的中文来源标注，两者同源）
                    "routed_services": result.get("routed_services") or [],
                    "final_answer_degraded": bool(result.get("final_answer_degraded")),
                }
            if stream_reply is not None:
                await stream_reply.finish(result["output"], metadata)
            else:
                await updater.add_artifact(
                    [Part(text=result["output"], media_type="text/plain")],
                    name="reply", metadata=metadata,
                )
            if result.get("cards"):
                await updater.add_artifact(
                    [data_part(data=card, media_type=CANVAS_CARD_MEDIA) for card in result["cards"]],
                    name="canvas-card", metadata=metadata,
                )
            await updater.complete()
        finally:
            db.close()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # R1: 取消即重置该 context 的防环计数，允许后续重新开始
        if context.context_id:
            turn_tracker.reset_turns(context.context_id)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


def build_master_a2a_app(db: Database, master: dict, *, task_store=None):
    """构建单个总控 Agent 的 A2A Starlette 子应用。

    task_store 为空时新建 InMemoryTaskStore。运行期重新挂载（绑定变更刷新 Card）
    时由 mounts 传入既有存储，否则进行中的任务状态会随子应用一起丢掉。
    """
    card = build_master_card(db, master)
    return build_application(card, MasterAgentExecutor(master["master_id"]), task_store=task_store,
        allowed_services={b["slug"] for b in master_service.active_bindings(db, master["master_id"]) if b.get("source_type") == "HIAGENT"})
