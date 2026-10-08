# -*- coding: utf-8 -*-
"""运行时 A2A 工具: 把一个已解析的 Release 目标包装成 AgentScope ToolBase。

P0-3/P0-5 之后:
- 出站统一经 ManagedA2AClient（本文件不再直接创建 A2AClient/Resolver，
  也不再持有远端 URL——业务侧只感知 release 与服务 slug）。
- fail-closed: 调用失败转成带 `[错误码]` 前缀、state=ERROR 的 ToolChunk
  （AgentScope Toolkit 会吞掉工具异常、不会向上传播），由 run_master 扫描
  tool_result 非成功态兜底，记录 status=FAILED 与 error_code（07 文档语义）。

约束（§5.4）: 只挂 A2A 工具，绝不注册文件工具；
check_permissions 按「本请求可见集合」做纵深防御（§6.3）。
"""
from agentscope.permission import (
    PermissionBehavior,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk
from agentscope.message import TextBlock, ToolResultState

from ..errors import EAgentError
from ..router.client import ManagedA2AClient
from ..router.context import RouterContext
from . import run_timing

# 工具失败文本的错误码标记（如 "[DEPENDENCY_ERROR] 远端连接失败"）。
# AgentScope Toolkit 会把工具异常转成 state=ERROR 的 tool_result 且只保留
# 文本（metadata 不透传），错误码只能经内容文本带回 run_master。
def error_code_marker(code: str) -> str:
    return f"[{code}]"


class A2ATool(ToolBase):
    """一次 A2A 请求构建一次 Toolkit（§5.2），实例本身无跨请求状态。"""

    is_concurrency_safe = True
    is_read_only = False  # 远端可能有写副作用

    def __init__(
        self,
        tool_name: str,
        description: str,
        *,
        managed_client: ManagedA2AClient,
        release: dict,
        context: RouterContext,
        service_slug: str,
        master_id: str | None = None,
        workspace_id: str | None = None,
        allowed: bool = True,
    ) -> None:
        self.name = tool_name
        self.description = description
        self.input_schema = {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "发给该服务的用户请求原文",
                },
            },
            "required": ["message"],
        }
        self._client = managed_client
        self._release = release
        self._context = context
        self._service_slug = service_slug
        self._master_id = master_id
        self._workspace_id = workspace_id
        self._allowed = allowed

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
        try:
            # 阶段埋点: 账本里已有出站 elapsed_ms，这里再记一次是为了让
            # run_record 的 LLM/工具/其余三段能对上总时长（run_timing）
            with run_timing.record(run_timing.PHASE_TOOL, self.name):
                text = await self._client.send_message(
                    release=self._release,
                    context=self._context,
                    user_text=message,
                    capability=self.name,
                    master_id=self._master_id,
                    workspace_id=self._workspace_id,
                )
        except EAgentError as exc:
            # AgentScope Toolkit（_toolkit.py except Exception）会把工具抛出
            # 的异常吞掉、转成 state=ERROR 的 tool_result，异常不会传播到
            # run_master——因此在这里转成带错误码标记的 ERROR 结果，
            # 由 run_master 兜底恢复 fail-closed 语义（P0-5）。
            return ToolChunk(
                content=[TextBlock(
                    text=f"{error_code_marker(exc.code)} {exc.message}",
                )],
                state=ToolResultState.ERROR,
                metadata={
                    "service": self._service_slug,
                    "release_version": self._release["agent_version"],
                    "error_code": exc.code,
                },
            )
        return ToolChunk(
            content=[TextBlock(text=text)],
            # 不暴露远端地址，仅暴露治理语义（服务 slug 与 release 版本）
            metadata={
                "service": self._service_slug,
                "release_version": self._release["agent_version"],
            },
        )
