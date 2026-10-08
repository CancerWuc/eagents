# -*- coding: utf-8 -*-
"""总控 Master Agent 装配与运行（§5.2 + from_nekiro/02/03/04/07）。

一次 A2A 请求的生命周期:
  1. 使用 A2A 边界已确认的 RuntimePrincipal → 解析可见服务子集
  2. 绑定 ∩ 可见 ∩ ENABLED+COMPATIBLE → 解析已安装的 PUBLISHED Release
     （fail-closed: 未发布/未安装/被禁用的目标不进入工具箱）
  3. 按 contextId 取/建 AgentState（多轮记忆）
  4. PUBLISHED 提示词经 middleware 动态注入（热更新）
  5. DONT_ASK 运行 agent loop → Invocation 事件 + run_record
     （trace_id/root_task_id/parent/invocation_id 血缘 + error_code）；
     出站经 RouterContext 携带用户身份头（a2a 规范 §3 逐跳透传）
"""
import asyncio
import json
import logging
import time
from dataclasses import replace

from agentscope.agent import Agent, ReActConfig
from agentscope.message import Msg, TextBlock, ToolResultState
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from ..config import settings
from ..errors import (
    EAgentError,
    GatewayTimeoutError,
    is_runtime_error_code,
    runtime_error_is_retryable,
    runtime_error_message,
)
from ..persistence.db import Database, new_id, now
from ..permission import service as perm_svc
from ..registry import MysqlInstanceDirectory
from ..router import RouterContext, ledger
from ..router.client import ManagedA2AClient
from ..router.resolver import resolve_target
from ..security.redact import digest_of, redact_text
from .a2a_tool import A2ATool
from .kb_tool import build_kb_tools
from .model_factory import (
    build_chat_model_for,
    build_chat_model_from_config,
    model_config_usable,
    parse_model_config,
)
from .prompt_middleware import DbPromptMiddleware, get_published_prompt
from . import active_flow, run_timing, turn_tracker

logger = logging.getLogger("eagent.master")

# ---------------------------------------------------------------------------
# 会话存储: contextId ↔ AgentState（一期进程内存 + LRU 上限；DB 持久化二期）
# ---------------------------------------------------------------------------
_MAX_SESSIONS = 1024
_sessions: dict[str, AgentState] = {}


def get_or_create_state(context_id: str) -> AgentState:
    state = _sessions.get(context_id)
    if state is None:
        if len(_sessions) >= _MAX_SESSIONS:
            _sessions.pop(next(iter(_sessions)))
        state = AgentState(session_id=context_id)
        _sessions[context_id] = state
    return state


# ---------------------------------------------------------------------------
# 定义查询
# ---------------------------------------------------------------------------
def get_master(db: Database, master_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_master_agent WHERE master_id=:mid", {"mid": master_id},
    )


def get_master_by_slug(db: Database, slug: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_master_agent WHERE slug=:slug", {"slug": slug},
    )


def list_masters(db: Database) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_master_agent ORDER BY created_at DESC",
    )


def active_bindings(db: Database, master_id: str) -> list[dict]:
    """绑定 + ENABLED + COMPATIBLE 的服务（工具候选）。"""
    return db.query_all(
        """
        SELECT b.binding_id, b.service_id, b.release_id, b.tool_name,
               b.tool_description, b.interaction_mode, s.slug, s.display_name,
               s.agent_card_url, s.enabled, s.compatibility, s.source_type
          FROM eag_master_binding b
          JOIN eag_a2a_service_registration s ON s.service_id = b.service_id
         WHERE b.master_id = :mid AND b.enabled = 1
           AND s.enabled = 1 AND s.compatibility = 'COMPATIBLE'
        """,
        {"mid": master_id},
    )


def resolve_visible_bindings(
    db: Database, master_id: str, cap_user_name: str,
) -> list[dict]:
    """可见绑定 = 绑定 ∩ 用户可见集合（组 ∪ 个人直授）。
    deny-by-default：无映射记录 → 空集合。"""
    visible = set(perm_svc.visible_service_ids(db, cap_user_name))
    bindings = active_bindings(db, master_id)
    return [b for b in bindings if b["service_id"] in visible]


def resolve_tool_targets(
    db: Database, master_id: str, bindings: list[dict],
    cap_user_name: str | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """把可见绑定解析为 (binding+release+workspace) 列表。

    fail-closed: 解析失败的目标不进入工具箱，原因记录在
    unavailable[tool_name] = reason（P0-2 §6.6、07 §6 不 fallback）。
    cap_user_name 供灰度分组分流（DEV-11）。"""
    targets: list[dict] = []
    unavailable: dict[str, str] = {}
    for b in bindings:
        if b.get("source_type") == "HIAGENT":
            # HIA 成员（hiagent-integration/01 §3.5）：原生直连，不走
            # Release/Installation 解析链——合成 release 只承载治理元数据
            # （版本号取注册时 Card 快照摘要），装配期分支见 run_master
            from ..catalog import service as catalog_svc
            from ..hiagent import service as hia_svc

            svc = catalog_svc.get_service(db, b["service_id"])
            if not svc or not svc.get("enabled"):
                unavailable[b["tool_name"]] = "SERVICE_DISABLED"
                logger.warning(
                    "binding %s -> unavailable (SERVICE_DISABLED: %s)",
                    b["tool_name"], b["service_id"],
                )
                continue
            targets.append({
                **b,
                "release": hia_svc.synthetic_release(db, svc),
                "workspace_id": None,
            })
            continue
        target, reason = resolve_target(
            db,
            service_id=b["service_id"],
            release_id=b.get("release_id"),
            master_id=master_id,
            cap_user_name=cap_user_name,
        )
        if target is None:
            unavailable[b["tool_name"]] = reason
            logger.warning(
                "binding %s -> unavailable (%s: %s)",
                b["tool_name"], reason, b["service_id"],
            )
            continue
        targets.append({**b, "release": target["release"],
                        "workspace_id": target["workspace_id"]})
    return targets, unavailable


def _find_eagent_error(exc: BaseException) -> EAgentError | None:
    """沿异常链/异常组找回被框架（AgentScope 等）包装的 EAgentError。

    fail-closed 语义（07 文档）: 工具抛出的 TIMEOUT/DEPENDENCY_ERROR
    不能因为被包装（含 ExceptionGroup）就退化成 INTERNAL_ERROR。"""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, EAgentError):
            return cur
        # ExceptionGroup / wrapped call 链
        stack.extend(getattr(cur, "exceptions", ()) or ())
        stack.append(cur.__cause__)
        stack.append(cur.__context__)
    return None


def _extract_tool_error(tool_output: str) -> tuple[str | None, str]:
    text = (tool_output or "").lstrip()
    if text.startswith("[") and "]" in text:
        end = text.index("]")
        return text[1:end], text[end + 1:].strip()
    return None, ""


def _recover_tool_error_code(tool_output: str) -> str:
    """从工具失败文本恢复错误码（a2a_tool 的 "[CODE] 文案" 约定）。

    未携带合法标记的失败文本（如 KB 工具）按 07 表默认 DEPENDENCY_ERROR。"""
    code, _ = _extract_tool_error(tool_output)
    if is_runtime_error_code(code):
        return str(code)
    return "DEPENDENCY_ERROR"


def _single_tool_raw_output(
    tool_results: list,
    *,
    input_required: bool,
    status: str,
    has_structured_output: bool = False,
) -> str | None:
    """Return the sole successful text result without normalizing it.

    The strict shape check is intentional: multiple calls and structured or
    multi-block outputs must continue through the model synthesis path.
    """
    if (input_required or has_structured_output
            or status != "SUCCEEDED" or len(tool_results) != 1):
        return None
    result = tool_results[0]
    if str(result.state) != str(ToolResultState.SUCCESS):
        return None
    output = result.output
    if isinstance(output, str):
        return output
    if not isinstance(output, list) or len(output) != 1:
        return None
    text = getattr(output[0], "text", None)
    return text if isinstance(text, str) else None


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
# R3 标记解析抽至 run_semantics（standalone 容器共用；此处保持原名再导出）
from .run_semantics import INPUT_REQUIRED_MARKER, extract_input_required  # noqa: E402,F401


def _finish_metadata(elapsed_ms: int, timing: dict) -> dict:
    """INVOCATION_FINISHED 的 metadata: 只放耗时标量（账本不放输入输出）。

    有了 llm_ms / tool_ms，查一次 trace 就能判断慢在模型还是慢在下游，
    不用再拿事件时间戳去倒推空档。"""
    return {
        "elapsed_ms": elapsed_ms,
        "llm_ms": timing["llmMs"],
        "llm_calls": timing["llmCalls"],
        "tool_ms": timing["toolMs"],
        "tool_calls": timing["toolCalls"],
        "other_ms": timing["otherMs"],
    }


async def _reply_with_backoff(
    agent, message, *, backoff_s: float, timeout_s: float = 0.0,  # noqa: ANN001
):
    """M2a（from_hermes_a2a/04）: 瞬时错误（非业务 EAgentError）退避重试一次。

    业务错误（TIMEOUT/DEPENDENCY_ERROR 等，含被 AgentScope 包装的）一律
    fail-closed 直接抛出不重试——重试会把"工具已失败"的状态带进第二次
    回复，伪造成功；HTTP 429/5xx 已由 OpenAI 客户端 max_retries 内部重试，
    此处只兜底网络/框架层偶发异常。

    R5: timeout_s > 0 时对整个回复阶段加 wall-clock 预算，超时抛
    GatewayTimeoutError（TIMEOUT，不伪装成功）；外部取消（CancelledError）
    原样向上传播。
    """
    async def _once():
        try:
            return await agent.reply(message)
        except Exception as exc:
            if isinstance(exc, EAgentError) or \
                    _find_eagent_error(exc) is not None:
                raise
            await asyncio.sleep(backoff_s)
            return await agent.reply(message)

    if timeout_s and timeout_s > 0:
        try:
            return await asyncio.wait_for(_once(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise GatewayTimeoutError(
                f"总控运行超过 wall-clock 预算（{timeout_s}s）",
            ) from exc
    return await _once()


def _insert_run_record(
    db: Database, *, run_id: str, ctx: RouterContext, master_id: str,
    prompt_version_id: str | None, cap_user_name: str, context_id: str,
    user_text: str, final_output: str, routed: list[str],
    tool_calls_log: list[dict], status: str, error_code: str | None,
    error_detail: str | None, started, finished,  # noqa: ANN001
    timing: dict | None = None,
) -> None:
    """脱敏后落 run_record（§10.5: 原文不进库）。"""
    redacted_input = redact_text(user_text)
    db.execute(
        """
        INSERT INTO eag_run_record
            (run_id, trace_id, root_task_id, parent_invocation_id,
             invocation_id, master_id, prompt_version_id, cap_user_name,
             context_id, input_text, input_digest, final_output,
             routed_services, tool_calls, iterations, status, error_code,
             error_detail, timing_json, started_at, finished_at)
        VALUES (:rid, :tid, :rtid, :pid, :iid, :mid, :pvid, :user, :ctx,
                :inp, :dg, :out, :routed, :calls, :iters, :status, :ecode,
                :err, :timing, :st, :fin)
        """,
        {
            "rid": run_id, "tid": ctx.trace_id, "rtid": ctx.root_task_id,
            "pid": ctx.parent_invocation_id, "iid": ctx.invocation_id,
            "mid": master_id,
            "pvid": prompt_version_id or "",
            "user": cap_user_name, "ctx": context_id,
            "inp": redacted_input, "dg": digest_of(redacted_input),
            "out": redact_text(final_output),
            "routed": json.dumps(routed, ensure_ascii=False),
            "calls": json.dumps(tool_calls_log, ensure_ascii=False),
            "iters": len(routed),
            "status": status, "ecode": error_code, "err": error_detail,
            "timing": json.dumps(timing, ensure_ascii=False) if timing else None,
            "st": started, "fin": finished,
        },
    )


def _flow_inputs(service_parameters: dict | None, binding: dict) -> dict:
    """调用方经 services Data Part 给该服务的完整 Inputs（与 HiAgentTool 同口径）。"""
    return (service_parameters or {}).get(binding["slug"], {}).get("inputs", {})


def _select_flow_target(
    db: Database, *, master_id: str, context_id: str, subject_id: str, release: bool,
) -> dict | None:
    """本轮是否直接交给某个流程型绑定（不经模型）。

    本期规则：该会话有未过期的活跃流程，且其绑定对当前用户仍可见、仍是 FLOW。
    升级点：跨子 Agent 切换时，把这里换成每轮一次的结构化路由器即可，
    派发、原样返回、卡片与审计都不需要改。"""
    if not subject_id:
        return None
    key = {"master_id": master_id, "context_id": context_id, "subject_id": subject_id}
    if release:
        active_flow.clear(db, **key)
        db.commit()
        return None
    row = active_flow.get(db, **key)
    if row is None:
        return None
    for binding in resolve_visible_bindings(db, master_id, subject_id):
        if binding["binding_id"] == row["binding_id"] and active_flow.is_flow_binding(binding):
            return binding
    active_flow.clear(db, **key)
    db.commit()
    return None


async def run_master(
    db: Database,
    *,
    master_id: str,
    cap_user_name: str | None,
    context_id: str,
    user_text: str,
    trace_ctx: RouterContext | None = None,
    real_name: str | None = None,
    service_parameters: dict | None = None,
    stream_sink=None,
    card_submission: dict | None = None,
    raw_text: str | None = None,
    flow_release: bool = False,
) -> dict:
    """执行一次总控 Agent 回复，落调用事件与 run_record，返回结果 dict。

    A2A 入站会先建立 RuntimePrincipal，因此正常运行时 cap_user_name 是已
    校验的权威工号，real_name 来自本地用户目录或有效管理台 Session。
    参数仍保留可空以兼容内部单元调用；空值按 anonymous 且无服务权限处理。
    Master 只接受根请求（publisher 已拒绝嵌套血缘），因此这里的身份
    即本链路的权威身份——统一 replace 盖章到 ctx；None 保持 None，
    出站 child_call_headers 不注入空头、不伪造身份。"""
    master = get_master(db, master_id)
    if not master or not master["enabled"]:
        raise ValueError(f"Agent Team 不可用: {master_id}")
    if card_submission is not None:
        from .card_submission import run_confirmation
        from types import SimpleNamespace
        ctx = replace(trace_ctx or RouterContext.new_root(),
                      cap_user_name=cap_user_name, real_name=real_name)
        return await run_confirmation(
            db, master_id=master_id, context_id=context_id,
            principal=SimpleNamespace(user_id=cap_user_name, display_name=real_name),
            text=user_text, data=card_submission, ctx=ctx, stream_sink=stream_sink,
        )
    # 流程型绑定一律收用户原话（publisher 传入 framing 之前的文本）
    dispatch_text = user_text if raw_text is None else raw_text
    flow_binding = _select_flow_target(
        db, master_id=master_id, context_id=context_id,
        subject_id=cap_user_name or "", release=flow_release,
    )
    if flow_binding is not None:
        from types import SimpleNamespace

        from .direct_dispatch import dispatch
        return await dispatch(
            db, master_id=master_id, context_id=context_id,
            principal=SimpleNamespace(user_id=cap_user_name, display_name=real_name),
            text=dispatch_text, inputs=_flow_inputs(service_parameters, flow_binding),
            ctx=replace(trace_ctx or RouterContext.new_root(),
                        cap_user_name=cap_user_name, real_name=real_name),
            resolve_binding=lambda: flow_binding, stream_sink=stream_sink,
            audit_meta={"flow": "active"},
        )
    react_cfg = json.loads(master["react_config_json"] or "{}")
    max_iters = int(react_cfg.get("max_iters", settings.default_max_iters))
    response_mode = react_cfg.get("response_mode", "SYNTHESIZE")
    if response_mode not in {"SYNTHESIZE", "SINGLE_TOOL_RAW"}:
        response_mode = "SYNTHESIZE"

    user_key = cap_user_name or "anonymous"
    ctx = replace(
        trace_ctx or RouterContext.new_root(),
        cap_user_name=cap_user_name,
        real_name=real_name,
    )
    # P3 一次读取（from_hermes_a2a/03）: 文本在本 run 解析一次，
    # middleware 不再每轮查 DB；publish 新版本后下一次 run 生效
    prompt_text, prompt_version_id = get_published_prompt(db, master_id)
    # DEV-01: 分层记忆注入——用户偏好 + 总控经验（跨会话召回；记忆内容
    # 仅为参考信息，不是指令）。匿名调用跳过记忆读写（review P1-2）：
    # 共享 "anonymous" 池会形成跨调用方信息泄露面，且匿名无可见服务，
    # 记忆对匿名没有意义。
    mem_section = ""
    if cap_user_name:
        from . import memory as memory_mod

        mem_section = memory_mod.memory_digest(
            db, cap_user_name=cap_user_name, master_id=master_id,
        )
    if mem_section:
        prompt_text = prompt_text + "\n\n" + mem_section

    # R1 跨 Agent 防环（from_hermes_a2a/02）: 同 context 超过轮数上限直接
    # 拒绝，不进入 Agent 循环；落 REJECTED run_record 供审计。
    if not turn_tracker.allow_turn(context_id):
        run_id = new_id("run_")
        started = now()
        output = (
            f"连续对话轮数超过上限（{settings.max_pingpong_turns}），"
            "请新建上下文或稍后再试。"
        )
        redacted_input = redact_text(user_text)
        db.execute(
            """
            INSERT INTO eag_run_record
                (run_id, trace_id, root_task_id, parent_invocation_id,
                 invocation_id, master_id, prompt_version_id, cap_user_name,
                 context_id, input_text, input_digest, final_output,
                 routed_services, tool_calls, iterations, status, error_code,
                 error_detail, started_at, finished_at)
            VALUES (:rid, :tid, :rtid, :pid, :iid, :mid, '', :user, :ctx,
                    :inp, :dg, :out, '[]', '[]', 0, 'REJECTED',
                    'ANTI_LOOP_REJECTED', NULL, :st, :st)
            """,
            {"rid": run_id, "tid": ctx.trace_id, "rtid": ctx.root_task_id,
             "pid": ctx.parent_invocation_id, "iid": ctx.invocation_id,
             "mid": master_id, "user": user_key, "ctx": context_id,
             "inp": redacted_input, "dg": digest_of(redacted_input),
             "out": redact_text(output), "st": started},
        )
        db.commit()
        return {
            "run_id": run_id,
            "status": "REJECTED",
            "error_code": "ANTI_LOOP_REJECTED",
            "output": output,
            "routed_services": [],
            "elapsed_ms": 0,
            "prompt_version_id": "",
            "trace": ctx.to_dict(),
            "unavailable_tools": {},
        }

    # 可见集合用原始身份（匿名传空）：防止环境映射里出现 "anonymous"
    # 字面量时把服务授权给所有匿名调用（review P1-2）
    bindings = resolve_visible_bindings(db, master_id, cap_user_name or "")
    targets, unavailable = resolve_tool_targets(
        db, master_id, bindings, cap_user_name=user_key,
    )
    # KB 只读工具（from_llm_wiki 01 §8.2/T17）：绑定即授权；
    # 仅 READY 且 current_rev 非空的 KB 进入工具箱（fail-closed）
    kb_tools = build_kb_tools(db, master_id)
    # DEV-05: 外部服务只读工具（外部 KB 问答 / 通用 REST，SSRF 守卫出站）
    from ..external.adapter import build_external_tools

    ext_tools = build_external_tools(db, master_id)

    run_id = new_id("run_")
    started = now()
    t0 = time.monotonic()
    # 阶段耗时埋点（run_timing）: 总时长之外，把「模型在想」和「工具在跑」
    # 分开量，否则一次 30s 的运行在账本里只看得到几十毫秒的出站调用
    timeline = run_timing.RunTimeline()

    # 模型解析（master-model-config 专题）: per-master 独立配置优先（脱钩单一
    # 模型），否则回退全局智能路由（M5）+ fallback 链
    model_cfg = parse_model_config(master.get("model_config_json"))
    if model_config_usable(model_cfg):
        model, _scripted = build_chat_model_from_config(model_cfg)
    else:
        model, _scripted = build_chat_model_for(user_text)
    run_timing.instrument_model(model, timeline)
    tool_lines = [f"{b['tool_name']}: {b['tool_description']}" for b in targets]
    tool_lines += [f"{t.name}: {t.description}" for t in kb_tools]
    tool_lines += [f"{t.name}: {t.description}" for t in ext_tools]
    # service_parameters 统一参数约定：提示词协调——只注入 key 名清单（值不进
    # 提示词），模型不再追问/转写已结构化提供的参数（见 service_parameters.py）
    hint = ""
    if service_parameters:
        from ..hiagent.query_extends import args_hint

        hint = args_hint(service_parameters)
    middleware = DbPromptMiddleware(
        prompt_text=prompt_text, tool_lines=tool_lines, args_hint=hint,
    )

    status = "SUCCEEDED"
    error_code: str | None = None
    error_detail: str | None = None
    error_source: str | None = None
    error_service: str | None = None
    error_retryable = False
    final_text = ""
    input_required = False
    final_answer_degraded = False
    final_answer_degrade_reason = None
    routed: list[str] = []
    tool_calls_log: list[dict] = []
    hiagent_results: list[dict] = []
    cards: list[dict] = []

    ledger.append_event(
        db,
        event_type=ledger.INVOCATION_STARTED,
        status="RUNNING",
        invocation_id=ctx.invocation_id,
        trace_id=ctx.trace_id,
        root_task_id=ctx.root_task_id,
        parent_invocation_id=ctx.parent_invocation_id,
        workspace_id=ctx.workspace_id,
        master_id=master_id,
        started_at=started,
    )

    try:
        managed_client = ManagedA2AClient(
            db, instance_directory=MysqlInstanceDirectory(db),
        )
        # 装配分支（hiagent-integration/01 §3.5）：绑定指向 HIAGENT 来源
        # 时用原生直连 HiAgentTool（治理面对齐），其余走 A2ATool
        from ..hiagent.tool import HiAgentTool
        from .handoff_tool import HandoffTool

        tools: list = []
        handoff_requests: list[dict] = []
        tool_targets = {b["tool_name"]: b for b in targets}
        for b in targets:
            if active_flow.is_flow_binding(b):
                # 流程型：模型只选目标，原话与原样结果由循环后的直接派发负责
                tools.append(HandoffTool(b, requests=handoff_requests))
            elif b.get("source_type") == "HIAGENT":
                tools.append(HiAgentTool(
                    b["tool_name"],
                    b["tool_description"],
                    db=db,
                    context=ctx,
                    service_id=b["service_id"],
                    service_slug=b["slug"],
                    release=b["release"],
                    master_id=master_id,
                    workspace_id=b.get("workspace_id"),
                    context_id=context_id,
                    allowed=True,  # 进入 Toolkit 的都已在可见集合内
                    service_parameters=service_parameters,
                    result_sink=hiagent_results,
                ))
            else:
                tools.append(A2ATool(
                    b["tool_name"],
                    b["tool_description"],
                    managed_client=managed_client,
                    release=b["release"],
                    context=ctx,
                    service_slug=b["slug"],
                    master_id=master_id,
                    workspace_id=b["workspace_id"],
                    allowed=True,
                ))
        tools += kb_tools + ext_tools
        state = get_or_create_state(context_id)
        # DEV-01: LRU 未命中时从持久层恢复（重启/多副本多轮不丢）
        if len(state.context) == 0:
            restored = memory_mod.load_session(db, context_id)
            if restored is not None:
                state = restored
        # 多轮对话下 state.context 是该 context 的全部历史。下面提取路由轨迹时
        # 必须只看本轮新追加的消息，否则上一轮的 tool_result 会被重复计入
        # routed_services / tool_calls，iterations 也跟着虚高。
        history_start = len(state.context)
        state.permission_context = PermissionContext(
            mode=PermissionMode.DONT_ASK,
        )
        agent = Agent(
            name=f"master-{master['slug']}",
            system_prompt="",
            model=model,
            toolkit=Toolkit(tools=tools),
            middlewares=[middleware],
            state=state,
            react_config=ReActConfig(max_iters=max_iters),
        )
        with run_timing.use_timeline(timeline):
            final_msg = await _reply_with_backoff(
                agent,
                Msg(
                    name="user",
                    role="user",
                    content=[TextBlock(text=user_text)],
                ),
                backoff_s=settings.llm_retry_backoff_s,
                timeout_s=settings.master_timeout_s,
            )
        final_text = final_msg.get_text_content() or ""
        # 从上下文提取路由轨迹 + 工具失败态（P0-5 兜底，见循环后判定）
        # 只扫本轮新增的消息（见 history_start 处注释）
        failed_tools: list[tuple[str, str]] = []
        current_tool_results: list = []
        for m in state.context[history_start:]:
            for blk in m.content:
                if getattr(blk, "type", None) == "tool_result":
                    current_tool_results.append(blk)
                    routed.append(blk.name)
                    output = blk.output
                    if isinstance(output, list):
                        output = " ".join(
                            getattr(x, "text", "") or "" for x in output
                        )
                    tool_calls_log.append(
                        {"tool": blk.name, "state": str(blk.state),
                         "output": redact_text((output or "")[:500])},
                    )
                    if str(blk.state) != str(ToolResultState.SUCCESS):
                        failed_tools.append((blk.name, output or ""))
        if handoff_requests:
            # 流程型接管整轮：模型文本与其他工具结果只留在 tool_calls 审计里，
            # 用户原话直接派发，下游文本与卡片原样返回（不经 SYNTHESIZE）
            from types import SimpleNamespace

            from .direct_dispatch import call_flow

            flow_binding = handoff_requests[0]
            outcome = await call_flow(
                db, master_id=master_id, context_id=context_id,
                principal=SimpleNamespace(user_id=cap_user_name, display_name=real_name),
                binding=flow_binding, text=dispatch_text,
                inputs=_flow_inputs(service_parameters, flow_binding), ctx=ctx,
                stream_sink=stream_sink, audit_meta={"flow": "handoff"},
            )
            status, error_code = outcome.status, outcome.error_code
            final_text, cards = outcome.output, outcome.cards
            input_required = outcome.input_required
            if error_code:
                error_detail = outcome.output[:1000]
                error_source, error_service = "HIAGENT", flow_binding["slug"]
            tool_calls_log.append({
                "tool": flow_binding["tool_name"], "state": f"handoff:{status}",
                "output": redact_text(final_text[:500]),
            })
            if state.context and state.context[-1].role == "assistant":
                state.context[-1].content = [TextBlock(text=final_text)]
            active_flow.after_dispatch(
                db, master_id=master_id, context_id=context_id, subject_id=cap_user_name,
                binding=flow_binding, status=status, cards=cards,
                input_required=input_required,
            )
        else:
            # R3 澄清握手: 解析并剥离 [INPUT_REQUIRED] 标记
            input_required, final_text = extract_input_required(final_text)
            # HiAgent's structured result is authoritative even if the model omits
            # the textual marker. This does not introduce an execution checkpoint.
            pending_hiagent = [tool.last_result for tool in tools
                               if isinstance(tool, HiAgentTool) and tool.last_result
                               and tool.last_result.input_required]
            if pending_hiagent:
                input_required = True
                final_text = "\n\n".join(result.text for result in pending_hiagent)
            if agent.state.cur_iter >= max_iters:
                status = "LIMIT_REACHED"
            if failed_tools:
                # P0-5 fail-closed 兜底: AgentScope Toolkit 会把工具异常吞成
                # state=ERROR 的 tool_result（异常不传播到本函数），A2ATool
                # 失败时以 "[错误码] 文案" 携带语义；凡上下文存在非成功态
                # 工具结果（error/denied/interrupted…）即 FAILED，绝不伪装成功。
                tool_name, fail_text = failed_tools[0]
                marker_code, marker_detail = _extract_tool_error(fail_text)
                marker_known = is_runtime_error_code(marker_code)
                status = "FAILED"
                error_code = _recover_tool_error_code(fail_text)
                error_detail = redact_text(f"{tool_name}: {fail_text}")[:1000]
                target = tool_targets.get(tool_name) or {}
                error_service = target.get("slug")
                if target.get("source_type") == "HIAGENT":
                    error_source = "HIAGENT"
                error_retryable = runtime_error_is_retryable(error_code)
                safe_detail = (
                    marker_detail[:1024]
                    if error_source == "HIAGENT"
                    and error_code == "HIAGENT_INPUTS_INVALID"
                    else None
                )
                final_text = (
                    runtime_error_message(
                        error_code,
                        service=error_service,
                        detail=safe_detail,
                    )
                    if marker_known else "处理失败，请稍后重试。"
                )
            if response_mode == "SINGLE_TOOL_RAW":
                has_structured_output = any(
                    isinstance(tool, HiAgentTool)
                    and tool.last_result
                    and tool.last_result.cards
                    for tool in tools
                )
                raw_output = _single_tool_raw_output(
                    current_tool_results,
                    input_required=input_required,
                    status=status,
                    has_structured_output=has_structured_output,
                )
                if raw_output is not None:
                    final_text = raw_output
            if status == "SUCCEEDED" and not input_required \
                    and response_mode == "SYNTHESIZE":
                candidate = final_text
                try:
                    from .final_answer import render_final_answer

                    async with asyncio.timeout(settings.master_timeout_s or None):
                        final_text, emitted = await render_final_answer(
                            user_text=user_text, candidate=candidate,
                            model_config=model_cfg, timeline=timeline,
                            on_text=stream_sink.push if stream_sink is not None else None,
                        )
                    if stream_sink is not None and emitted and emitted != final_text:
                        await stream_sink.replace(final_text, {"run_id": run_id})
                except Exception as exc:  # noqa: BLE001 - candidate remains authoritative
                    logger.warning("final answer generation failed run_id=%s", run_id,
                                   exc_info=True)
                    final_text = candidate
                    final_answer_degraded = True
                    final_answer_degrade_reason = type(exc).__name__
                    if stream_sink is not None:
                        await stream_sink.replace(candidate, {"run_id": run_id,
                                                              "final_answer_degraded": True})
                if state.context and state.context[-1].role == "assistant":
                    state.context[-1].content = [TextBlock(text=final_text)]
            if status == "SUCCEEDED" and not input_required:
                from .card_submission import issue_cards
                cards = issue_cards(db, hiagent_results, master_id=master_id,
                                    context_id=context_id, subject_id=user_key)
        # Persist only the authoritative answer, never the ReAct candidate.
        if cap_user_name:
            memory_mod.save_session(
                db, state, master_id=master_id, cap_user_name=cap_user_name,
            )
    except asyncio.CancelledError:
        # R5（from_hermes_a2a/02）: 上游取消（a2a tasks/cancel 经
        # producer_task.cancel() 传播）→ 落 CANCELED 记录后原样上抛，
        # 不伪造成功文本
        canceled_timing = timeline.summary(int((time.monotonic() - t0) * 1000))
        ledger.append_event(
            db,
            event_type=ledger.INVOCATION_FINISHED,
            status="CANCELED",
            invocation_id=ctx.invocation_id,
            trace_id=ctx.trace_id,
            root_task_id=ctx.root_task_id,
            parent_invocation_id=ctx.parent_invocation_id,
            workspace_id=ctx.workspace_id,
            master_id=master_id,
            error_code="CANCELED",
            started_at=started,
            finished_at=now(),
            metadata=_finish_metadata(
                canceled_timing["totalMs"], canceled_timing,
            ),
        )
        _insert_run_record(
            db, run_id=run_id, ctx=ctx, master_id=master_id,
            prompt_version_id=prompt_version_id,
            cap_user_name=user_key, context_id=context_id,
            user_text=user_text, final_output="请求已取消。",
            routed=routed, tool_calls_log=tool_calls_log,
            status="CANCELED", error_code="CANCELED",
            error_detail="上游取消（tasks/cancel）",
            started=started, finished=now(),
            timing=canceled_timing,
        )
        db.commit()
        raise
    except EAgentError as exc:
        # P0-5 fail-closed: 明确错误码，不把失败伪装成成功
        status = "FAILED"
        error_code = exc.code
        error_detail = exc.message[:1000]
        error_retryable = runtime_error_is_retryable(error_code)
        final_text = (
            runtime_error_message(error_code)
            if is_runtime_error_code(error_code)
            else "处理失败，请稍后重试。"
        )
    except Exception as exc:  # noqa: BLE001
        status = "FAILED"
        wrapped = _find_eagent_error(exc)
        if wrapped is not None:
            error_code = wrapped.code
            error_detail = wrapped.message[:1000]
            error_retryable = runtime_error_is_retryable(error_code)
            final_text = (
                runtime_error_message(error_code)
                if is_runtime_error_code(error_code)
                else "处理失败，请稍后重试。"
            )
        else:
            error_code = "INTERNAL_ERROR"
            error_detail = str(exc)[:1000]
            final_text = "处理失败，请稍后重试。"
        logger.exception("run_master failed run_id=%s", run_id)

    finished = now()
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    timing = timeline.summary(elapsed_ms)
    if final_answer_degraded:
        timing["finalAnswer"] = {"degraded": True,
                                 "reason": final_answer_degrade_reason}
    ledger.append_event(
        db,
        event_type=ledger.INVOCATION_FINISHED,
        status=status,
        invocation_id=ctx.invocation_id,
        trace_id=ctx.trace_id,
        root_task_id=ctx.root_task_id,
        parent_invocation_id=ctx.parent_invocation_id,
        workspace_id=ctx.workspace_id,
        master_id=master_id,
        error_code=error_code,
        started_at=started,
        finished_at=finished,
        metadata=_finish_metadata(elapsed_ms, timing),
    )
    _insert_run_record(
        db, run_id=run_id, ctx=ctx, master_id=master_id,
        prompt_version_id=prompt_version_id,
        cap_user_name=user_key, context_id=context_id,
        user_text=user_text, final_output=final_text,
        routed=routed, tool_calls_log=tool_calls_log,
        status=status, error_code=error_code, error_detail=error_detail,
        started=started, finished=finished, timing=timing,
    )
    db.commit()
    return {
        "run_id": run_id,
        "status": status,
        "error_code": error_code,
        "error_detail": error_detail,
        "error_source": error_source,
        "error_service": error_service,
        "error_retryable": error_retryable,
        "output": final_text,
        "cards": cards if status == "SUCCEEDED" else [],
        "input_required": input_required,
        "final_answer_degraded": final_answer_degraded,
        "routed_services": routed,
        # 逐次工具调用轨迹（与 run_record.tool_calls 同源，已 redact）。
        # 管理台试运行要展示"调了什么、返回了什么"，靠 routed_services 只有
        # 工具名，定位不了误路由的原因。
        "tool_calls": tool_calls_log,
        "elapsed_ms": elapsed_ms,
        # 阶段耗时拆解（LLM 每轮 / 工具每次 / 其余），形状见 run_timing.summary
        "timing": timing,
        "prompt_version_id": prompt_version_id,
        "trace": ctx.to_dict(),
        "unavailable_tools": unavailable,
    }
