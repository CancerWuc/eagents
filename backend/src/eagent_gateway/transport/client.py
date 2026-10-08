# -*- coding: utf-8 -*-
"""统一 A2A 出站传输层（from_nekiro/12 §S1 / A1，2026-08-15）。

所有 A2A 出站（Card 拉取、JSON-RPC message/send）经 `A2ATransport`:

- SSRF 防护（解析+校验+固定 IP，见 security/pinned_http）
- 禁止重定向（3xx 显式拒绝，不跟随）
- 统一超时（显式传入，无隐式默认）
- 响应大小上限（Content-Length 预检 + 实读字节数强制校验）
- JSON-RPC envelope 校验: 必须含 jsonrpc=2.0，响应 id 必须与请求 id 一致
- 错误统一分类（transport/errors.classify_transport_error）

ManagedA2AClient 是本层的唯一上游（业务代码禁止直接使用）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from ..config import settings
from ..security import pinned_http
from .errors import (
    MalformedRpcError,
    RedirectRejectedError,
    ResponseTooLargeError,
    classify_transport_error,
)
from .sse import consume_sse

_MAX_REDIRECT = {301, 302, 303, 307, 308}

# 0.2.x TaskState 终态集合（types.py TaskState + input-required 暂停语义）:
# 流式消费按载荷判定终态——result 为 Task 且 status.state 落在此集合、
# 或 result 为 Message（含 parts，0.2.x 允许直出最终结果）、或 error envelope。
_TASK_TERMINAL_STATES = frozenset({
    "completed", "failed", "canceled", "rejected", "auth-required",
    "input-required",
})


def envelope_is_terminal(envelope: Any) -> bool:
    """流式 JSON-RPC envelope 载荷终态判定（intranet-a2a-stream-adapter）。

    a2a-sdk 0.2.x 的 message/stream wire 只发 data: 不发 event: 名，
    终态必须看载荷（0.2.16 实测事件形态）:
    - error envelope；
    - TaskStatusUpdateEvent.final == true（0.2.16 权威终态标志）；
    - status.state 落入终态集合（completed/failed/... 及 input-required）；
    - result 为 Message（含 parts，0.2.x 允许直出最终结果）。"""
    if not isinstance(envelope, dict):
        return False
    if envelope.get("error") is not None:
        return True
    result = envelope.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("final") is True:
        return True
    status = result.get("status")
    if isinstance(status, dict) and status.get("state") in _TASK_TERMINAL_STATES:
        return True
    return isinstance(result.get("parts"), list)


class A2ATransport:
    """显式配置的受管出站传输（每次调用新建连接，无跨请求复用状态）。"""

    def __init__(
        self,
        *,
        timeout_s: float,
        max_response_bytes: int,
        allow_private: bool | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须为正数（禁止无界传输）")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes 必须为正数（禁止无界传输）")
        self._timeout = timeout_s
        self._max_bytes = max_response_bytes
        self._allow_private = (
            settings.allow_private_addresses
            if allow_private is None else allow_private
        )

    def _client(self) -> httpx.AsyncClient:
        transport = _SizeEnforcingTransport(
            max_bytes=self._max_bytes,
            allow_private=self._allow_private,
        )
        return httpx.AsyncClient(
            transport=transport,
            timeout=self._timeout,
            follow_redirects=False,  # 统一出口规则: 永不跟随重定向
        )

    async def get_json(self, url: str, *, headers: dict | None = None) -> Any:
        """GET 并解析 JSON（Card 拉取）。非 2xx/重定向/超限/非法 JSON 均抛统一错误。"""
        try:
            async with self._client() as client:
                resp = await client.get(url, headers=headers)
                self._check_status(resp)
                body = await self._read_capped(resp)
        except Exception as exc:  # noqa: BLE001
            raise classify_transport_error(exc) from exc
        return self._parse_json(body)

    async def post_jsonrpc(
        self,
        url: str,
        payload: dict,
        *,
        headers: dict | None = None,
        request_id: str | None = None,
    ) -> dict:
        """POST JSON-RPC 并校验 envelope。返回解析后的 dict。

        - request_id 缺省时取 payload["id"]（仍要求响应 id 一致）
        - 响应 id 与请求 id 不一致 → MalformedRpcError（防串扰/脏数据）
        """
        expected_id = request_id if request_id is not None else payload.get("id")
        if expected_id is None:
            expected_id = uuid.uuid4().hex
            payload = {**payload, "id": expected_id}
        try:
            async with self._client() as client:
                resp = await client.post(url, json=payload, headers=headers)
                self._check_status(resp)
                body = await self._read_capped(resp)
        except Exception as exc:  # noqa: BLE001
            raise classify_transport_error(exc) from exc
        envelope = self._parse_json(body)
        if not isinstance(envelope, dict):
            raise MalformedRpcError("JSON-RPC 响应必须是对象")
        if envelope.get("jsonrpc") != "2.0":
            raise MalformedRpcError("JSON-RPC 响应缺少 jsonrpc=2.0")
        if envelope.get("id") != expected_id:
            raise MalformedRpcError("JSON-RPC 响应 id 与请求不一致")
        return envelope

    async def post_jsonrpc_stream(
        self,
        url: str,
        payload: dict,
        *,
        headers: dict | None = None,
        request_id: str | None = None,
        max_events: int,
        max_total_bytes: int,
    ) -> list[dict]:
        """POST message/stream 并按 SSE 规范消费（intranet-a2a-stream-adapter）。

        返回按到达顺序解析的 JSON-RPC envelope 列表（每条 data 一个）。

        - 显式 `Accept: text/event-stream`；
        - Content-Type 非 event-stream 时退回单 envelope 解析（部分对端对
          非法请求回 JSON-RPC error JSON 而非 SSE，需如实透出错误语义）；
        - 每条 data 必须是 jsonrpc=2.0 且 id 与请求一致的 envelope；
        - 终态按载荷判定（envelope_is_terminal），消费不到终态即断流失败；
        - SSRF/禁重定向/大小上限与 post_jsonrpc 完全同一套规则。
        """
        expected_id = request_id if request_id is not None else payload.get("id")
        if expected_id is None:
            expected_id = uuid.uuid4().hex
            payload = {**payload, "id": expected_id}
        parsed: dict[str, dict] = {}

        def _envelope_of(data: str) -> dict:
            envelope = parsed.get(data)
            if envelope is None:
                envelope = self._parse_json(data.encode("utf-8"))
                self._validate_envelope(envelope, expected_id)
                parsed[data] = envelope
            return envelope

        envelopes: list[dict] | None = None
        body: bytes | None = None
        try:
            async with self._client() as client:
                stream_headers = {**(headers or {})}
                stream_headers.setdefault("Accept", "text/event-stream")
                async with client.stream(
                    "POST", url, json=payload, headers=stream_headers,
                ) as resp:
                    self._check_status(resp)
                    content_type = resp.headers.get("content-type", "")
                    if content_type.split(";")[0].strip().lower() \
                            == "text/event-stream":
                        events = await consume_sse(
                            resp,
                            is_terminal=lambda evt: envelope_is_terminal(
                                _envelope_of(evt.data),
                            ),
                            max_events=max_events,
                            max_total_bytes=max_total_bytes,
                        )
                        envelopes = [_envelope_of(e.data) for e in events]
                    else:
                        body = await self._read_capped(resp)
        except Exception as exc:  # noqa: BLE001
            raise classify_transport_error(exc) from exc
        if envelopes is not None:
            return envelopes
        envelope = self._parse_json(body or b"")
        self._validate_envelope(envelope, expected_id)
        return [envelope]

    @staticmethod
    def _validate_envelope(envelope: Any, expected_id: str) -> None:
        if not isinstance(envelope, dict):
            raise MalformedRpcError("JSON-RPC 响应必须是对象")
        if envelope.get("jsonrpc") != "2.0":
            raise MalformedRpcError("JSON-RPC 响应缺少 jsonrpc=2.0")
        if envelope.get("id") != expected_id:
            raise MalformedRpcError("JSON-RPC 响应 id 与请求不一致")

    # ------------------------------------------------------------------
    @staticmethod
    def _check_status(resp: httpx.Response) -> None:
        if resp.status_code in _MAX_REDIRECT:
            location = resp.headers.get("location", "")
            raise RedirectRejectedError(
                f"远端返回 {resp.status_code} 重定向"
                + (f" -> {location[:64]}" if location else ""),
            )
        if resp.status_code >= 400:
            from ..errors import DependencyError

            raise DependencyError(
                f"远端返回 HTTP {resp.status_code}",
            )

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max_bytes:
            raise ResponseTooLargeError()
        await resp.aread()
        if len(resp.content) > self._max_bytes:
            raise ResponseTooLargeError()
        return resp.content

    @staticmethod
    def _parse_json(body: bytes) -> Any:
        import json as _json

        try:
            return _json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise MalformedRpcError("远端响应不是合法 JSON") from exc


class _SizeEnforcingTransport(pinned_http.SSRFGuardAsyncTransport):
    """SSRF 防护 + Content-Length 预检（实读字节数由 _read_capped 强制）。"""

    def __init__(self, *, max_bytes: int, allow_private: bool) -> None:
        super().__init__(allow_private=allow_private)
        self._max_bytes = max_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max_bytes:
            raise ResponseTooLargeError()
        return response
