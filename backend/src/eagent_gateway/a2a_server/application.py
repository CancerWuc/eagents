"""A2A 1.0-only application, authorized before SDK task creation."""
from __future__ import annotations

import uuid
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.agent_execution import AgentExecutor
from a2a.types import Task, TaskStatus, TaskState
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError, ExtensionSupportRequiredError
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..persistence.db import Database
from ..permission.service import visible_service_ids
from ..security.runtime_identity import (
    IdentityRequiredError,
    SubjectNotAvailableError,
    resolve_runtime_principal,
)
from ..hiagent.query_extends import (
    INPUTS_EXTENSION, MASTER_EXTENSION, INPUTS_MEDIA, MASTER_MEDIA, RESPONSE_MEDIA,
    extract_files, extract_parameters, validate_values,
    extract_card_submission, CARD_SUBMIT_MEDIA, CARD_SUBMIT_EXTENSION,
    extract_flow_release, FLOW_MEDIA, FLOW_EXTENSION,
)


class PrincipalUser(User):
    def __init__(self, principal):
        self.principal = principal

    @property
    def is_authenticated(self):
        return True

    @property
    def user_name(self):
        return self.principal.user_id


class ContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request):
        context = super().build(request)
        principal = request.state.a2a_principal
        context.user = PrincipalUser(principal)
        context.state["principal"] = principal
        return context


class Authentication(BaseHTTPMiddleware):
    def __init__(self, app, service_id=None):
        super().__init__(app)
        self.service_id = service_id

    async def dispatch(self, request, call_next):
        if request.url.path.endswith("/.well-known/agent-card.json"):
            return await call_next(request)
        db = Database()
        try:
            headers = {str(k).lower(): str(v) for k, v in request.headers.items()}
            principal = resolve_runtime_principal(db, headers)
            console_page = headers.get("x-eagent-console-page", "").strip()
            if console_page:
                # 管理台中的真实 A2A 试运行仍走运行面协议，但必须同时具备
                # 对应页面的 manage 权限。外部 A2A 调用不带该头，不受影响。
                from ..permission.page_permissions import effective_permissions, is_allowed
                from ..security import console_auth
                raw = console_auth.bearer_from_headers(headers)
                session = console_auth.auth_context_from_token(db, raw) if raw else None
                if session is None or not is_allowed(
                    effective_permissions(
                        db, session.user_id, is_admin=session.is_admin(),
                    ),
                    console_page, "manage",
                ):
                    return JSONResponse(
                        {"code": "FORBIDDEN",
                         "message": f"缺少页面权限: {console_page}:manage"},
                        status_code=403,
                    )
            if self.service_id and self.service_id not in visible_service_ids(
                db, principal.user_id,
            ):
                return JSONResponse(
                    {"code": "SERVICE_FORBIDDEN", "message": "无权调用该服务"},
                    status_code=403,
                )
        except IdentityRequiredError as exc:
            return JSONResponse(
                {"code": "IDENTITY_REQUIRED", "message": str(exc)},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        except SubjectNotAvailableError as exc:
            return JSONResponse(
                {"code": "SUBJECT_NOT_AVAILABLE", "message": str(exc)},
                status_code=403,
            )
        finally:
            db.close()
        request.state.a2a_principal = principal
        if request.headers.get("a2a-version") != "1.0":
            try:
                request_id = (await request.json()).get("id")
            except ValueError:
                request_id = None
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32009, "message": "VersionNotSupportedError: A2A-Version 1.0 required"}})
        return await call_next(request)


class ValidatedHandler(DefaultRequestHandler):
    def __init__(self, *, schema=None, allowed_services=None, file_base_url=None, **kwargs):
        super().__init__(**kwargs)
        self.schema = schema
        self.allowed_services = allowed_services
        self.file_base_url = file_base_url
        # Store ownership survives remounts together with the in-memory TaskStore.
        if not hasattr(self.task_store, "context_owners"):
            self.task_store.context_owners = {}

    def _prepare_message(self, params, context):
        message = MessageToDict(params.message)
        try:
            master = self.schema is None
            submission = extract_card_submission(message)
            if submission is not None:
                if not master:
                    raise ValueError("Card submission is only supported by master endpoints")
                if not message.get("contextId") or message.get("taskId"):
                    raise ValueError("Card submission requires the original contextId and no taskId")
                if CARD_SUBMIT_EXTENSION not in context.requested_extensions:
                    raise ExtensionSupportRequiredError("Card submission extension was not activated")
            if master and extract_flow_release(message):
                if FLOW_EXTENSION not in context.requested_extensions:
                    raise ExtensionSupportRequiredError("Flow control extension was not activated")
            values = extract_parameters(message, master=master)
            extension = MASTER_EXTENSION if master else INPUTS_EXTENSION
            if not master or values:
                if extension not in context.requested_extensions:
                    raise ExtensionSupportRequiredError("Required extension was not activated")
            texts = [p for p in message.get("parts", []) if "text" in p]
            responses = [p for p in message.get("parts", []) if p.get("mediaType") == RESPONSE_MEDIA]
            if len(texts) + len(responses) != 1:
                raise ValueError("Exactly one text or interaction response is required")
            extract_files(message, base_url=self.file_base_url)
            allowed = {INPUTS_MEDIA if not master else MASTER_MEDIA, RESPONSE_MEDIA, "text/plain"}
            data_media = {INPUTS_MEDIA if not master else MASTER_MEDIA, RESPONSE_MEDIA}
            if master:
                allowed.update({CARD_SUBMIT_MEDIA, FLOW_MEDIA})
                data_media.update({CARD_SUBMIT_MEDIA, FLOW_MEDIA})
            for part in message.get("parts", []):
                if "url" in part or "raw" in part:
                    continue
                if part.get("mediaType", "text/plain") not in allowed:
                    raise ValueError("Unsupported input media type")
                if "data" in part and part.get("mediaType") not in data_media:
                    raise ValueError("Data Parts require their declared extension media type")
                if "text" in part and part.get("mediaType", "text/plain") != "text/plain":
                    raise ValueError("Text Parts require text/plain")
            if texts and (not texts[0]["text"].strip() or len(texts[0]["text"].encode()) > 32768):
                raise ValueError("Text must be nonempty and at most 32 KiB")
            if not master:
                validate_values(values, self.schema)
            elif any(not isinstance(v, dict) or set(v) != {"inputs"} for v in values.values()):
                raise ValueError("Service parameters must contain inputs")
            elif self.allowed_services is not None and not set(values) <= self.allowed_services:
                raise ValueError("Parameters contain a service that is not bound")
            if responses and not params.message.task_id:
                raise ValueError("An interaction response requires taskId")
        except ValueError as exc:
            raise InvalidParamsError(str(exc)) from exc
        owner = context.user.user_name
        owners = self.task_store.context_owners
        cid = params.message.context_id
        if cid and owners.get(cid) != owner:
            raise TaskNotFoundError("Context is not accessible")
        if not cid:
            cid = uuid.uuid4().hex
            params.message.context_id = cid
            owners[cid] = owner
        if hasattr(self.agent_executor, "validate_interaction"):
            self.agent_executor.validate_interaction(params.message, owner)

    async def on_message_send(self, params, context=None):
        self._prepare_message(params, context)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params, context=None):
        # The SDK uses a separate handler for SSE.  Apply exactly the same
        # validation and owner assignment before it creates a Task.
        self._prepare_message(params, context)
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def _check_task_owner(self, task_id, context):
        task = await self.task_store.get(task_id, context)
        if task is None or self.task_store.context_owners.get(task.context_id) != context.user.user_name:
            raise TaskNotFoundError("Task is not accessible")

    async def on_get_task(self, params, context=None):
        await self._check_task_owner(params.id, context)
        return await super().on_get_task(params, context)

    async def on_cancel_task(self, params, context=None):
        await self._check_task_owner(params.id, context)
        return await super().on_cancel_task(params, context)

    async def on_subscribe_to_task(self, params, context=None):
        await self._check_task_owner(params.id, context)
        async for event in super().on_subscribe_to_task(params, context):
            yield event


class TaskLifecycle(AgentExecutor):
    """Publish the initial Task required by the SDK's v1 event contract."""
    def __init__(self, executor):
        self.executor = executor

    def validate_interaction(self, message, owner):
        if hasattr(self.executor, "validate_interaction"):
            self.executor.validate_interaction(message, owner)

    async def execute(self, context, event_queue):
        if context.current_task is None:
            await event_queue.enqueue_event(Task(id=context.task_id, context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        await self.executor.execute(context, event_queue)

    async def cancel(self, context, event_queue):
        await self.executor.cancel(context, event_queue)


def build_application(card, executor, *, task_store=None, schema=None, allowed_services=None,
                      file_base_url=None):
    handler = ValidatedHandler(agent_executor=TaskLifecycle(executor), agent_card=card,
                               task_store=task_store or InMemoryTaskStore(), schema=schema,
                               allowed_services=allowed_services, file_base_url=file_base_url)
    async def get_card(request):
        return JSONResponse(MessageToDict(card))
    app = Starlette(routes=[Route("/.well-known/agent-card.json", get_card),
                            *create_jsonrpc_routes(handler, "/rpc", ContextBuilder(), enable_v0_3_compat=False)])
    app.add_middleware(Authentication, service_id=getattr(executor, "_service_id", None))
    return app
