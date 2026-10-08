# -*- coding: utf-8 -*-
"""受管调用 Router 模块（from_nekiro/03）。所有 A2A 出站调用经此。"""
from .client import ManagedA2AClient
from .context import (
    INVOCATION_HEADER,
    PARENT_INVOCATION_HEADER,
    ROOT_TASK_HEADER,
    TRACE_HEADER,
    WORKSPACE_HEADER,
    RouterContext,
    new_invocation_id,
    new_root_task_id,
    new_trace_id,
)
from .resolver import resolve_target

__all__ = [
    "ManagedA2AClient",
    "RouterContext",
    "new_trace_id",
    "new_invocation_id",
    "new_root_task_id",
    "resolve_target",
    "TRACE_HEADER",
    "INVOCATION_HEADER",
    "ROOT_TASK_HEADER",
    "PARENT_INVOCATION_HEADER",
    "WORKSPACE_HEADER",
]
