# -*- coding: utf-8 -*-
"""SSE 消费规范接口（from_nekiro/12 §S1 / A2，实现随流式启用）。

规范（详见 eagent-wiki/from_nekiro/13-transport-sse-spec.md）:
1. 只接受 `Content-Type: text/event-stream`，其余一律视为协议错误。
2. 每个事件: 单行 `data:`（多行 data 拒绝）、可选且唯一的 `id:`、
   可选 `event:`；空行分隔。
3. 按到达顺序同步消费（不做客户端缓冲重排）。
4. 必须消费到 terminal event 且收到 EOF，否则视为「流中断」
   （半截结果不是成功，fail-closed）。
5. 取消只来自调用方；不自动重连、不自动重试。
6. 事件数量与总字节数必须有显式上限。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from ..errors import DependencyError, PayloadTooLargeError


class SSEProtocolError(DependencyError):
    safe_message = "远端 SSE 流不符合消费规范"


class SSEInterruptedError(DependencyError):
    """未消费到 terminal event 就断流（半截结果视为失败）。"""

    safe_message = "SSE 流中断（未收到 terminal event）"


@dataclass
class SSEEvent:
    id: str | None
    event: str | None
    data: str


async def consume_sse(
    response: httpx.Response,
    *,
    terminal_event: str | None = None,
    is_terminal: Callable[[SSEEvent], bool] | None = None,
    max_events: int,
    max_total_bytes: int,
) -> list[SSEEvent]:
    """按规范消费一条 SSE 流，返回事件列表。

    终态判定（intranet-a2a-stream-adapter）: 终止条件为「event 名等于
    terminal_event」或「is_terminal 谓词对事件返回真」，二者取或。
    a2a-sdk 0.2.x 的 message/stream wire 只发 data: 不发 event: 名
    （server/apps/jsonrpc/jsonrpc_app.py 只 yield {'data': ...}），
    因此流式 JSON-RPC 消费必须用 is_terminal 按载荷判定；
    terminal_event 保留给带事件名的对端。两者都未提供时，
    任何事件都不构成终态（由调用方保证至少提供一种判定）。

    必须消费到终态事件且收到 EOF 才算成功；
    违反任一规范立即抛错。
    """
    if max_events <= 0 or max_total_bytes <= 0:
        raise ValueError("max_events / max_total_bytes 必须为正数（禁止无界消费）")
    if terminal_event is None and is_terminal is None:
        raise ValueError("terminal_event / is_terminal 必须至少提供一个")
    content_type = response.headers.get("content-type", "")
    if not content_type.split(";")[0].strip().lower() == "text/event-stream":
        raise SSEProtocolError(f"Content-Type 不是 text/event-stream: {content_type}")

    events: list[SSEEvent] = []
    total = 0
    current: dict[str, str | None] = {"id": None, "event": None, "data": None}
    reached_terminal = False

    def _flush() -> None:
        nonlocal reached_terminal
        if current["data"] is None and current["id"] is None \
                and current["event"] is None:
            return
        if current["data"] is None:
            raise SSEProtocolError("事件缺少 data 字段")
        evt = SSEEvent(
            id=current["id"], event=current["event"], data=current["data"],
        )
        events.append(evt)
        if len(events) > max_events:
            raise SSEProtocolError(f"SSE 事件数超过上限 {max_events}")
        if (terminal_event is not None and evt.event == terminal_event) or (
            is_terminal is not None and is_terminal(evt)
        ):
            reached_terminal = True

    async for raw_line in response.aiter_lines():
        total += len(raw_line.encode()) + 1
        if total > max_total_bytes:
            raise PayloadTooLargeError("SSE 流超过总字节上限")
        line = raw_line.rstrip("\r")
        if not line:
            _flush()  # 空行 = 事件边界
            current = {"id": None, "event": None, "data": None}
            continue
        if line.startswith(":"):
            continue  # 注释行（规范允许的心跳注释）
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            if current["data"] is not None:
                raise SSEProtocolError("同一事件出现多行 data")
            current["data"] = value
        elif field == "id":
            if current["id"] is not None:
                raise SSEProtocolError("同一事件出现多个 id")
            current["id"] = value
        elif field == "event":
            if current["event"] is not None:
                raise SSEProtocolError("同一事件出现多个 event")
            current["event"] = value
        else:
            raise SSEProtocolError(f"SSE 不支持的字段: {field}")
    _flush()  # EOF 前的最后一个事件

    if not reached_terminal:
        raise SSEInterruptedError()
    return events


async def iter_sse_errors(_: AsyncIterator[bytes]) -> None:  # pragma: no cover
    """占位: 显式声明错误流处理随 message/stream 一起启用。"""
    raise NotImplementedError("message/stream 未启用")
