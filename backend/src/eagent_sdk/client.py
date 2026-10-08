# -*- coding: utf-8 -*-
"""eAgent 业务系统客户端（from_nekiro/12 §S2 / B1、B3、B4）。

业务系统只面向 eAgent 网关，不感知也不传远端 A2A URL（B1）。

约束:
- 大小/超时限制必须显式传入，缺失或非法直接初始化失败（B3）。
- 身份支持两种来源：eAgent Session Bearer，或可信入口注入的
  cap-user-name（real-name 仅展示）；至少提供一种。
- 禁止重定向、不自动重试（fail-closed）。
- 错误统一为 EAgentPlatformError，不含原始响应体（B4）。
- 作为 Agent 被网关调用后回调网关（嵌套调用）时，传入
  platform_context 携带血缘头，调用链保持同一棵 Trace 树。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

import httpx

from .errors import (
    EAgentConnectionError,
    EAgentPayloadTooLargeError,
    EAgentPlatformError,
    EAgentTimeoutError,
    from_http_error,
)


def _header_value(value: str) -> str | bytes:
    """保留 ASCII 兼容；中文姓名以 UTF-8 Header bytes 交给 httpx。"""
    return value if value.isascii() else value.encode("utf-8")


@dataclass
class PlatformContext:
    """嵌套调用的血缘上下文（Agent 回调网关时传入）。"""

    trace_id: str
    root_task_id: str
    own_invocation_id: str

    def headers(self) -> dict[str, str]:
        # 注意: 不携带 X-EAgent-Invocation-Id —— 新一跳由网关分配新
        # invocation，父是「自己当前的 invocation」
        return {
            "X-EAgent-Trace-Id": self.trace_id,
            "X-EAgent-Root-Task-Id": self.root_task_id,
            "X-EAgent-Parent-Invocation-Id": self.own_invocation_id,
        }


@dataclass
class SendResult:
    text: str
    trace_id: str | None
    task_id: str | None
    context_id: str | None
    state: str = ""
    interaction: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HiAgentFile:
    """Reference to a file already uploaded to the target HiAgent origin."""

    url: str
    filename: str
    media_type: str
    path: str
    size: int

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.url) if isinstance(self.url, str) else None
            if parsed:
                _ = parsed.port
        except ValueError as exc:
            raise ValueError("HiAgentFile.url must be an HTTPS URL") from exc
        if not parsed or parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("HiAgentFile.url must be an HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("HiAgentFile.url must not contain user information")
        if not isinstance(self.filename, str) or not self.filename.strip():
            raise ValueError("HiAgentFile.filename must be a nonempty string")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("HiAgentFile.media_type must be a nonempty string")
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("HiAgentFile.path must be a nonempty string")
        normalized_parts = self.path.replace("\\", "/").split("/")
        if PurePosixPath(self.path).is_absolute() \
                or PureWindowsPath(self.path).is_absolute() \
                or PureWindowsPath(self.path).drive or ".." in normalized_parts:
            raise ValueError("HiAgentFile.path must be relative and must not contain '..'")
        if isinstance(self.size, bool) or not isinstance(self.size, int) \
                or self.size < 0:
            raise ValueError("HiAgentFile.size must be a nonnegative integer")

    def to_part(self) -> dict:
        return {
            "url": self.url,
            "filename": self.filename,
            "mediaType": self.media_type,
            "metadata": {"hiagent": {"path": self.path, "size": self.size}},
        }


class EAgentClient:
    """调用 eAgent A2A 1.0 端点；默认消费 SSE，保留非流式兼容。"""

    def __init__(
        self,
        *,
        gateway_url: str,
        agent_slug: str,
        timeout_s: float,
        request_max_bytes: int,
        response_max_bytes: int,
        bearer_token: str | None = None,
        cap_user_name: str | None = None,
        real_name: str | None = None,
        verify_tls: bool = True,
    ) -> None:
        # B3: 显式限制，无默认值、无隐式放行
        if not gateway_url.startswith(("http://", "https://")):
            raise ValueError("gateway_url 必须是 http(s) 地址")
        if not agent_slug:
            raise ValueError("agent_slug 不能为空")
        if timeout_s is None or timeout_s <= 0:
            raise ValueError("timeout_s 必须显式传入且为正数")
        if request_max_bytes is None or request_max_bytes <= 0:
            raise ValueError("request_max_bytes 必须显式传入且为正数")
        if response_max_bytes is None or response_max_bytes <= 0:
            raise ValueError("response_max_bytes 必须显式传入且为正数")
        self._endpoint = (
            f"{gateway_url.rstrip('/')}/a2a/{agent_slug.strip('/')}/rpc"
        )
        self._timeout = timeout_s
        self._req_max = request_max_bytes
        self._resp_max = response_max_bytes
        bearer = (bearer_token or "").strip()
        cap_user = (cap_user_name or "").strip()
        display_name = (real_name or "").strip()
        if not bearer and not cap_user:
            raise ValueError("bearer_token or cap_user_name is required")
        self._headers: dict[str, str | bytes] = {
            "content-type": "application/json", "A2A-Version": "1.0",
        }
        if bearer:
            self._headers["Authorization"] = "Bearer " + bearer
        if cap_user:
            self._headers["cap-user-name"] = _header_value(cap_user)
        if display_name:
            self._headers["real-name"] = _header_value(display_name)
        self._verify_tls = verify_tls

    async def send_message(
        self,
        text: str,
        *,
        context_id: str | None = None,
        platform_context: PlatformContext | None = None,
        task_id: str | None = None,
        inputs: dict | None = None,
        services: dict | None = None,
        interaction_response: dict | None = None,
        files: list[HiAgentFile] | None = None,
        streaming: bool = True,
        on_event=None,
    ) -> SendResult:
        """发送一条消息；默认消费 SSE，返回本轮终态与完整正文。"""
        if files and services is not None:
            raise ValueError("files cannot be used with services")
        if files and inputs is None:
            raise ValueError("files require direct HiAgent inputs")
        if files and any(not isinstance(item, HiAgentFile) for item in files):
            raise ValueError("files must contain HiAgentFile values")
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "SendStreamingMessage" if streaming else "SendMessage",
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "messageId": uuid.uuid4().hex,
                    "parts": [{"text": text, "mediaType": "text/plain"}],
                },
            },
        }
        message = payload["params"]["message"]
        if context_id:
            message["contextId"] = context_id
        if task_id:
            message["taskId"] = task_id
        extension = None
        if interaction_response is not None:
            message["parts"] = [{"data": interaction_response, "mediaType": "application/vnd.eagent.hiagent-interaction-response+json"}]
        if inputs is not None:
            extension = "https://eagent.example/extensions/hiagent-inputs/v1"
            message["parts"].append({"data":{"inputs":inputs},"mediaType":"application/vnd.eagent.hiagent-inputs+json"})
        elif services is not None:
            extension = "https://eagent.example/extensions/hiagent-services/v1"
            message["parts"].append({"data":{"services":services},"mediaType":"application/vnd.eagent.hiagent-services+json"})
        if files:
            message["parts"].extend(item.to_part() for item in files)
        if extension:
            message["extensions"] = [extension]
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) > self._req_max:
            raise EAgentPayloadTooLargeError(
                f"请求体 {len(body)} 字节超过限制 {self._req_max}",
            )
        headers = dict(self._headers)
        if extension:
            headers["A2A-Extensions"] = extension
        if platform_context is not None:
            headers.update(platform_context.headers())
        if streaming:
            headers["Accept"] = "text/event-stream"

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,  # 禁止重定向
                verify=self._verify_tls,
            ) as client:
                if streaming:
                    async with client.stream("POST", self._endpoint, content=body,
                                             headers=headers) as resp:
                        result = await self._consume_stream(
                            resp, payload["id"], on_event=on_event,
                        )
                else:
                    resp = await client.post(self._endpoint, content=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise EAgentTimeoutError() from exc
        except httpx.HTTPError as exc:
            raise EAgentConnectionError() from exc

        if resp.status_code in (301, 302, 303, 307, 308):
            raise EAgentConnectionError("网关返回重定向，已拒绝")
        if not streaming and len(resp.content) > self._resp_max:
            raise EAgentPayloadTooLargeError(
                f"响应体 {len(resp.content)} 字节超过限制 {self._resp_max}",
            )
        if streaming:
            return self._send_result(result, resp)
        try:
            envelope = resp.json()
        except ValueError as exc:
            raise EAgentConnectionError("网关响应不是合法 JSON") from exc

        if resp.status_code >= 400:
            raise from_http_error(resp.status_code, envelope)
        if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0":
            raise EAgentConnectionError("网关响应不是 JSON-RPC envelope")
        if envelope.get("id") != payload["id"]:
            raise EAgentConnectionError("JSON-RPC 响应 id 与请求不一致")
        if envelope.get("error"):
            err = envelope["error"]
            raise EAgentPlatformError(
                code=f"A2A_{err.get('code', 'ERROR')}",
                message=str(err.get("message") or "远端 Agent 返回错误"),
                trace_id=resp.headers.get("x-trace-id"),
            )
        wrapper = envelope.get("result") or {}
        result = wrapper.get("task") or wrapper.get("message")
        return self._send_result(result, resp)

    def _send_result(self, result: dict, resp) -> SendResult:
        if not isinstance(result, dict):
            raise EAgentConnectionError("Invalid A2A 1.0 response")
        state = result.get("status", {}).get("state", "")
        if state in {"TASK_STATE_FAILED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED"}:
            raise EAgentPlatformError(code=state, message=self._extract_text(result) or state)
        parts = result.get("status", {}).get("message", {}).get("parts", [])
        interaction = next((p["data"] for p in parts if p.get("mediaType") == "application/vnd.eagent.hiagent-interaction+json"), {})
        return SendResult(
            text=self._extract_text(result),
            trace_id=resp.headers.get("x-trace-id"),
            task_id=result.get("id"),
            context_id=result.get("contextId"),
            state=state, interaction=interaction,
        )

    @staticmethod
    def _extract_text(result: dict) -> str:
        for part in result.get("parts") or []:
            if part.get("text"):
                return part["text"]
        if result.get("status", {}).get("state") in {
            "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED",
            "TASK_STATE_CANCELED",
        }:
            return "".join(p.get("text", "") for p in result.get("status", {}).get("message", {}).get("parts", []))
        text = "".join(
            part.get("text", "") for artifact in result.get("artifacts") or []
            for part in artifact.get("parts") or []
        )
        if text:
            return text
        return "".join(p.get("text", "") for p in result.get("status", {}).get("message", {}).get("parts", []))

    async def _consume_stream(self, resp: httpx.Response, request_id: str,
                              *, on_event=None) -> dict:
        if resp.status_code in (301, 302, 303, 307, 308):
            raise EAgentConnectionError("网关返回重定向，已拒绝")
        if "text/event-stream" not in resp.headers.get("content-type", ""):
            data = await resp.aread()
            if len(data) > self._resp_max:
                raise EAgentPayloadTooLargeError("响应超过大小限制")
            try:
                envelope = json.loads(data)
            except (ValueError, UnicodeDecodeError) as exc:
                raise EAgentConnectionError("网关响应不是合法 JSON") from exc
            if resp.status_code >= 400:
                raise from_http_error(resp.status_code, envelope)
            if envelope.get("id") != request_id:
                raise EAgentConnectionError("JSON-RPC 响应 id 与请求不一致")
            if envelope.get("error"):
                raise EAgentPlatformError(
                    code=f"A2A_{envelope['error'].get('code', 'ERROR')}",
                    message=str(envelope['error'].get("message") or "远端 Agent 返回错误"),
                )
            return (envelope.get("result") or {}).get("task") or {}

        pending = ""
        total = 0
        task: dict | None = None

        async def frame(value: str) -> None:
            nonlocal task
            data = "\n".join(line[5:].lstrip() for line in value.split("\n")
                             if line.startswith("data:"))
            if not data:
                return
            try:
                envelope = json.loads(data)
            except ValueError as exc:
                raise EAgentConnectionError("A2A SSE 事件不是合法 JSON") from exc
            if envelope.get("id") != request_id:
                raise EAgentConnectionError("JSON-RPC 响应 id 与请求不一致")
            if envelope.get("error"):
                err = envelope["error"]
                raise EAgentPlatformError(
                    code=f"A2A_{err.get('code', 'ERROR')}",
                    message=str(err.get("message") or "远端 Agent 返回错误"),
                )
            event = envelope.get("result") or {}
            if "task" in event:
                task = event["task"]
            elif task is None:
                raise EAgentConnectionError("A2A 流缺少初始 Task")
            elif "statusUpdate" in event:
                task["status"] = event["statusUpdate"]["status"]
            elif "artifactUpdate" in event:
                update = event["artifactUpdate"]
                artifact = update["artifact"]
                artifacts = task.setdefault("artifacts", [])
                old = next((a for a in artifacts if a.get("artifactId") == artifact.get("artifactId")), None)
                if update.get("append"):
                    if old is None:
                        raise EAgentConnectionError("A2A Artifact 追加前缺少首片段")
                    old.setdefault("parts", []).extend(artifact.get("parts") or [])
                    old.setdefault("metadata", {}).update(artifact.get("metadata") or {})
                elif old is None:
                    artifacts.append(artifact)
                else:
                    artifacts[artifacts.index(old)] = artifact
            if on_event is not None:
                value = on_event(event, task)
                if hasattr(value, "__await__"):
                    await value

        async for chunk in resp.aiter_text():
            total += len(chunk.encode("utf-8"))
            if total > self._resp_max:
                raise EAgentPayloadTooLargeError("响应超过大小限制")
            pending += chunk
            pending = pending.replace("\r\n", "\n")
            end = pending.find("\n\n")
            while end >= 0:
                await frame(pending[:end])
                pending = pending[end + 2:]
                end = pending.find("\n\n")
        if pending.strip():
            await frame(pending)
        if task is None or task.get("status", {}).get("state") not in {
            "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED",
            "TASK_STATE_REJECTED", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED",
        }:
            raise EAgentConnectionError("A2A 流未返回本轮结束状态")
        return task
