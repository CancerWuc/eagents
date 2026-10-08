# -*- coding: utf-8 -*-
"""jti 重放防护（from_nekiro/05-credential-security.md §5.3，P1-1）。

一期为进程内实现（单实例部署足够：TTL 30s + 每请求唯一 jti）。
多实例部署时应替换为 Redis/MySQL 实现，key=jti 带过期。
"""
from __future__ import annotations

import threading
import time


class InMemoryReplayGuard:
    def __init__(self, ttl_seconds: int = 60) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def check_and_mark(self, jti: str) -> bool:
        """首次出现返回 True 并登记；重复出现返回 False（重放）。"""
        now = time.time()
        with self._lock:
            self._seen = {k: v for k, v in self._seen.items() if v > now}
            if jti in self._seen:
                return False
            self._seen[jti] = now + self._ttl
            return True

    def size(self) -> int:
        with self._lock:
            return len(self._seen)
