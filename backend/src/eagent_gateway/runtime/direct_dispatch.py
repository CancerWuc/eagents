# -*- coding: utf-8 -*-
"""不经模型的直接派发：原话发给 HIAGENT 绑定，文本与卡片原样返回。

两层：
- ``call_flow``：一次受治理的子调用（AGENT_CALL_* 账本、invoke、卡片签发、
  错误码映射）。首轮 handoff 由 run_master 在自己的 invocation 内调用它。
- ``dispatch``：一整轮独立回复（INVOCATION_* 账本、run_record、会话记忆、
  活跃流程维护），供卡片提交和活跃流程直达使用。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from agentscope.message import Msg, TextBlock

from ..errors import EAgentError
from ..persistence.db import new_id, now
from ..router import ledger
from ..router.context import new_invocation_id
from ..security.redact import redact_text
from . import active_flow
from .card_submission import CardSubmissionError, issue_cards


@dataclass
class FlowOutcome:
    status: str = "FAILED"
    output: str = ""
    cards: list = field(default_factory=list)
    input_required: bool = False
    error_code: str | None = None
    called: bool = False


def _error_outcome(exc: BaseException, submission: dict | None) -> FlowOutcome:
    if submission is not None and submission.get("claimed") \
            and not isinstance(exc, CardSubmissionError):
        return FlowOutcome(error_code="CARD_EXECUTION_UNCERTAIN",
                           output="无法确认业务执行结果，请核实业务状态，勿重复提交。")
    if isinstance(exc, EAgentError):
        return FlowOutcome(error_code=exc.code, output=exc.message)
    return FlowOutcome(error_code="INTERNAL_ERROR",
                       output="卡片处理失败，未发起业务执行。" if submission is not None
                       else "处理失败，请稍后重试。")


async def call_flow(db, *, master_id, context_id, principal, binding, text, inputs, ctx,
                    stream_sink=None, submission=None, audit_meta=None) -> FlowOutcome:
    """一次子调用；下游失败转成 FlowOutcome，只有取消会向上抛出。"""
    from ..hiagent.executor import invoke

    child = {"invocation_id": new_invocation_id(), "trace_id": ctx.trace_id,
             "root_task_id": ctx.root_task_id, "parent_invocation_id": ctx.invocation_id,
             "master_id": master_id, "service_id": binding["service_id"]}
    meta = dict(audit_meta or {})
    ledger.append_event(db, event_type=ledger.AGENT_CALL_STARTED, status="RUNNING",
                        metadata=meta, **child)
    outcome = FlowOutcome(called=True)
    try:
        result = await invoke(db, binding["service_id"], context_id, text, principal, inputs,
                              on_text=stream_sink.push if stream_sink else None,
                              submission=submission)
        if result.input_required and submission is not None:
            raise CardSubmissionError("CARD_INTERACTION_UNSUPPORTED",
                                      "下游要求任务续接，本接口暂不支持；请核实业务状态")
        cards = [] if result.input_required else issue_cards(
            db, [{"result": result, "service_id": binding["service_id"],
                  "service_slug": binding["slug"]}],
            master_id=master_id, context_id=context_id, subject_id=principal.user_id)
        outcome = FlowOutcome(status="SUCCEEDED", output=result.text, cards=cards,
                              input_required=result.input_required, called=True)
    except asyncio.CancelledError:
        outcome = FlowOutcome(error_code="CANCELED", called=True, status="CANCELED",
                              output="请求已取消，请核实业务状态，勿重复提交。")
        if stream_sink:
            await stream_sink.clear()
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to a stable code
        outcome = _error_outcome(exc, submission)
        outcome.called = True
        if stream_sink:
            await stream_sink.clear()
    finally:
        ledger.append_event(db, event_type=ledger.AGENT_CALL_FINISHED, status=outcome.status,
                            error_code=outcome.error_code, metadata=meta, **child)
    return outcome


def remember_turn(db, *, master_id, context_id, subject_id, text, output) -> None:
    """把直达的一轮写进总控会话记忆（与 ReAct 轮次同一份历史）。"""
    from . import master_service, memory

    state = master_service.get_or_create_state(context_id)
    if not state.context:
        restored = memory.load_session(db, context_id)
        if restored is not None:
            state = restored
            master_service._sessions[context_id] = state
    state.context.extend([
        Msg(name="user", role="user", content=[TextBlock(text=redact_text(text))]),
        Msg(name="assistant", role="assistant", content=[TextBlock(text=redact_text(output))]),
    ])
    memory.save_session(db, state, master_id=master_id, cap_user_name=subject_id)


async def dispatch(db, *, master_id, context_id, principal, text, inputs, ctx,
                   resolve_binding, stream_sink=None, submission=None,
                   audit_meta=None) -> dict:
    """一整轮直达回复。resolve_binding() 在任何下游调用前执行，可抛出拒绝。"""
    from .master_service import _insert_run_record

    run_id, started = new_id("run_"), now()
    if submission is not None:
        submission["run_id"] = run_id
    meta = {**(audit_meta or {}), "runId": run_id}
    audit = {"invocation_id": ctx.invocation_id, "trace_id": ctx.trace_id,
             "root_task_id": ctx.root_task_id, "parent_invocation_id": ctx.parent_invocation_id,
             "master_id": master_id}
    ledger.append_event(db, event_type=ledger.INVOCATION_STARTED, status="RUNNING",
                        metadata=meta, **audit)
    binding, outcome = None, FlowOutcome()
    try:
        binding = resolve_binding()
        outcome = await call_flow(db, master_id=master_id, context_id=context_id,
                                  principal=principal, binding=binding, text=text,
                                  inputs=inputs, ctx=ctx, stream_sink=stream_sink,
                                  submission=submission, audit_meta=audit_meta)
        if outcome.status == "SUCCEEDED":
            remember_turn(db, master_id=master_id, context_id=context_id,
                          subject_id=principal.user_id, text=text, output=outcome.output)
    except asyncio.CancelledError:
        outcome = FlowOutcome(status="CANCELED", error_code="CANCELED", called=binding is not None,
                              output="请求已取消，请核实业务状态，勿重复提交。")
        raise
    except Exception as exc:  # noqa: BLE001 - rejections before any downstream call
        outcome = _error_outcome(exc, submission)
        if stream_sink:
            await stream_sink.clear()
    finally:
        dispatched = submission["claimed"] if submission is not None else outcome.called
        if submission is not None and submission["claimed"]:
            db.execute(
                "UPDATE eag_master_card_submission SET state=:st, finished_at=:ts, error_code=:err"
                " WHERE card_id=:cid AND submission_run_id=:rid",
                {"st": "SUCCEEDED" if outcome.status == "SUCCEEDED" else "UNCERTAIN",
                 "ts": now(), "err": outcome.error_code, "cid": submission["card_id"],
                 "rid": run_id},
            )
            db.commit()
        if binding is not None and dispatched:
            active_flow.after_dispatch(
                db, master_id=master_id, context_id=context_id, subject_id=principal.user_id,
                binding=binding, status=outcome.status, cards=outcome.cards,
                input_required=outcome.input_required)
        ledger.append_event(db, event_type=ledger.INVOCATION_FINISHED, status=outcome.status,
                            error_code=outcome.error_code, metadata=meta, **audit)
        _insert_run_record(
            db, run_id=run_id, ctx=ctx, master_id=master_id, prompt_version_id=None,
            cap_user_name=principal.user_id, context_id=context_id, user_text=text,
            final_output=outcome.output,
            routed=[binding["tool_name"]] if binding and dispatched else [],
            tool_calls_log=[{**(audit_meta or {}), "service": binding["slug"],
                             "state": outcome.status}] if binding else [],
            status=outcome.status, error_code=outcome.error_code,
            error_detail=outcome.output if outcome.error_code else None,
            started=started, finished=now(),
        )
        db.commit()
    return {"run_id": run_id, "status": outcome.status, "output": outcome.output,
            "cards": outcome.cards if outcome.status == "SUCCEEDED" else [],
            "error_code": outcome.error_code, "error_retryable": False, "error_source": "HIAGENT",
            "error_service": binding["slug"] if binding else None,
            "routed_services": [binding["tool_name"]] if binding and dispatched else [],
            "input_required": outcome.input_required, "trace": ctx.to_dict()}
