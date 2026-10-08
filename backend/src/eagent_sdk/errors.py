# -*- coding: utf-8 -*-
"""eAgent SDK 类型化错误（from_nekiro/12 §S2 / B4）。

- 统一错误模型 EAgentPlatformError: code / message / traceId / http_status。
- 不暴露原始响应体（B4）; 服务端返回 platform-error 结构时直接映射。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EAgentPlatformError(Exception):
    code: str
    message: str
    trace_id: str | None = None
    http_status: int | None = None

    def __str__(self) -> str:  # pragma: no cover - 展示友好
        base = f"[{self.code}] {self.message}"
        return f"{base} (traceId={self.trace_id})" if self.trace_id else base


class EAgentTimeoutError(EAgentPlatformError):
    def __init__(self, message: str = "调用超时"):
        super().__init__(code="TIMEOUT", message=message)


class EAgentPayloadTooLargeError(EAgentPlatformError):
    def __init__(self, message: str = "请求/响应超过大小限制"):
        super().__init__(code="PAYLOAD_TOO_LARGE", message=message)


class EAgentConnectionError(EAgentPlatformError):
    def __init__(self, message: str = "网关连接失败"):
        super().__init__(code="DEPENDENCY_ERROR", message=message)


def from_http_error(status: int, payload: dict | None) -> EAgentPlatformError:
    """把网关 HTTP 错误映射为类型化错误（读 platform-error 结构，不携带原文）。"""
    payload = payload if isinstance(payload, dict) else {}
    return EAgentPlatformError(
        code=str(payload.get("code") or f"HTTP_{status}"),
        message=str(payload.get("message") or "网关返回错误"),
        trace_id=payload.get("traceId"),
        http_status=status,
    )
