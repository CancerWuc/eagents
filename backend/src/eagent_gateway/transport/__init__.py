# -*- coding: utf-8 -*-
"""A2A 出站传输层（from_nekiro/12 §S1 / A1）。

统一出站规则的唯一实现处；ManagedA2AClient 是唯一上游。
"""
from .client import A2ATransport
from .errors import (
    MalformedRpcError,
    RedirectRejectedError,
    ResponseTooLargeError,
    classify_transport_error,
)
from .sse import (
    SSEEvent,
    SSEInterruptedError,
    SSEProtocolError,
    consume_sse,
)

__all__ = [
    "A2ATransport",
    "classify_transport_error",
    "ResponseTooLargeError",
    "RedirectRejectedError",
    "MalformedRpcError",
    "consume_sse",
    "SSEEvent",
    "SSEProtocolError",
    "SSEInterruptedError",
]
