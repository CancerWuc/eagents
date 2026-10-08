"""Shared blocking/SSE response semantics for direct and orchestrated calls."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from .client import HiagentAPIError, extract_interrupted_options


_CARD_FENCE = re.compile(
    r"```(?P<source>canvascard|fromcreatecard)(?P<payload>.*?)```",
    re.DOTALL,
)


@dataclass
class HiAgentResult:
    text: str = ""
    options: list[dict] = field(default_factory=list)
    input_required: bool = False
    cards: list[dict] = field(default_factory=list)
    # Internal provenance; never serialized into the public card payload.
    conversation_id: str = ""
    identity_digest: str = ""
    config_version: str = ""


def extract_canvas_cards(content: str) -> list[dict]:
    """Extract opaque HiAgent card fences without imposing a business schema."""
    cards = []
    for match in _CARD_FENCE.finditer(content):
        raw = match.group("payload")
        try:
            payload = json.loads(raw.strip())
        except (TypeError, ValueError):
            payload = raw
        cards.append({
            "type": "canvas_card",
            "envelopeVersion": 1,
            "sourceFormat": match.group("source"),
            "payload": payload,
        })
    return cards


def check_error(payload: dict) -> None:
    meta = payload.get("ResponseMetadata") or {}
    error = meta.get("Error") if isinstance(meta, dict) else None
    if isinstance(error, dict) and error.get("Code"):
        raise HiagentAPIError(f"HIAGENT_UPSTREAM_ERROR: {error['Code']}")
    if payload.get("event") in {"error", "failed", "message_error"}:
        raise HiagentAPIError("HIAGENT_UPSTREAM_ERROR")


def interaction(payload: dict) -> HiAgentResult:
    raw = extract_interrupted_options(payload)
    options = [{"id": str(i), "label": str(o.get("option_content") or "")} for i, o in enumerate(raw, 1)]
    options = [o for o in options if o["label"]]
    prompt = str(payload.get("prompt") or "请补充信息")
    if options:
        prompt += "\n" + "\n".join(f"{o['id']}. {o['label']}" for o in options)
    return HiAgentResult(prompt, options, True)


def normalize_blocking(payload: dict) -> HiAgentResult:
    check_error(payload)
    if payload.get("event") == "interrupted" or payload.get("interrupted") is True:
        return interaction(payload)
    answer = payload.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise HiagentAPIError("HIAGENT_EMPTY_RESPONSE")
    return HiAgentResult(answer)


class _CardBuffer:
    """Hold only a possible fence; ordinary text can stream immediately.

    Card fences become cards. ``reference`` fences carry raw knowledge-base
    retrieval results (the schedule export's 返回reference node); they are
    internal data, so they are dropped, even when the stream ends unclosed."""
    card_markers = ("```canvascard", "```fromcreatecard")
    drop_markers = ("```reference",)
    markers = card_markers + drop_markers

    def __init__(self):
        self.pending = ""

    def feed(self, text, *, final=False):
        self.pending += text
        visible, cards = [], []
        while self.pending:
            positions = [self.pending.find(m) for m in self.markers]
            starts = [p for p in positions if p >= 0]
            if starts:
                start = min(starts)
                visible.append(self.pending[:start])
                self.pending = self.pending[start:]
                marker = next(m for m in self.markers if self.pending.startswith(m))
                end = self.pending.find("```", len(marker))
                if end < 0:
                    break
                if marker in self.card_markers:
                    cards.extend(extract_canvas_cards(self.pending[:end + 3]))
                self.pending = self.pending[end + 3:]
            else:
                keep = max((n for m in self.markers for n in range(1, len(m))
                            if self.pending.endswith(m[:n])), default=0)
                if keep:
                    visible.append(self.pending[:-keep])
                    self.pending = self.pending[-keep:]
                else:
                    visible.append(self.pending)
                    self.pending = ""
                break
        if final:
            if not self.pending.startswith(self.drop_markers):
                visible.append(self.pending)
            self.pending = ""
        return "".join(visible), cards


async def normalize_stream(events, *, on_text=None) -> HiAgentResult:
    chunks, cards = [], []
    buffers = {kind: _CardBuffer() for kind in ("message", "tool_message")}

    async def emit(kind, text="", *, final=False):
        visible, found = buffers[kind].feed(text, final=final)
        cards.extend(found)
        if visible:
            chunks.append(visible)
            if on_text is not None:
                await on_text(visible)

    completed = False
    async for event in events:
        check_error(event)
        kind = event.get("event")
        if kind in buffers:
            answer = event.get("answer")
            if not isinstance(answer, str):
                if kind == "message":
                    raise HiagentAPIError("HIAGENT_INVALID_RESPONSE")
                continue
            await emit(kind, answer)
        elif kind in {"tool_message_output_end", "message_output_end"}:
            await emit(kind.removesuffix("_output_end"), final=True)
        elif kind == "interrupted":
            return interaction(event)
        elif kind == "message_end":
            completed = True
    if not completed:
        raise HiagentAPIError("HIAGENT_INCOMPLETE_RESPONSE")
    for kind in buffers:
        await emit(kind, final=True)
    text = "".join(chunks)
    if not text.strip() and not cards:
        raise HiagentAPIError("HIAGENT_EMPTY_RESPONSE")
    return HiAgentResult(text, cards=cards)
