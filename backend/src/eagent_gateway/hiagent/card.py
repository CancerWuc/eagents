# -*- coding: utf-8 -*-
"""HIAGENT 来源服务的生成式 Agent Card（**不拉取远端**）。

hiagent 没有 Agent Card 概念；挂载 `/a2a/{slug}` 所需的 Card 由平台按注册
配置生成，保证通过 agent-card.v1 契约校验（catalog 同一套校验器）。
skills 素材来自注册行的 `agent_config_json.skills`，缺省给一个兜底 skill。
"""
from __future__ import annotations

import json
import hashlib

from a2a.types import AgentCapabilities, AgentCard, AgentSkill, AgentInterface
from .query_extends import (
    CANVAS_CARD_MEDIA, EMPTY_SCHEMA, INPUTS_EXTENSION, INPUTS_MEDIA,
    INTERACTION_MEDIA, RESPONSE_MEDIA,
)

from ..config import settings


def parse_agent_config(agent_config_json: str | None) -> dict:
    """注册行的 Card 素材解析（非法 JSON 按空处理，不炸挂载）。"""
    if not agent_config_json:
        return {}
    try:
        cfg = json.loads(agent_config_json)
    except ValueError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def build_hiagent_card(service: dict, agent_config: dict) -> AgentCard:
    """生成 HIAGENT 服务的 Agent Card（url 指向本网关挂载点）。"""
    skills = [
        AgentSkill(
            id=str(s.get("id") or f"skill-{i}"),
            name=str(s.get("name") or f"skill-{i}"),
            description=str(s.get("description") or s.get("name") or "HiAgent skill")[:500],
            tags=[str(t) for t in (s.get("tags") or ["hiagent"])][:8],
            examples=[str(e) for e in s.get("examples", [])],
        )
        for i, s in enumerate(agent_config.get("skills") or [])
        if isinstance(s, dict)
    ] or [
        AgentSkill(
            id="hiagent-default",
            name=str(service.get("slug") or "hiagent-agent"),
            description=str(service.get("display_name") or "")[:500]
            or "HiAgent 智能体",
            tags=["hiagent"],
        ),
    ]
    base = f"{settings.public_url.rstrip('/')}/a2a/{service['slug']}"
    return AgentCard(
        name=str(service.get("display_name") or service.get("slug") or "hiagent"),
        description=str(agent_config.get("description") or service.get("description") or "")
        or "HiAgent 接入（平台生成 Card）",
        supported_interfaces=[AgentInterface(url=f"{base}/rpc", protocol_binding="JSONRPC", protocol_version="1.0")],
        version="1.0." + str(int(hashlib.sha256(json.dumps(agent_config, sort_keys=True).encode()).hexdigest()[:8], 16)),
        default_input_modes=["text/plain", INPUTS_MEDIA, RESPONSE_MEDIA],
        default_output_modes=[
            "text/plain", "application/json", INTERACTION_MEDIA,
            CANVAS_CARD_MEDIA,
        ],
        capabilities=AgentCapabilities(streaming=settings.a2a_streaming, extensions=[{"uri": INPUTS_EXTENSION, "required": True, "params": {"mediaType": INPUTS_MEDIA, "schema": agent_config.get("inputsSchema", EMPTY_SCHEMA)}}]),
        security_schemes={
            "externalDirectoryIdentity": {"api_key_security_scheme": {
                "location": "header", "name": "cap-user-name",
                "description": "可信入口注入、且已登记启用的 eAgent 用户工号",
            }},
            "eagentSession": {"http_auth_security_scheme": {
                "scheme": "Bearer", "description": "eAgent 管理台登录会话",
            }},
        },
        security_requirements=[
            {"schemes": {"externalDirectoryIdentity": {"list": []}}},
            {"schemes": {"eagentSession": {"list": []}}},
        ],
        skills=skills,
    )
