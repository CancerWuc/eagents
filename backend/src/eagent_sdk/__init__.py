# -*- coding: utf-8 -*-
"""eAgent Python SDK（from_nekiro/12 §S2）。

- client.EAgentClient: 业务系统调用 eAgent 网关（只面向网关，不见远端 URL）。
- agent_auth: 受管 Agent 校验 eAgent Router 凭证，拒绝直连。
- errors.EAgentPlatformError: 统一类型化错误（code/message/traceId）。

独立模块，不依赖 eagent_gateway；成熟后可拆独立包（A3 决策点再评估）。
"""
from .agent_auth import ASGIAgentAuthMiddleware, RouterCredentialGuard
from .client import EAgentClient, HiAgentFile, PlatformContext, SendResult
from .errors import (
    EAgentConnectionError,
    EAgentPayloadTooLargeError,
    EAgentPlatformError,
    EAgentTimeoutError,
    from_http_error,
)

__all__ = [
    "EAgentClient",
    "HiAgentFile",
    "PlatformContext",
    "SendResult",
    "EAgentPlatformError",
    "EAgentTimeoutError",
    "EAgentPayloadTooLargeError",
    "EAgentConnectionError",
    "from_http_error",
    "RouterCredentialGuard",
    "ASGIAgentAuthMiddleware",
]
