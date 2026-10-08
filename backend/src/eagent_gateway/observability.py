# -*- coding: utf-8 -*-
"""请求级 trace 上下文（P0-1 落地基础）。

每个请求进入即确定 trace_id（沿用入站 X-EAgent-Trace-Id，否则生成），
写入 request.state、回写响应头 X-Trace-Id，并确保 scope headers 里有
X-EAgent-Trace-Id / X-EAgent-Root-Task-Id —— 这样 A2A 子应用的入站上下文
（RouterContext.from_inbound_headers）与 HTTP 层 trace 天然同源，
外部调用方的 trace 查询（GET /api/v1/traces/{id}）不会分叉。

P0-3 Router 托管调用与 P0-4 Invocation Ledger 在此之上传播
invocation_id / root_task_id 血缘。
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .persistence.db import new_id
from .router.context import ROOT_TASK_HEADER, TRACE_HEADER

RESPONSE_TRACE_HEADER = "X-Trace-Id"


class TraceContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        trace_id = (
            request.headers.get(TRACE_HEADER)
            or request.headers.get(RESPONSE_TRACE_HEADER)
            or new_id("trc_")
        )
        request.state.trace_id = trace_id
        _ensure_platform_headers(request, trace_id)
        response = await call_next(request)
        response.headers[RESPONSE_TRACE_HEADER] = trace_id
        return response


def _ensure_platform_headers(request: Request, trace_id: str) -> None:
    """把 trace/root 注入 scope headers（已存在则不覆盖）。

    A2A SDK 子应用会从 scope headers 捕获 call_context.state["headers"]，
    因此这里注入后，RouterContext.from_inbound_headers 能拿到同一 trace。
    注意: ASGI 规范要求 scope headers key 为小写字节。"""
    headers = list(request.scope.get("headers") or [])
    existing = {k.lower() for k, _ in headers}
    trace_key = TRACE_HEADER.lower().encode("latin-1")
    root_key = ROOT_TASK_HEADER.lower().encode("latin-1")
    if trace_key not in existing:
        headers.append((trace_key, trace_id.encode("latin-1")))
    if root_key not in existing:
        headers.append((root_key, new_id("task_").encode("latin-1")))
    request.scope["headers"] = headers
