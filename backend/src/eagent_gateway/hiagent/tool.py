# -*- coding: utf-8 -*-
"""HiAgentTool：Team 混编里 HIA 成员的原生直连工具（hiagent-integration/01 §3.5）。

hiagent 特批允许不经过 a2a 直接接入（规范 §1 定位）；相比「HIA → A2A →
再转回」的回环路径省一层协议往返，身份直接走 body `Inputs`。**治理面必须
与 A2ATool 等价**（不允许「直连=裸奔」）：

- 账本：AGENT_CALL_STARTED / AGENT_CALL_FINISHED（service_id、elapsed_ms、
  error_code），血缘挂在本次 run 的 invocation 下；
- run_timing.record(PHASE_TOOL)（run_record 三段耗时对得上总时长）；
- 出站文本 redact_outbound（凭证形字符串不外发，R6）；
- fail-closed：失败转带 `[错误码]` 标记的 ERROR ToolChunk——AgentScope
  Toolkit 会吞工具异常，由 run_master 扫描非成功态兜底（P0-5 语义）；
- check_permissions 按「本请求可见集合」纵深防御（§6.3，同 A2ATool）。

TODO-03（单一控制流所有者）：HIA agent 对 Team 而言是黑盒（ATOMIC_AGENT
形态参与编排），本工具不展开其内部循环/重试。
"""
from __future__ import annotations

import time

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk
from agentscope.message import TextBlock, ToolResultState

from ..errors import EAgentError
from ..persistence.db import now as db_now
from ..router import ledger
from ..router.context import RouterContext, new_invocation_id
from ..runtime import run_timing
from ..runtime.a2a_tool import error_code_marker
from ..security.outbound_redact import redact_outbound
from . import service as hia_svc


class HiAgentTool(ToolBase):
    """一次 A2A 请求构建一次 Toolkit（§5.2），实例本身无跨请求状态。"""

    is_concurrency_safe = True
    is_read_only = False  # 远端可能有写副作用

    def __init__(
        self,
        tool_name: str,
        description: str,
        *,
        db,
        context: RouterContext,
        service_id: str,
        service_slug: str,
        release: dict,
        master_id: str | None = None,
        workspace_id: str | None = None,
        context_id: str = "",
        allowed: bool = True,
        service_parameters: dict | None = None,
        result_sink: list | None = None,
    ) -> None:
        self.name = tool_name
        self.description = description
        self.input_schema = {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "发给该智能体的用户请求原文",
                },
            },
            "required": ["message"],
        }
        self._db = db
        self._context = context
        self._service_id = service_id
        self._service_slug = service_slug
        self._release = release
        self._master_id = master_id
        self._workspace_id = workspace_id
        # Team 会话透传：同一 context 的多次工具调用复用同一 hiagent 会话
        self._context_id = context_id
        self._allowed = allowed
        # service_parameters 统一参数约定：调用方经 Data Part 传入的完整
        # Inputs 快照；模型不得补充或覆盖。
        self._service_parameters = service_parameters
        self.last_result = None
        self._result_sink = result_sink

    async def check_permissions(self, tool_input, context):  # noqa: ANN001
        if not self._allowed:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"{self.name} 不在当前用户可见服务集合内",
            )
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} 可见",
        )

    async def __call__(self, message: str) -> ToolChunk:
        child_invocation_id = new_invocation_id()
        started = db_now()
        t0 = time.monotonic()
        ledger.append_event(
            self._db,
            event_type=ledger.AGENT_CALL_STARTED,
            status="RUNNING",
            invocation_id=child_invocation_id,
            trace_id=self._context.trace_id,
            root_task_id=self._context.root_task_id,
            parent_invocation_id=self._context.invocation_id,
            workspace_id=self._workspace_id or self._context.workspace_id,
            master_id=self._master_id,
            service_id=self._service_id,
            started_at=started,
        )
        error: EAgentError | None = None
        text = ""
        try:
            # 阶段埋点：与 A2ATool 同口径（账本已有 elapsed_ms，此处供
            # run_record 的工具段耗时聚合）
            with run_timing.record(run_timing.PHASE_TOOL, self.name):
                # service_parameters 统一参数约定（纯配置通道）：按注册声明的
                # Inputs Schema required 校验——值只来自调用方的完整快照，
                # 不从对话或模型提取（缺失 fail-closed → ERROR chunk）
                from .query_extends import validate_values, EMPTY_SCHEMA
                from .card import parse_agent_config
                from .executor import invoke
                from types import SimpleNamespace
                cfg = hia_svc.get_hiagent_config(self._db, self._service_id) or {}
                schema = parse_agent_config(cfg.get("agent_config_json")).get("inputsSchema", EMPTY_SCHEMA)
                values = (self._service_parameters or {}).get(self._service_slug, {}).get("inputs", {})
                try:
                    validate_values(values, schema)
                except ValueError as exc:
                    from ..errors import HiAgentInputsValidationError
                    raise HiAgentInputsValidationError(str(exc)) from exc
                result = await invoke(self._db, self._service_id, self._context_id, message,
                    SimpleNamespace(user_id=self._context.cap_user_name, display_name=self._context.real_name), values)
                self.last_result = result
                if self._result_sink is not None and not result.input_required:
                    self._result_sink.append({"service_id": self._service_id,
                                              "service_slug": self._service_slug, "result": result})
                text = ("[INPUT_REQUIRED] " if result.input_required else "") + result.text
        except EAgentError as exc:
            error = exc
        finally:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            ledger.append_event(
                self._db,
                event_type=ledger.AGENT_CALL_FINISHED,
                status="FAILED" if error is not None else "SUCCEEDED",
                invocation_id=child_invocation_id,
                trace_id=self._context.trace_id,
                root_task_id=self._context.root_task_id,
                parent_invocation_id=self._context.invocation_id,
                workspace_id=self._workspace_id or self._context.workspace_id,
                master_id=self._master_id,
                service_id=self._service_id,
                error_code=getattr(error, "code", None),
                started_at=started,
                finished_at=db_now(),
                metadata={
                    "elapsed_ms": elapsed_ms,
                    "release_version": self._release["agent_version"],
                },
            )
        if error is not None:
            # Toolkit 会吞掉工具异常——错误码只能经内容文本带回 run_master（P0-5）
            return ToolChunk(
                content=[TextBlock(
                    text=f"{error_code_marker(error.code)} {error.message}",
                )],
                state=ToolResultState.ERROR,
                metadata={
                    "service": self._service_slug,
                    "release_version": self._release["agent_version"],
                    "error_code": error.code,
                },
            )
        return ToolChunk(
            content=[TextBlock(text=text)],
            # 不暴露 hiagent 地址，仅暴露治理语义
            metadata={
                "service": self._service_slug,
                "release_version": self._release["agent_version"],
                "source_type": hia_svc.SOURCE_TYPE,
                "input_required": bool(self.last_result and self.last_result.input_required),
            },
        )
