# -*- coding: utf-8 -*-
"""提示词外置 + 热更新（§5.3 + from_hermes_a2a/03 P3）。

P3 一次读取: run_master 在装配前查一次 PUBLISHED 版本，把文本传入
middleware；middleware 不再访问 DB（消除每轮重复查询与重拼）。
发布新版本 = 更新指针，下一次 run 重建 middleware 立即生效（ADR-0002）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from agentscope.middleware import MiddlewareBase

from .seed_prompt import DEFAULT_MASTER_PROMPT

if TYPE_CHECKING:  # 容器（standalone）复用本 middleware，无需 DB 依赖
    from ..persistence.db import Database


def get_published_prompt(db: Database, master_id: str) -> tuple[str | None, str | None]:
    """返回 (prompt_text, prompt_version_id)；未发布时 (None, None)。"""
    row = db.query_one(
        """
        SELECT prompt_version_id, prompt_text FROM eag_master_prompt_version
         WHERE master_id=:mid AND status='PUBLISHED'
         ORDER BY version_no DESC LIMIT 1
        """,
        {"mid": master_id},
    )
    if not row:
        return None, None
    return row["prompt_text"], row["prompt_version_id"]


class DbPromptMiddleware(MiddlewareBase):
    """系统提示词 middleware（P3: 已解析文本由构造时注入，不再查 DB）。

    同一 run 内多次调用返回字节稳定的结果；可见工具清单按请求裁剪。
    args_hint 为 service_parameters 统一参数约定的提示词协调段（只含 key 名，
    仅在调用方携带结构化参数时非空，见 hiagent/service_parameters.py）。"""

    def __init__(
        self, *, prompt_text: str | None, tool_lines: list[str],
        args_hint: str = "",
    ) -> None:
        self._prompt_text = prompt_text
        self._tool_lines = tool_lines
        self._args_hint = args_hint

    async def on_system_prompt(self, agent, current_prompt: str) -> str:  # noqa: ANN001
        base = self._prompt_text or DEFAULT_MASTER_PROMPT
        listing = "\n".join(f"- {line}" for line in self._tool_lines) or "（无）"
        prompt = (
            f"{base}\n\n## 当前可用服务工具（已按用户可见集合裁剪）\n{listing}"
        )
        if self._args_hint:
            prompt = f"{prompt}\n\n{self._args_hint}"
        return prompt
