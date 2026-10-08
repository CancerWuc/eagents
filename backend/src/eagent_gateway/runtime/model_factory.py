# -*- coding: utf-8 -*-
"""模型工厂: 按 provider 注册表取第一个已配置的 OpenAI 兼容真实模型
（from_hermes_a2a/04 M1）；全部未配置时回退 ScriptedChatModel
（演示/CI 模式，不依赖外网）。

出站连接一律复用共享连接池（见 shared_http_client），别绕过它直接构造
OpenAIChatModel——那等于每轮 LLM 调用重付一次 TLS 握手。
"""
import asyncio
import json
import uuid
import weakref

from agentscope.credential import CredentialBase, OpenAICredential
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse

from ..config import settings
from . import providers


# ---------------------------------------------------------------------------
# 出站 HTTP 连接复用
# ---------------------------------------------------------------------------
# AgentScope 的 OpenAIChatModel 在 _call_api 里 new openai.AsyncClient
# （agentscope/model/_openai_chat/_model.py），也就是每一轮 LLM 调用都新建
# 一个连接池 → 每轮重付 TCP+TLS 握手，而且这些 client 从不关闭。
# 实测对 api.deepseek.com: 每轮多花约 1.2s（2709ms → 1083ms），一次 8 轮的
# ReAct 运行白扔 ~10s。
#
# 修法是把一个进程内共享的 httpx 客户端经 client_kwargs 传进去
# （OpenAIChatModel 支持转发 http_client）: AsyncClient 对象照旧每轮新建
# （纯内存开销），但底层连接池共享，握手只在第一次付。
#
# 按事件循环分桶: httpx 的连接绑定创建它的 loop，测试里 asyncio.run() 每次
# 换 loop，共用一个实例会拿到已关闭 loop 上的连接。WeakKeyDictionary 让
# loop 被回收时条目自动消失。
_http_clients: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def shared_http_client():
    """当前事件循环上的共享 httpx 客户端；无运行中的 loop 时返回 None。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 同步上下文（脚本、部分单测）: 退回每轮新建，行为与改造前一致
        return None
    client = _http_clients.get(loop)
    if client is None or client.is_closed:
        import httpx

        client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=settings.llm_http_max_connections,
                max_keepalive_connections=settings.llm_http_max_keepalive,
                keepalive_expiry=settings.llm_http_keepalive_expiry_s,
            ),
            # 兜底值: openai SDK 会在每个请求上覆盖成自己的超时
            # （connect 5s / read 600s），这里的读超时实际不生效
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        _http_clients[loop] = client
    return client


async def close_shared_http_clients() -> None:
    """停机清理（main.lifespan 调用）: 关掉本进程建过的连接池。"""
    for client in list(_http_clients.values()):
        if not client.is_closed:
            await client.aclose()
    _http_clients.clear()


def _client_kwargs() -> dict | None:
    """OpenAIChatModel 的 client_kwargs；无共享客户端时返回 None（不改行为）。"""
    client = shared_http_client()
    return {"http_client": client} if client is not None else None


def build_chat_model(*, only: str | None = None, stream: bool = False):
    """返回 (model, is_scripted)。

    按注册表顺序取第一个 configured 的 provider（config 级降级链）；
    only 指定时只匹配该注册名。全部未配置 → ScriptedRouterModel。
    """
    from agentscope.model import OpenAIChatModel

    for p in providers.list_providers():
        if only and p.name != only:
            continue
        if p.configured:
            return OpenAIChatModel(
                credential=OpenAICredential(
                    api_key=p.api_key,
                    base_url=p.base_url or None,
                ),
                model=p.default_model,
                stream=stream,
                max_retries=p.max_retries,
                client_kwargs=_client_kwargs(),
            ), False
    return ScriptedRouterModel(), True


def build_optimizer_model():
    """M3（from_hermes_a2a/04）: Loop Engineer 优化改写专用模型。

    优先 EAGENT_OPT_LLM_*（可用更便宜模型），缺省回退主 LLM；
    全部未配置 → ScriptedRouterModel（由调用方跳过 LLM 改写）。
    """
    from agentscope.model import OpenAIChatModel

    key = settings.opt_llm_api_key or settings.llm_api_key
    if key:
        return OpenAIChatModel(
            credential=OpenAICredential(
                api_key=key,
                base_url=settings.opt_llm_base_url or None,
            ),
            model=settings.opt_llm_model,
            stream=False,
            max_retries=1,
            client_kwargs=_client_kwargs(),
        ), False
    return ScriptedRouterModel(), True


def build_aux_model():
    """M4（from_hermes_a2a/04）: 辅助任务模型（失败样本摘要等）。

    仅当 EAGENT_AUX_LLM_API_KEY 配置时启用（避免为建而建）；
    未配置 → ScriptedRouterModel（消费方跳过辅助步骤）。
    """
    from agentscope.model import OpenAIChatModel

    if settings.aux_llm_api_key:
        return OpenAIChatModel(
            credential=OpenAICredential(
                api_key=settings.aux_llm_api_key,
                base_url=settings.aux_llm_base_url or None,
            ),
            model=settings.aux_llm_model,
            stream=False,
            max_retries=1,
            client_kwargs=_client_kwargs(),
        ), False
    return ScriptedRouterModel(), True


def select_provider_for(user_text: str) -> str | None:
    """M5（from_hermes_a2a/04）: smart routing——第一个命中规则的
    关键词 → provider 注册名；未命中返回 None（走默认链）。"""
    for rule in settings.llm_routing_rules:
        keywords = rule.get("match") or []
        if any(kw and kw in (user_text or "") for kw in keywords):
            name = str(rule.get("provider") or "").strip()
            if name:
                return name
    return None


def build_chat_model_for(user_text: str, *, stream: bool = False):
    """M5 入口: 按规则选 provider（简单→便宜、复杂→大模型）。

    规则指向的 provider 未配置/未命中时回退默认注册表顺序，
    保证路由规则故障不影响主链路（可配置可回退）。
    """
    hint = select_provider_for(user_text)
    if hint:
        model, scripted = build_chat_model(only=hint, stream=stream)
        if not scripted:
            return model, False
    return build_chat_model(stream=stream)


# ---------------------------------------------------------------------------
# per-master 模型配置（master-model-config 专题）：脱钩单一模型
# ---------------------------------------------------------------------------

def parse_model_config(config_json: str | None) -> dict | None:
    """解析 eag_master_agent.model_config_json；非法/空返回 None。"""
    if not config_json:
        return None
    try:
        cfg = json.loads(config_json)
    except (ValueError, TypeError):
        return None
    return cfg if isinstance(cfg, dict) else None


def model_config_usable(cfg: dict | None) -> bool:
    """per-master 配置是否完整到可独立构建模型（api_key + model 齐备）。"""
    return bool(
        cfg
        and (cfg.get("api_key") or "").strip()
        and (cfg.get("model") or "").strip()
    )


def build_chat_model_from_config(cfg: dict, *, stream: bool = False):
    """按 per-master 显式配置构建 OpenAI 兼容模型（调用方已判 usable）。"""
    from agentscope.model import OpenAIChatModel

    return OpenAIChatModel(
        credential=OpenAICredential(
            api_key=(cfg.get("api_key") or "").strip(),
            base_url=(cfg.get("base_url") or "").strip() or None,
        ),
        model=(cfg.get("model") or "").strip(),
        stream=stream,
        max_retries=2,
        client_kwargs=_client_kwargs(),
    ), False


def mask_model_config(cfg: dict | None) -> dict | None:
    """对外脱敏：只回显 base_url/model 与 key 尾 4 位，绝不回传明文 api_key。"""
    if not cfg:
        return None
    key = (cfg.get("api_key") or "").strip()
    return {
        "provider": cfg.get("provider") or "",
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "api_key_set": bool(key),
        "api_key_masked": ("****" + key[-4:]) if key else "",
    }


class DummyCredential(CredentialBase):
    pass


class ScriptedRouterModel(ChatModelBase):
    """无 LLM key 时的演示模型: 按关键词把请求路由到第一个匹配工具。

    仅用于链路演示与 e2e 测试，不进入生产（生产必须配置真实模型）。
    """

    def __init__(self) -> None:
        super().__init__(
            credential=DummyCredential(),
            model="scripted-router",
            parameters=ChatModelBase.Parameters(),
            stream=False,
            max_retries=0,
        )
        self._turn = 0

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):  # noqa: ANN001, ANN202
        self._turn += 1
        if self._turn % 2 == 1 and tools:
            # 奇数轮: 若上一轮没有 tool_result 则按词面匹配选工具
            last = messages[-1] if messages else None
            has_tool_result = any(
                getattr(b, "type", None) == "tool_result"
                for b in getattr(last, "content", []) or []
            )
            if not has_tool_result:
                user_text = ""
                for m in reversed(messages):
                    if m.role == "user":
                        blocks = [
                            getattr(b, "text", "") or ""
                            for b in m.content
                        ]
                        user_text = " ".join(blocks)
                        break
                tool = self._match_tool(tools, user_text)
                if tool is not None:
                    return ChatResponse(
                        content=[
                            ToolCallBlock(
                                id=uuid.uuid4().hex,
                                name=tool,
                                input=json.dumps(
                                    {"message": user_text}, ensure_ascii=False,
                                ),
                            ),
                        ],
                        is_last=True,
                    )
        # 偶数轮或无匹配: 汇总工具结果输出
        summary = self._summarize_tool_results(messages)
        return ChatResponse(
            content=[TextBlock(text=summary)], is_last=True,
        )

    @staticmethod
    def _match_tool(tools: list[dict], text: str) -> str | None:
        for t in tools:
            fn = t.get("function", {})
            desc = (fn.get("description") or "") + fn.get("name", "")
            name = fn.get("name", "")
            for kw in name.replace("_", " ").split() + [name]:
                if len(kw) >= 3 and kw.lower() in text.lower():
                    return name
            # 中文描述词面匹配
            for ch in ("天气", "报销", "财务", "假期", "人事"):
                if ch in text and ch in desc:
                    return name
        return tools[0]["function"]["name"] if tools else None

    @staticmethod
    def _summarize_tool_results(messages) -> str:  # noqa: ANN001
        parts = []
        for m in messages:
            for b in getattr(m, "content", []) or []:
                if getattr(b, "type", None) == "tool_result":
                    output = b.output
                    if isinstance(output, list):
                        output = " ".join(
                            getattr(x, "text", "") or "" for x in output
                        )
                    parts.append(f"[{b.name}] {output}")
        if parts:
            return "调用结果汇总: " + "; ".join(parts)
        return "当前可见服务集合内没有匹配的服务，请补充说明您的需求。"
