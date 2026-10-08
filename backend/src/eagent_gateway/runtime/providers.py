# -*- coding: utf-8 -*-
"""模型 Provider 注册表（from_hermes_a2a/04 M1）。

声明式注册: 主 provider（EAGENT_LLM_* 一组）+ EAGENT_LLM_FALLBACK 动态
注册，优先级 = 列表顺序。新增 provider 不改 build_chat_model 逻辑。
主 provider 的注册名由 EAGENT_LLM_PROVIDER_NAME 配置（默认 glm），
端点/模型分别取 EAGENT_LLM_BASE_URL / EAGENT_LLM_MODEL。

fallback spec 格式: "name:base_url:model:key"，多个以 ; 分隔。
base_url 含 scheme/port 冒号，因此解析取首尾字段、中间合并还原 URL。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("eagent.providers")


@dataclass(frozen=True)
class ModelProvider:
    name: str            # 注册名，如 "glm"/"deepseek"（routing 规则按此匹配）
    base_url: str        # OpenAI 兼容 base_url
    api_key: str = ""    # 空 = 未配置
    default_model: str = ""
    max_retries: int = 2

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.default_model)


def parse_fallback_spec(spec: str) -> tuple[str, str, str, str] | None:
    """解析 "name:base:model:key"；字段不足 4 段返回 None（跳过）。"""
    parts = spec.split(":")
    if len(parts) < 4 or not parts[0].strip():
        return None
    name = parts[0].strip()
    key = parts[-1].strip()
    model = parts[-2].strip()
    base = ":".join(parts[1:-2]).strip()
    return name, base, model, key


def list_providers() -> list[ModelProvider]:
    """主 provider 在前 + fallback 链按配置顺序。

    主 provider 的注册名来自 EAGENT_LLM_PROVIDER_NAME（config 层已兜底非空）。
    """
    from ..config import settings

    providers = [
        ModelProvider(
            settings.llm_provider_name, settings.llm_base_url,
            settings.llm_api_key, settings.llm_model,
        ),
    ]
    for spec in settings.llm_fallback:
        parsed = parse_fallback_spec(spec)
        if parsed is None:
            logger.warning("忽略无法解析的 fallback 配置: %r", spec)
            continue
        name, base, model, key = parsed
        providers.append(ModelProvider(name, base, key, model))
    return providers
