"""Generate the user-visible final answer after ReAct has finished tool work."""
from __future__ import annotations

import inspect
import json
from contextlib import aclosing

from agentscope.message import Msg, TextBlock

from . import run_timing
from .model_factory import (
    build_chat_model_for, build_chat_model_from_config, model_config_usable,
)


_SYSTEM = (
    "你是最终答复器。工具决策和调用已经完成，你不能调用工具。"
    "只根据给出的用户请求与候选答复，逐步生成面向用户的最终答复。"
    "保留候选答复的事实、数字、服务来源和不确定性，不新增事实。"
    "不要输出内部推理、工具参数、协议标记或[INPUT_REQUIRED]。"
)


async def render_final_answer(
    *, user_text: str, candidate: str, model_config: dict | None,
    timeline: run_timing.RunTimeline, on_text=None,
) -> tuple[str, str]:
    """Return (authoritative answer, already emitted delta text).

    Scripted/demo mode retains the candidate; there is no real model stream.
    Exceptions propagate so the caller can replace any provisional artifact.
    """
    if model_config_usable(model_config):
        model, scripted = build_chat_model_from_config(model_config, stream=True)
    else:
        model, scripted = build_chat_model_for(user_text, stream=True)
    if scripted:
        return candidate, ""
    run_timing.instrument_model(model, timeline)
    messages = [
        Msg(name="system", role="system", content=[TextBlock(text=_SYSTEM)]),
        Msg(name="user", role="user", content=[TextBlock(text=json.dumps({
            "request": user_text, "candidate": candidate,
        }, ensure_ascii=False))]),
    ]
    response = await model(messages, tools=None)
    emitted: list[str] = []
    final = ""
    if inspect.isasyncgen(response):
        async with aclosing(response):
            async for chunk in response:
                text = "".join(
                    block.text for block in chunk.content
                    if isinstance(block, TextBlock)
                )
                if chunk.is_last:
                    final = text
                elif text:
                    emitted.append(text)
                    if on_text is not None:
                        await on_text(text)
    else:
        final = "".join(
            block.text for block in response.content
            if isinstance(block, TextBlock)
        )
    if not final.strip() or "[INPUT_REQUIRED]" in final:
        raise ValueError("final answer model returned no usable text")
    return final, "".join(emitted)
