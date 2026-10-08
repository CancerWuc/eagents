# -*- coding: utf-8 -*-
"""hiagent 协议客户端（platform-spec hiagent-topic/01）。

- 四接口：create_conversation / update_conversation / chat_query_v2
  （streaming + blocking）/ sync_run_app_workflow（本期仅留封装位）。
- SSE：`data:` 帧 + `[DONE]` 结束 + 未知事件原样上抛（由上层按事件语义表
  分流，规范 §5 强制容错——hiagent 升级新增事件不得炸解析端）。
- 结构显式校验：鉴权失败/参数错误返回的是 `ResponseMetadata.Error` 结构，
  必须先判断 `Conversation`/状态再取值，不得让 KeyError 顶替真实错误
  （qagent 三渠道都踩过的坑，规范 §4.1）。
- 凭证：header `Apikey` + body `AppKey`；api_key 与 base_url 均
  **实例级优先、全局 env 兜底**（hiagent-integration/01 D6）：
  实例行 → `HIAGENT_API_KEY` / `HIAGENT_URL` → `HIAGNET_URL`（历史拼写）
  （服务地址必须由部署方显式配置）。
- 身份走 body（§6）：`Inputs.cap_user_name` / `real_name` + `UserID`，
  与 a2a 的 header 方式不混用；缺失用占位值（`未识别` / `unkown`）。

出站连接经 pinned_http 守卫（SSRF）；hiagent 主机经 `allowed_hosts()`
豁免**地址段校验**，固定 IP/禁重定向/超时/大小上限全部保留。
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from ..errors import EAgentError, GatewayTimeoutError

logger = logging.getLogger("eagent.hiagent")

# §6 身份占位值（沿用 qagent 历史拼写，保持与存量一致）
DEFAULT_REAL_NAME = "未识别"
DEFAULT_CAP_USER = "unkown"


def _query_extends(files: list[dict] | None) -> dict:
    return {"Files": list(files)} if files else {}


class HiagentAPIError(EAgentError):
    """hiagent 返回错误结构 / 响应不符合协议预期（DEPENDENCY_ERROR）。"""

    code = "HIAGENT_INVALID_RESPONSE"

    def __init__(self, message=None, **kwargs):
        super().__init__(message, **kwargs)
        code = (message or "").split(":", 1)[0]
        if code in {"HIAGENT_EMPTY_RESPONSE", "HIAGENT_INCOMPLETE_RESPONSE",
                    "HIAGENT_UPSTREAM_ERROR", "HIAGENT_RESPONSE_TOO_LARGE",
                    "HIAGENT_UPDATE_FAILED"}:
            self.code = code


def resolve_base_url(instance_base_url: str | None = None) -> str:
    """base_url 解析链：实例行 → HIAGENT_URL → HIAGNET_URL；缺失时拒绝调用。"""
    from ..config import settings

    url = (instance_base_url or "").strip() or settings.hiagent_url
    if not url:
        raise ValueError("Configure HIAGENT_URL or an instance base URL before calling HiAgent")
    return url.rstrip("/")


def resolve_api_key(instance_app_key: str | None = None) -> str:
    """api_key 解析链：实例行（解密后）→ 全局 HIAGENT_API_KEY。"""
    from ..config import settings

    return (instance_app_key or "").strip() or settings.hiagent_api_key.strip()


def _timeout(timeout_s: float) -> httpx.Timeout:
    # 规范 §2：connect 5s / read 放宽（流式回答耗时长）
    return httpx.Timeout(timeout_s, connect=5.0, read=timeout_s)


# 连接池按 base_url + timeout 复用（api_key 是逐请求 header，不进池 key）
_pool: dict[tuple[str, float], httpx.AsyncClient] = {}


def _pooled_client(base_url: str, timeout_s: float) -> httpx.AsyncClient:
    normalized_timeout = float(timeout_s)
    pool_key = (base_url, normalized_timeout)
    client = _pool.get(pool_key)
    if client is None or client.is_closed:
        from ..security.pinned_http import guarded_async_client

        client = guarded_async_client(timeout=_timeout(normalized_timeout))
        _pool[pool_key] = client
        try:
            host = httpx.URL(base_url).host or "<unknown>"
        except httpx.InvalidURL:
            host = "<invalid>"
        logger.info(
            "HiAgent HTTP pool created host=%s timeout_s=%s",
            host,
            normalized_timeout,
        )
    return client


async def close_pooled() -> None:
    """关闭连接池（lifespan 停机 / 测试清理用）。"""
    for client in _pool.values():
        if not client.is_closed:
            await client.aclose()
    _pool.clear()


def _map_transport_error(exc: Exception, what: str) -> EAgentError:
    """出站传输层异常 → 统一错误（注册/探测/调用共用）。

    SSRF 拒绝（AddressBlockedError，ValueError 子类）若不在此映射会冒泡成
    500——实例级 base_url 指向未豁免的内网地址是可预期的配置错误，
    必须以 DEPENDENCY_ERROR 明确报错并给出豁免指引。"""
    from ..security.pinned_http import AddressBlockedError

    if isinstance(exc, AddressBlockedError):
        return HiagentAPIError(
            f"hiagent {what} 被 SSRF 策略拒绝（{exc}）；"
            "实例地址指向内网时需在 EAGENT_HIAGENT_ALLOW_HOST 豁免该主机",
        )
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return GatewayTimeoutError(f"hiagent {what} 超时")
    return HiagentAPIError(f"hiagent {what} 连接失败: {exc}")


def _identity_body(
    cap_user_name: str | None, real_name: str | None,
) -> tuple[dict, str]:
    """§6 身份 → (Inputs, UserID)。缺失用占位值，不伪造真实身份。"""
    cap = (cap_user_name or "").strip() or DEFAULT_CAP_USER
    real = (real_name or "").strip() or DEFAULT_REAL_NAME
    return {"cap_user_name": cap, "real_name": real}, cap


def _raise_api_error(resp: httpx.Response, payload: Any) -> None:
    """统一错误提取：ResponseMetadata.Error 结构 → 显式 HiagentAPIError，
    非 JSON/HTTP 错误 → 带 status + body 前 500 字符（规范 §10.7）。"""
    if isinstance(payload, dict):
        meta = payload.get("ResponseMetadata") or {}
        err = meta.get("Error") if isinstance(meta, dict) else None
        if isinstance(err, dict) and err.get("Code"):
            raise HiagentAPIError(
                f"hiagent 返回错误 {err.get('Code')}: {err.get('Message', '')}",
            )
    body = resp.text if isinstance(resp.text, str) else str(resp.content[:500])
    raise HiagentAPIError(
        f"hiagent 响应不符合协议（HTTP {resp.status_code}）: {body[:500]}",
    )


def parse_sse_line(line: str) -> dict | None:
    """单行 SSE → 事件 dict。非 data 行 / [DONE] 返回 None；未知事件原样
    上抛由调用方按语义表容错（规范 §5）。非法 JSON 显式报错（不静默丢帧）。"""
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise HiagentAPIError(f"hiagent SSE 帧不是合法 JSON: {payload[:200]}") from exc
    if not isinstance(event, dict):
        return None
    return event


class HiagentClient:
    """单个 HIA agent（实例级 base_url + api_key）的协议客户端。"""

    def __init__(
        self, *, base_url: str | None = None, api_key: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._base_url = resolve_base_url(base_url)
        self._api_key = api_key  # 惰性解析：None 时每次按解析链取（env 可热变）
        self._timeout_s = timeout_s

    @property
    def base_url(self) -> str:
        return self._base_url

    def _key(self) -> str:
        key = resolve_api_key(self._api_key)
        if not key:
            raise HiagentAPIError(
                "hiagent api_key 未配置（实例级 app_key 与全局"
                " HIAGENT_API_KEY 均为空）",
            )
        return key

    def _client(self) -> httpx.AsyncClient:
        from ..config import settings

        return _pooled_client(
            self._base_url, self._timeout_s or settings.hiagent_timeout_s,
        )

    async def create_conversation(
        self, *, inputs: dict | None = None,
        cap_user_name: str | None, real_name: str | None = None,
    ) -> str:
        """§4.1 建会话，返回 AppConversationID。

        首轮业务 Inputs 与身份一并发送，认证身份最后覆盖。
        显式校验 `Conversation` 字段再取值——鉴权失败返回
        `ResponseMetadata.Error`，直接取值会抛误导性 KeyError。"""
        from .query_extends import validate_input_keys

        business_inputs = dict(inputs or {})
        validate_input_keys(business_inputs)
        identity, user_id = _identity_body(cap_user_name, real_name)
        merged = {**business_inputs, **identity}
        key = self._key()
        client = self._client()
        try:
            resp = await client.post(
                f"{self._base_url}/create_conversation",
                headers={"Apikey": key},
                json={"AppKey": key, "Inputs": merged, "UserID": user_id},
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, EAgentError):
                raise
            raise _map_transport_error(exc, "create_conversation") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            from .normalizer import check_error
            check_error(payload)
        if not 200 <= resp.status_code < 300 or not isinstance(payload, dict) \
                or not isinstance(payload.get("Conversation"), dict):
            _raise_api_error(resp, payload)
        conv_id = payload["Conversation"].get("AppConversationID")
        if not conv_id:
            raise HiagentAPIError(
                "hiagent create_conversation 缺少 AppConversationID",
            )
        return str(conv_id)

    async def update_conversation(
        self, conversation_id: str, *, inputs: dict | None = None,
        cap_user_name: str | None = None, real_name: str | None = None,
        conversation_name: str = "eAgent 会话",
    ) -> None:
        """§4.2 用本轮完整业务 Inputs 更新会话，认证身份最后覆盖。"""
        from .query_extends import validate_input_keys

        business_inputs = dict(inputs or {})
        validate_input_keys(business_inputs)
        identity, user_id = _identity_body(cap_user_name, real_name)
        merged = {**business_inputs, **identity}
        key = self._key()
        client = self._client()
        try:
            resp = await client.post(
                f"{self._base_url}/update_conversation",
                headers={"Apikey": key},
                json={
                    "AppConversationID": conversation_id,
                    "ConversationName": conversation_name,
                    "Inputs": merged,
                    "UserID": user_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, EAgentError):
                raise
            raise _map_transport_error(exc, "update_conversation") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        business_error = None
        if isinstance(payload, dict):
            meta = payload.get("ResponseMetadata")
            if isinstance(meta, dict):
                business_error = meta.get("Error")
        if not 200 <= resp.status_code < 300 or business_error is not None:
            code = business_error.get("Code") if isinstance(business_error, dict) else None
            suffix = f":{code}" if code else ""
            raise HiagentAPIError(f"HIAGENT_UPDATE_FAILED{suffix}")

    async def chat_blocking(
        self, conversation_id: str, query: str, *,
        cap_user_name: str | None, real_name: str | None = None,
        files: list[dict] | None = None,
    ) -> dict:
        """§4.3 非流式对话（ResponseMode=blocking），返回完整 JSON。

        答案在顶层 `answer` 字段（qagent stream_handler 同口径）。"""
        _inputs, user_id = _identity_body(cap_user_name, real_name)
        body: dict[str, Any] = {
            "AppConversationID": conversation_id,
            "Query": query,
            "ResponseMode": "blocking",
            "QueryExtends": _query_extends(files),
            "UserID": user_id,
        }
        client = self._client()
        try:
            resp = await client.post(
                f"{self._base_url}/chat_query_v2",
                headers={"Apikey": self._key()},
                json=body,
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, EAgentError):
                raise
            raise _map_transport_error(exc, "chat_query_v2") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not 200 <= resp.status_code < 300 or not isinstance(payload, dict):
            _raise_api_error(resp, payload)
        from .normalizer import normalize_blocking
        normalize_blocking(payload)
        return payload

    async def chat_stream(
        self, conversation_id: str, query: str, *,
        cap_user_name: str | None, real_name: str | None = None,
        files: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """§4.3 流式对话（ResponseMode=streaming），逐事件 yield（含未知事件）。

        事件语义表见规范 §5；`message.answer` 是正文、`interrupted` 是
        信息收集交互，分流在上层（executor/tool）完成。"""
        _inputs, user_id = _identity_body(cap_user_name, real_name)
        body: dict[str, Any] = {
            "AppConversationID": conversation_id,
            "Query": query,
            "ResponseMode": "streaming",
            "QueryExtends": _query_extends(files),
            "UserID": user_id,
        }
        client = self._client()
        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat_query_v2",
                headers={"Apikey": self._key(), "Accept": "text/event-stream"},
                json=body,
            ) as resp:
                if not 200 <= resp.status_code < 300:
                    await resp.aread()
                    _raise_api_error(resp, None)
                if "text/event-stream" not in resp.headers.get("content-type", ""):
                    await resp.aread()
                    try:
                        payload = resp.json()
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict):
                        from .normalizer import check_error
                        check_error(payload)
                    raise HiagentAPIError("HIAGENT_INVALID_RESPONSE")
                total_bytes = 0
                async for line in resp.aiter_lines():
                    total_bytes += len(line.encode())
                    if total_bytes > 8 * 1024 * 1024:
                        raise HiagentAPIError("HIAGENT_RESPONSE_TOO_LARGE")
                    event = parse_sse_line(line)
                    if event is not None:
                        yield event
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, EAgentError):
                raise
            raise _map_transport_error(exc, "流式回答") from exc

    async def run_welcome_workflow(
        self, callback_url: str, *, cap_user_name: str, timeout_ms: int = 4000,
    ) -> dict:
        """§4.4 欢迎语工作流（**本期不接入**，渠道场景专用；留封装位）。

        InputData 是序列化后的 JSON 字符串；超时钳制 500ms~4000ms。"""
        key = self._key()
        client = self._client()
        try:
            resp = await client.post(
                callback_url,
                headers={"Apikey": key},
                json={
                    "InputData": json.dumps(
                        {"cap_user_name": cap_user_name}, ensure_ascii=False,
                    ),
                    "UserID": cap_user_name,
                },
                timeout=min(max(timeout_ms, 500), 4000) / 1000,
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, EAgentError):
                raise
            raise _map_transport_error(exc, "欢迎语工作流") from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise HiagentAPIError("hiagent 欢迎语工作流响应非 JSON") from exc


def extract_interrupted_options(event: dict) -> list[dict]:
    """`interrupted` 事件的选项解析（规范 §5 专节）。

    `biz_code.data.options` 是**序列化后的 JSON 字符串**，需二次解析；
    解析失败按空选项处理（不炸会话）。"""
    biz = event.get("biz_code")
    data = biz.get("data") if isinstance(biz, dict) else None
    raw = data.get("options") if isinstance(data, dict) else None
    if isinstance(raw, list):
        return [o for o in raw if isinstance(o, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        return [o for o in parsed if isinstance(o, dict)] if isinstance(
            parsed, list,
        ) else []
    return []
