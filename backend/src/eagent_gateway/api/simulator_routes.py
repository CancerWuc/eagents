"""Admin-session-only A2A simulator. All execution traverses the real A2A app."""
from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import re
import time
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import Response, StreamingResponse

from .. import errors
from ..a2a_server import mounts
from ..permission.service import visible_service_ids
from ..persistence.db import Database, new_id
from ..security import console_auth

logger = logging.getLogger("eagent.simulator")
router = APIRouter(prefix="/api/v1/admin/simulator", tags=["simulator"])
MAX_REQUEST_BYTES = 1024 * 1024


def require_admin_session(request: Request) -> str:
    # Revalidate the session/roles per request; no header-token fallback.
    token = console_auth.bearer_token(request)
    if not token:
        raise errors.UnauthenticatedError("模拟中心需要管理员登录会话")
    db = Database()
    try:
        ctx = console_auth.auth_context_from_token(db, token)
        if ctx is None:
            raise errors.UnauthenticatedError("登录会话无效或已过期")
        if not ctx.is_admin():
            raise errors.ForbiddenError("模拟中心仅管理员可用")
        return ctx.user_id
    finally:
        db.close()


def _subject(db: Database, user_id: str) -> dict:
    user = db.query_one(
        "SELECT user_id, display_name, status FROM eag_console_user WHERE user_id=:u",
        {"u": user_id},
    )
    if not user or user["status"] != "active":
        raise errors.ForbiddenError("被模拟用户未登记或已禁用")
    return user


def _targets(db: Database) -> list[dict]:
    rows = db.query_all(
        "SELECT master_id AS id, slug, display_name AS name, 'MASTER' AS kind "
        "FROM eag_master_agent WHERE enabled=1 "
        "UNION ALL SELECT service_id AS id, slug, display_name AS name, 'HIAGENT' AS kind "
        "FROM eag_a2a_service_registration WHERE enabled=1 AND source_type='HIAGENT'",
    )
    return sorted(
        [r for r in rows if mounts.slug_mounted(r["slug"])],
        key=lambda r: (r["kind"], r["slug"]),
    )


@router.get("/targets")
def list_targets(
    asUser: str = Query(min_length=1, max_length=64),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=10, ge=1, le=200),
    q: str = Query(default="", max_length=200),
    kind: Literal["MASTER", "HIAGENT"] | None = None,
    operator: str = Depends(require_admin_session),
):
    db = Database()
    try:
        _subject(db, asUser)
        visible = set(visible_service_ids(db, asUser))
        rows = [r for r in _targets(db) if (not kind or r["kind"] == kind)
                and q.casefold() in (r["name"] + " " + r["slug"]).casefold()]
        items = []
        for row in rows[(page - 1) * pageSize:page * pageSize]:
            available = row["kind"] == "MASTER" or row["id"] in visible
            items.append({
                **row, "cardPath": f"/a2a/{row['slug']}/.well-known/agent-card.json",
                "available": available,
                "unavailableReason": None if available else "该用户无服务调用权限",
            })
        return {"items": items, "total": len(rows), "page": page, "pageSize": pageSize}
    finally:
        db.close()


def _audit(operator: str, subject: str, slug: str, action: str, detail: dict) -> None:
    db = Database()
    try:
        db.execute(
            "INSERT INTO eag_audit_event "
            "(audit_id, cap_user_name, action, resource_type, resource_id, detail) "
            "VALUES (:id, :op, :action, 'agent_simulator', :slug, :detail)",
            {"id": new_id(), "op": operator, "action": action, "slug": slug,
             "detail": json.dumps({**detail, "subjectUserId": subject}, ensure_ascii=False)},
        )
        db.commit()
    finally:
        db.close()


@router.post("/{slug}/rpc")
async def invoke(
    slug: str, request: Request,
    asUser: str = Query(min_length=1, max_length=64),
    source: Literal["user", "debug"] = "user",
    operator: str = Depends(require_admin_session),
):
    # Never construct an arbitrary URL or accept client-controlled forwarding headers.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise errors.ValidationError("Agent 标识不合法")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise errors.ValidationError("模拟请求不能超过 1 MiB")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise errors.ValidationError("请求必须为 JSON-RPC 对象") from None
    if not isinstance(body, dict) or body.get("method") not in {
        "SendMessage", "SendStreamingMessage", "GetTask", "CancelTask", "SubscribeToTask",
    }:
        raise errors.ValidationError("模拟中心不支持该 A2A 方法")
    db = Database()
    try:
        user = _subject(db, asUser)
        target = next((r for r in _targets(db) if r["slug"] == slug), None)
        if target is None:
            raise errors.NotFoundError("Agent 未启用或未挂载 A2A 端点")
    finally:
        db.close()

    trace_id = new_id("trc_")
    simulation_id = new_id("sim_")
    detail = {
        "simulationId": simulation_id, "traceId": trace_id, "source": source,
        "targetId": target["id"], "kind": target["kind"],
        # Client IDs are arbitrary input: correlate by digest rather than logging values.
        "requestIdDigest": hashlib.sha256(json.dumps(body.get("id")).encode()).hexdigest(),
    }
    _audit(operator, asUser, slug, "simulator.started", detail)  # fail closed
    started = time.monotonic()
    headers = {
        "content-type": "application/json", "cap-user-name": user["user_id"],
        "x-eagent-trace-id": trace_id,
    }
    for name in ("a2a-version", "a2a-extensions"):
        if name in request.headers:
            headers[name] = request.headers[name]
    if body["method"] in {"SendStreamingMessage", "SubscribeToTask"}:
        return await _stream_rpc(request, slug, raw, headers, detail, operator, asUser,
                                 trace_id, simulation_id, started)
    response = None
    audit_failed = False
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request.app, raise_app_exceptions=False),
            base_url="http://simulator.internal", follow_redirects=False,
        ) as client:
            response = await client.post(f"/a2a/{slug}/rpc", content=raw, headers=headers)
    finally:
        result = {**detail, "elapsedMs": round((time.monotonic() - started) * 1000),
                  "httpStatus": response.status_code if response is not None else None,
                  "outcome": "interrupted"}
        if response is not None:
            result["outcome"] = "http_error" if response.is_error else "response"
            try:
                envelope = response.json()
                wrapper = envelope.get("result") or {}
                task = wrapper.get("task") or (wrapper if wrapper.get("status") else {})
                result["outcome"] = task.get("status", {}).get("state") or result["outcome"]
                result["a2aErrorCode"] = (envelope.get("error") or {}).get("code")
                for key in ("id", "contextId"):
                    if task.get(key):
                        result["taskId" if key == "id" else key] = task[key]
            except (ValueError, AttributeError, TypeError):
                pass
        try:
            _audit(operator, asUser, slug, "simulator.finished", result)
        except Exception:
            # Execution may already have changed business data. Preserve its response,
            # explicitly report the audit problem; never induce an automatic retry.
            logger.exception("simulator completion audit failed: %s", simulation_id)
            audit_failed = True
    output_headers = {name: response.headers[name] for name in ("content-type", "x-trace-id")
                      if name in response.headers}
    output_headers["X-Simulation-Id"] = simulation_id
    output_headers["X-Simulation-Trace-Id"] = trace_id
    if audit_failed:
        output_headers["X-Simulation-Audit-Warning"] = "completion-audit-failed"
    return Response(response.content, status_code=response.status_code, headers=output_headers)


async def _stream_rpc(request: Request, slug: str, raw: bytes, headers: dict,
                      detail: dict, operator: str, subject: str, trace_id: str,
                      simulation_id: str, started: float) -> StreamingResponse:
    """Forward ASGI messages as produced; httpx.ASGITransport buffers the entire body."""
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
    response_start = asyncio.get_running_loop().create_future()
    detached = asyncio.Event()
    scope = dict(request.scope)
    scope.update({
        "type": "http", "path": f"/a2a/{slug}/rpc", "raw_path": f"/a2a/{slug}/rpc".encode(),
        "query_string": b"", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    })
    delivered = False
    status = None
    event_tail = ""
    outcome = "interrupted"
    task_id = None
    context_id = None

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Future()

    async def send(message):
        nonlocal status, event_tail, outcome, task_id, context_id
        if message["type"] == "http.response.start":
            status = message["status"]
            if not response_start.done():
                response_start.set_result(message)
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk and not detached.is_set():
                await queue.put(chunk)
            if chunk:
                event_tail += chunk.decode("utf-8", errors="replace")
                event_tail = event_tail.replace("\r\n", "\n")
                while "\n\n" in event_tail:
                    frame, event_tail = event_tail.split("\n\n", 1)
                    for line in frame.splitlines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:])
                            result = event.get("result") or {}
                            task = result.get("task") or {}
                            update = result.get("statusUpdate") or {}
                            if task:
                                task_id, context_id = task.get("id"), task.get("contextId")
                            if update.get("status", {}).get("state"):
                                outcome = update["status"]["state"]
                            elif task.get("status", {}).get("state"):
                                outcome = task["status"]["state"]
                            elif event.get("error"):
                                outcome = "rpc_error"
                        except (ValueError, TypeError, AttributeError):
                            pass

    async def run():
        nonlocal outcome
        try:
            await request.app(scope, receive, send)
        except Exception:
            logger.exception("simulator streamed execution failed: %s", simulation_id)
            outcome = "interrupted"
            if not response_start.done():
                response_start.set_result({"status": 500, "headers": []})
        finally:
            if not detached.is_set():
                await queue.put(None)
            audit = {**detail, "elapsedMs": round((time.monotonic() - started) * 1000),
                     "httpStatus": status, "outcome": outcome}
            if task_id:
                audit["taskId"] = task_id
            if context_id:
                audit["contextId"] = context_id
            try:
                _audit(operator, subject, slug, "simulator.finished", audit)
            except Exception:
                logger.exception("simulator completion audit failed: %s", simulation_id)

    asyncio.create_task(run())
    begin = await response_start
    output_headers = {k.decode(): v.decode() for k, v in begin.get("headers", [])
                      if k.lower() in {b"content-type", b"x-trace-id"}}
    output_headers.update({"X-Simulation-Id": simulation_id,
                           "X-Simulation-Trace-Id": trace_id,
                           "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def chunks():
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            detached.set()
            while not queue.empty():
                queue.get_nowait()

    return StreamingResponse(chunks(), status_code=begin["status"], headers=output_headers)
