# -*- coding: utf-8 -*-
"""HiAgent 接入（hiagent-integration/01；platform-spec hiagent-topic）。

- `client`：协议客户端（四接口 + SSE 解析，异步 httpx 连接池）
- `service`：统一 Agent Catalog 的 HIAGENT 注册/探测/会话映射
- `card`：生成式 Agent Card（不拉取远端）
- `executor`：A2A `HiAgentExecutor`（message/send → chat_query_v2）
- `tool`：`HiAgentTool`（Team 混编的原生直连工具，治理面对齐 A2ATool）
"""
from .client import HiagentAPIError, HiagentClient, resolve_api_key, resolve_base_url

__all__ = [
    "HiagentAPIError",
    "HiagentClient",
    "resolve_api_key",
    "resolve_base_url",
]
