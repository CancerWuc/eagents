# -*- coding: utf-8 -*-
"""KB 内部 LLM 助手（规划/生成与总控模型管线解耦，01 §6.4）。

- 走 OpenAI 兼容 /chat/completions；未配置 key 时返回 None（消费方走
  确定性降级路径——规划保守单页、生成规则式页面）。
- 出站经 SSRF 防护 transport；超时/失败抛异常由调用方按降级策略处理。
"""
from __future__ import annotations

import logging

from ..config import settings

logger = logging.getLogger("eagent.kb.llm")


def llm_configured() -> bool:
    return bool(settings.llm_api_key)


async def chat(
    *, system: str, user: str, timeout_s: float = 60.0,
    temperature: float = 0.2,
) -> str | None:
    """返回模型回复文本；未配置 LLM 返回 None。"""
    if not llm_configured():
        return None
    from ..security.pinned_http import guarded_async_client

    async with guarded_async_client(timeout=timeout_s) as client:
        resp = await client.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            json={
                "model": settings.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "stream": False,
            },
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM 返回 {resp.status_code}")
    choices = resp.json().get("choices") or []
    if not choices:
        raise RuntimeError("LLM 空回复")
    return choices[0].get("message", {}).get("content") or ""
