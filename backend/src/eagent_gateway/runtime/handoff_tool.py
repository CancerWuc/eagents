# -*- coding: utf-8 -*-
"""HandoffTool：流程型（FLOW）绑定在 ReAct 里的替身。

模型只用它表达"本轮交给谁"，工具本身不访问下游、不接收转述文本；
run_master 在循环结束后用用户原话直接派发（runtime/direct_dispatch），
下游的文本与卡片原样返回。
"""
from __future__ import annotations

from agentscope.message import TextBlock
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk


class HandoffTool(ToolBase):
    is_concurrency_safe = True
    is_read_only = True  # 仅记录交接请求；真正的下游调用由 run_master 执行

    def __init__(self, binding: dict, *, requests: list) -> None:
        self.name = binding["tool_name"]
        self.description = (
            f"{binding['tool_description']}\n"
            "该服务负责完整业务流程（含表单卡片与多轮办理）。涉及此类业务时必须调用本工具"
            "把请求交给它，不得自行回答、追问或改写用户问题；系统会把用户原话交给该服务并"
            "原样返回结果。调用后直接结束本轮。"
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "选择该服务的简短理由（仅审计）"},
            },
        }
        self._binding = binding
        self._requests = requests

    async def check_permissions(self, tool_input, context):  # noqa: ANN001
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message=f"{self.name} 可见")

    async def __call__(self, **_arguments) -> ToolChunk:
        # Toolkit 不按 schema 校验入参；模型多传的字段（如转述的 message）一律忽略，
        # 否则 TypeError 会让交接请求静默丢失。
        if self._binding not in self._requests:
            self._requests.append(self._binding)
        return ToolChunk(
            content=[TextBlock(text=f"已交由「{self.name}」处理，系统将原样返回其结果。请直接结束本轮。")],
            metadata={"service": self._binding["slug"], "handoff": True},
        )
