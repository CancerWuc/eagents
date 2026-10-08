# -*- coding: utf-8 -*-
"""传输层错误分类（from_nekiro/12 §S1 / A1）。

把底层 httpx / A2A SDK / 传输校验异常统一映射为平台错误码（07 表），
出站链路任何失败都有明确语义，不裸抛底层异常：
- 超时 → TIMEOUT
- 响应过大 → PAYLOAD_TOO_LARGE
- 重定向被拒 → DEPENDENCY_ERROR（明确文案）
- 畸形 JSON-RPC / id 不匹配 → DEPENDENCY_ERROR
- SSRF 拦截 → DEPENDENCY_ERROR
- 其他连接/协议错误 → DEPENDENCY_ERROR
"""
from __future__ import annotations

import httpx

from ..errors import DependencyError, GatewayTimeoutError, PayloadTooLargeError
from ..security.pinned_http import AddressBlockedError


class ResponseTooLargeError(PayloadTooLargeError):
    """响应体超过显式配置的上限。"""

    safe_message = "远端响应超过大小上限"


class RedirectRejectedError(DependencyError):
    """远端返回 3xx 重定向，已按统一出口规则拒绝。"""

    safe_message = "远端尝试重定向，已拒绝"


class MalformedRpcError(DependencyError):
    """响应不是合法 JSON-RPC envelope（缺 jsonrpc/id、id 不匹配、非法 JSON）。"""

    safe_message = "远端返回非 JSON-RPC 响应"


def classify_transport_error(exc: BaseException) -> Exception:
    """把任意出站异常映射为带明确 code 的 EAgentError（幂等：已是统一错误则原样返回）。"""
    from ..errors import EAgentError

    if isinstance(exc, EAgentError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return GatewayTimeoutError()
    if isinstance(exc, AddressBlockedError):
        return DependencyError("目标地址被 SSRF 策略拒绝")
    if isinstance(exc, ResponseTooLargeError | RedirectRejectedError
                  | MalformedRpcError):
        return exc
    if isinstance(exc, httpx.HTTPError):
        return DependencyError("远端连接失败")
    return DependencyError("远端传输失败")
