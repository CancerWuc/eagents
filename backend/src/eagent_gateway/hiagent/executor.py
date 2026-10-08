"""HiAgent A2A 1.0 executor. Task ownership is enforced by the SDK store."""
from __future__ import annotations
import asyncio
import hashlib
import threading
import time
import uuid
from contextlib import aclosing, asynccontextmanager
from eagent_gateway.a2a_server.protocol import data_part

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict
from ..a2a_server.application import build_application
from ..a2a_server.streaming import ReplyArtifactStream
from ..errors import (
    EAgentError, GatewayTimeoutError, HiAgentConversationBusyError,
    HiAgentInputsValidationError,
)
from ..persistence.db import Database, now as db_now
from ..router import RouterContext, ledger
from ..security.outbound_redact import redact_outbound
from . import service as hia_svc
from .card import build_hiagent_card, parse_agent_config
from .normalizer import normalize_stream
from .query_extends import (
    CANVAS_CARD_MEDIA, EMPTY_SCHEMA, INTERACTION_MEDIA, RESPONSE_MEDIA,
    files_from_context, parameters_from_context,
)


_LOCK_WAIT_S = 5.0
_conversation_locks: dict[tuple[str, str, str], tuple[asyncio.Lock, int]] = {}
_conversation_locks_guard = threading.Lock()


@asynccontextmanager
async def _hold_conversation_lock(key: tuple[str, str, str]):
    """Process-local update→chat critical section; entries are ref-counted."""
    with _conversation_locks_guard:
        lock, references = _conversation_locks.get(key, (asyncio.Lock(), 0))
        _conversation_locks[key] = (lock, references + 1)
    acquired = False
    try:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_LOCK_WAIT_S)
        except TimeoutError as exc:
            raise HiAgentConversationBusyError() from exc
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        with _conversation_locks_guard:
            current = _conversation_locks.get(key)
            if current and current[0] is lock:
                if current[1] <= 1:
                    _conversation_locks.pop(key, None)
                else:
                    _conversation_locks[key] = (lock, current[1] - 1)


async def invoke(db, service_id, context_id, text, principal, inputs, files=None, *, on_text=None,
                 submission=None):
    try:
        async with asyncio.timeout(120):
            key = (context_id, service_id, principal.user_id)
            async with _hold_conversation_lock(key):
                from .card import parse_agent_config
                from .query_extends import validate_values

                side = hia_svc.get_hiagent_config(db, service_id) or {}
                config = parse_agent_config(side.get("agent_config_json"))
                version = hia_svc.configuration_version(side)
                digest = hia_svc.identity_digest(principal.user_id, principal.display_name)
                try:
                    validate_values(inputs, config.get("inputsSchema", EMPTY_SCHEMA))
                except ValueError as exc:
                    raise HiAgentInputsValidationError(str(exc)) from exc
                client = hia_svc.build_client(db, service_id)
                if submission is not None:
                    # Validation and claim occur under the same update→chat lock.
                    from ..runtime.card_submission import claim
                    conversation = claim(db, submission, principal, context_id, version, digest)
                else:
                    conversation = await hia_svc.get_or_create_conversation(
                        db, service_id, context_id, inputs=inputs,
                        cap_user_name=principal.user_id,
                        real_name=principal.display_name)
                await client.update_conversation(
                    conversation,
                    inputs=inputs,
                    cap_user_name=principal.user_id,
                    real_name=principal.display_name,
                    conversation_name=(
                        "eAgent-" + hashlib.sha256(context_id.encode()).hexdigest()[:12]
                    ),
                )
                async with aclosing(client.chat_stream(
                    conversation, redact_outbound(text),
                    cap_user_name=principal.user_id,
                    real_name=principal.display_name,
                    files=files,
                )) as events:
                    result = await normalize_stream(events, on_text=on_text)
                    result.conversation_id = conversation
                    result.identity_digest = digest
                    result.config_version = version
                    return result
    except TimeoutError as exc:
        raise GatewayTimeoutError("HiAgent 调用超时") from exc



class HiAgentExecutor(AgentExecutor):
    def __init__(self, service_id, pending=None, *, file_base_url=None):
        self._service_id = service_id
        self._file_base_url = file_base_url
        self.pending = pending if pending is not None else {}
        self.running = {}
        self.canceled = set()

    def validate_interaction(self, message, owner):
        if not message.task_id:
            return
        key = (owner, message.task_id)
        pending = self.pending.get(key)
        if not pending or pending["contextId"] != message.context_id or pending["expires"] < time.time():
            raise InvalidParamsError("No pending interaction for this task")
        parts = MessageToDict(message).get("parts", [])
        replies = [p for p in parts if p.get("mediaType") == RESPONSE_MEDIA]
        if len(replies) != 1:
            raise InvalidParamsError("A pending interaction requires exactly one interaction response")
        data = replies[0].get("data", {})
        if data.get("interactionId") != pending["interactionId"]:
            raise InvalidParamsError("Interaction ID mismatch")
        ids = data.get("selectedOptionIds")
        if not isinstance(ids, list) or len(ids) != 1 or ids[0] not in pending["options"]:
            raise InvalidParamsError("Select exactly one valid option")
        if pending.get("claimed"):
            raise InvalidParamsError("Interaction already submitted")
        pending["claimed"] = True

    async def execute(self, context: RequestContext, event_queue):
        principal = context.call_context.state["principal"]
        key = (principal.user_id, context.task_id)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        trace = RouterContext.from_inbound_headers(context.call_context.state.get("headers", {}))
        reply = ReplyArtifactStream(updater, {
            "serviceId": self._service_id,
            "traceId": trace.trace_id,
        })
        self.running[context.task_id] = asyncio.current_task()
        db = Database()
        started = db_now()
        outcome, error_code = "FAILED", None
        try:
            ledger.append_event(db, event_type=ledger.INVOCATION_STARTED, status="RUNNING",
                invocation_id=trace.invocation_id, trace_id=trace.trace_id,
                root_task_id=trace.root_task_id, parent_invocation_id=trace.parent_invocation_id,
                service_id=self._service_id, started_at=started)
            text = context.get_user_input() or ""
            previous = self.pending.pop(key, None)
            if previous:
                replies = [p for p in MessageToDict(context.message).get("parts", []) if p.get("mediaType") == RESPONSE_MEDIA]
                if replies:
                    text = previous["originalQuery"]
            if context.current_task is None:
                await updater.submit()
            await updater.start_work()
            result = await invoke(db, self._service_id, context.context_id, text, principal,
                                  parameters_from_context(context),
                                  files_from_context(context, base_url=self._file_base_url),
                                  on_text=reply.push)
            if context.task_id in self.canceled:
                return
            if result.input_required:
                await reply.clear()
                outcome = "INPUT_REQUIRED"
                interaction_id = uuid.uuid4().hex
                self.pending[key] = {"interactionId": interaction_id, "contextId": context.context_id,
                    "options": {o["id"]: o["label"] for o in result.options},
                    "originalQuery": text, "expires": time.time()+172800}
                await updater.requires_input(message=updater.new_agent_message(parts=[
                    Part(text=result.text, media_type="text/plain"),
                    data_part(data={"interactionId": interaction_id, "type": "single_select",
                               "options": result.options, "allowFreeText": False}, media_type=INTERACTION_MEDIA)]))
            else:
                outcome = "SUCCEEDED"
                metadata = {
                    "serviceId": self._service_id,
                    "traceId": trace.trace_id,
                }
                if result.text.strip():
                    await reply.finish(result.text)
                if result.cards:
                    await updater.add_artifact(
                        [data_part(data=card, media_type=CANVAS_CARD_MEDIA)
                         for card in result.cards],
                        name="canvas-card", metadata=metadata,
                    )
                await updater.complete()
        except asyncio.CancelledError:
            await reply.clear()
            outcome = "CANCELED"
            if context.task_id not in self.canceled:
                await updater.cancel()
            raise
        except (EAgentError, TimeoutError, ValueError) as exc:
            await reply.clear()
            if context.task_id not in self.canceled:
                code = "HIAGENT_TIMEOUT" if isinstance(exc, TimeoutError) else getattr(exc, "code", "HIAGENT_INVALID_RESPONSE")
                error_code = code
                await updater.failed(message=updater.new_agent_message(parts=[
                    Part(text="HiAgent 执行失败", media_type="text/plain"),
                    data_part(data={"code": code, "source": "HIAGENT",
                                   "retryable": code == "HIAGENT_CONVERSATION_BUSY",
                                   "traceId": trace.trace_id}, media_type="application/json")]))
        except Exception:
            await reply.clear()
            error_code = "HIAGENT_INTERNAL_ERROR"
            if context.task_id not in self.canceled:
                await updater.failed(message=updater.new_agent_message(parts=[
                    Part(text="HiAgent 执行失败", media_type="text/plain"),
                    data_part(data={"code": error_code, "source": "HIAGENT",
                                   "traceId": trace.trace_id}, media_type="application/json"),
                ]))
        finally:
            self.running.pop(context.task_id, None)
            self.canceled.discard(context.task_id)
            try:
                ledger.append_event(db, event_type=ledger.INVOCATION_FINISHED, status=outcome,
                    invocation_id=trace.invocation_id, trace_id=trace.trace_id,
                    root_task_id=trace.root_task_id, parent_invocation_id=trace.parent_invocation_id,
                    service_id=self._service_id, error_code=error_code, started_at=started, finished_at=db_now())
            finally:
                db.close()

    async def cancel(self, context, event_queue):
        self.canceled.add(context.task_id)
        running = self.running.get(context.task_id)
        if running and running is not asyncio.current_task():
            running.cancel()
        principal = context.call_context.state["principal"]
        self.pending.pop((principal.user_id, context.task_id), None)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def build_hiagent_a2a_app(db, service, *, task_store=None):
    side = hia_svc.get_hiagent_config(db, service["service_id"]) or {}
    config = parse_agent_config(side.get("agent_config_json"))
    if task_store is not None and not hasattr(task_store, "pending_interactions"):
        task_store.pending_interactions = {}
    pending = getattr(task_store, "pending_interactions", None)
    from .client import resolve_base_url

    file_base_url = resolve_base_url(side.get("base_url"))
    executor = HiAgentExecutor(
        service["service_id"], pending, file_base_url=file_base_url,
    )
    return build_application(build_hiagent_card(service, config), executor,
        task_store=task_store, schema=config.get("inputsSchema", EMPTY_SCHEMA),
        file_base_url=file_base_url)
