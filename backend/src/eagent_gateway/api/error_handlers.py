# -*- coding: utf-8 -*-
"""FastAPI 异常处理：所有非 2xx 响应统一为 platform-error.v1 结构。

EAgentError 之外，RequestValidationError / 存量 HTTPException / 未捕获异常
也在此包装，保证前端只面对一种错误格式（fail-closed：不外泄内部信息）。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import EAgentError, ValidationError

logger = logging.getLogger("eagent.errors")

_HTTP_CODE_MAP = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    502: "DEPENDENCY_ERROR",
    503: "INSTANCE_UNAVAILABLE",
    504: "TIMEOUT",
}


def _trace_id(request: Request) -> str | None:
    return getattr(request.state, "trace_id", None)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(EAgentError)
    async def _eagent_error(request: Request, exc: EAgentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={**exc.payload(), "traceId": exc.trace_id or _trace_id(request)},
        )

    @app.exception_handler(RequestValidationError)
    async def _request_invalid(
        request: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        # 只保留字段路径与规则提示，不携带原始输入值
        details = [
            {
                "field": ".".join(str(loc) for loc in err.get("loc", ())) or "$",
                "issue": str(err.get("msg", "invalid")),
            }
            for err in exc.errors()[:10]
        ]
        wrapped = ValidationError(details=details)
        return JSONResponse(
            status_code=wrapped.http_status,
            content={
                **wrapped.payload(),
                "traceId": _trace_id(request),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_compat(
        request: Request, exc: StarletteHTTPException,
    ) -> JSONResponse:
        # 存量 HTTPException 与框架内部错误（如静态资源 404）的兼容包装
        detail = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": _HTTP_CODE_MAP.get(
                    exc.status_code, f"HTTP_{exc.status_code}",
                ),
                "message": detail,
                "traceId": _trace_id(request),
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error trace_id=%s path=%s",
            _trace_id(request), request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
                "traceId": _trace_id(request),
            },
        )
