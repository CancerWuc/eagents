"""Bounded A2A reply artifact updates shared by both entrypoints."""
from __future__ import annotations

import time
import uuid

from a2a.types import Part


class ReplyArtifactStream:
    def __init__(self, updater, metadata: dict | None = None):
        self.updater = updater
        self.metadata = metadata or {}
        self.artifact_id = uuid.uuid4().hex
        self.buffer = ""
        self.started = False
        self.received = False
        self.sealed = False
        self.last_flush = time.monotonic()

    async def push(self, text: str) -> None:
        if not text:
            return
        self.received = True
        self.buffer += text
        if not self.started or len(self.buffer) >= 256 or time.monotonic() - self.last_flush >= 0.1:
            await self._flush(last=False)

    async def _flush(self, *, last: bool) -> None:
        if not self.buffer and not (last and self.started):
            return
        await self.updater.add_artifact(
            [Part(text=self.buffer, media_type="text/plain")],
            artifact_id=self.artifact_id, name="reply",
            metadata={**self.metadata, "provisional": not last},
            append=self.started, last_chunk=last,
        )
        self.started = True
        self.buffer = ""
        self.last_flush = time.monotonic()

    async def finish(self, full_text: str, metadata: dict | None = None) -> None:
        if self.sealed:
            if metadata:
                await self.replace(full_text, metadata)
            return
        if metadata:
            self.metadata.update(metadata)
        if not self.received and full_text:
            self.buffer = full_text
        await self._flush(last=True)

    async def replace(self, text: str, metadata: dict | None = None) -> None:
        if metadata:
            self.metadata.update(metadata)
        self.buffer = ""
        self.received = True
        await self.updater.add_artifact(
            [Part(text=text, media_type="text/plain")],
            artifact_id=self.artifact_id, name="reply",
            metadata={**self.metadata, "provisional": False},
            append=False, last_chunk=True,
        )
        self.started = True
        self.sealed = True

    async def clear(self) -> None:
        if self.started:
            await self.replace("")
        self.buffer = ""
