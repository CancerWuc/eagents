# -*- coding: utf-8 -*-
"""平台调用上下文（from_nekiro/03 §4.3、04 §5）。

trace_id 贯穿一次端到端调用；invocation_id 标识一次调用（总控运行或
一次远端 Agent 调用）；root_task_id 标识根任务；parent_invocation_id
维护嵌套调用的父子关系。出站时注入平台上下文头，入站时继承。

用户身份（cap-user-name / real-name，a2a 规范 §3）经入站目录校验后由
Executor 盖章到链路快照，再由 child_call_headers 透传给下一跳，保证最终
执行 agent 知道"是替谁在办事"。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ..security.identity import (
    CAP_USER_HEADER,
    REAL_NAME_HEADER,
    outbound_header_value,
)

TRACE_HEADER = "X-EAgent-Trace-Id"
INVOCATION_HEADER = "X-EAgent-Invocation-Id"
ROOT_TASK_HEADER = "X-EAgent-Root-Task-Id"
PARENT_INVOCATION_HEADER = "X-EAgent-Parent-Invocation-Id"
WORKSPACE_HEADER = "X-EAgent-Workspace-Id"

PLATFORM_HEADERS = (
    TRACE_HEADER,
    INVOCATION_HEADER,
    ROOT_TASK_HEADER,
    PARENT_INVOCATION_HEADER,
    WORKSPACE_HEADER,
)


def new_trace_id() -> str:
    return "trc_" + uuid.uuid4().hex


def new_invocation_id() -> str:
    return "inv_" + uuid.uuid4().hex


def new_root_task_id() -> str:
    return "task_" + uuid.uuid4().hex


@dataclass
class RouterContext:
    """一次调用链的上下文快照（不可变语义，传递时不修改）。"""

    trace_id: str
    invocation_id: str
    root_task_id: str
    parent_invocation_id: str | None = None
    workspace_id: str | None = None
    # 用户身份（a2a 规范 §3 透传）；None=本跳没有身份（不注入出站头）
    cap_user_name: str | None = None
    real_name: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def new_root(
        cls,
        *,
        workspace_id: str | None = None,
        cap_user_name: str | None = None,
        real_name: str | None = None,
    ) -> "RouterContext":
        """入口请求没有平台上下文头时，生成全新链路。"""
        return cls(
            trace_id=new_trace_id(),
            invocation_id=new_invocation_id(),
            root_task_id=new_root_task_id(),
            workspace_id=workspace_id,
            cap_user_name=cap_user_name,
            real_name=real_name,
        )

    @classmethod
    def from_inbound_headers(
        cls,
        headers: dict | None,
        *,
        workspace_id: str | None = None,
    ) -> "RouterContext":
        """入站继承（from_nekiro/03 §4.4 嵌套调用）。

        - 继承 trace_id / root_task_id（同一条链）
        - invocation_id 沿用调用方在头里分配的值（同一调用在两端账本
          中是同一个 invocation）；头缺失时才生成新值
        - parent = 头里的 X-EAgent-Parent-Invocation-Id（调用方的 invocation）
        - 头缺失/为空时退化为全新链路（外部入口请求）
        """
        h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        trace_id = h.get(TRACE_HEADER.lower(), "").strip()
        root_task_id = h.get(ROOT_TASK_HEADER.lower(), "").strip()
        invocation_id = h.get(INVOCATION_HEADER.lower(), "").strip()
        # 身份不在这里从原始头提取；A2A 入站先完成目录校验，再由 Executor
        # 把权威 Principal 显式盖章到 RouterContext。
        cap_user, real_name = None, None
        if not trace_id or not root_task_id:
            return cls.new_root(
                workspace_id=workspace_id,
                cap_user_name=cap_user or None,
                real_name=real_name,
            )
        return cls(
            trace_id=trace_id,
            invocation_id=invocation_id or new_invocation_id(),
            root_task_id=root_task_id,
            parent_invocation_id=h.get(
                PARENT_INVOCATION_HEADER.lower(), "",
            ).strip() or None,
            workspace_id=h.get(WORKSPACE_HEADER.lower(), "").strip()
            or workspace_id,
            cap_user_name=cap_user or None,
            real_name=real_name,
        )

    def child_call_headers(
        self, child_invocation_id: str, *, workspace_id: str | None = None,
    ) -> dict[str, str | bytes]:
        """出站调用注入的平台上下文头（子调用获得新 invocation_id）。

        用户身份按 a2a 规范透传（canonical 连字符）；本跳没有身份时
        不注入空头，不伪造身份。"""
        headers = {
            TRACE_HEADER: self.trace_id,
            INVOCATION_HEADER: child_invocation_id,
            ROOT_TASK_HEADER: self.root_task_id,
            PARENT_INVOCATION_HEADER: self.invocation_id,
            WORKSPACE_HEADER: workspace_id or self.workspace_id or "",
        }
        if self.cap_user_name:
            headers[CAP_USER_HEADER] = outbound_header_value(self.cap_user_name)
        if self.real_name:
            headers[REAL_NAME_HEADER] = outbound_header_value(self.real_name)
        return headers

    def to_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "invocationId": self.invocation_id,
            "rootTaskId": self.root_task_id,
            "parentInvocationId": self.parent_invocation_id,
            "workspaceId": self.workspace_id,
        }
