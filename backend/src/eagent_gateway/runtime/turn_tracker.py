# -*- coding: utf-8 -*-
"""跨 Agent 防环: 按 contextId 计数，超过上限拒绝（from_hermes_a2a/02 R1）。

进程内存实现（一期）+ 空闲 TTL 回收；tasks/cancel 显式重置。
"""
import threading
import time


class TurnTracker:
    _TTL = 3600.0  # 空闲 1h 后回收计数
    _HARD_MAX = 20  # 配置硬顶，防止误配成无限循环

    def __init__(self, max_turns: int = 5):
        self._max = max(1, min(int(max_turns), self._HARD_MAX))
        self._counts: dict[str, int] = {}
        self._stamps: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def max_turns(self) -> int:
        return self._max

    def register_turn(self, context_id: str) -> bool:
        """登记一轮；返回 True=放行，False=应拒绝。"""
        with self._lock:
            now_ = time.time()
            stale = [c for c, t in self._stamps.items()
                     if now_ - t > self._TTL]
            for c in stale:
                self._counts.pop(c, None)
                self._stamps.pop(c, None)
            self._counts[context_id] = self._counts.get(context_id, 0) + 1
            self._stamps[context_id] = now_
            return self._counts[context_id] <= self._max

    def reset(self, context_id: str) -> None:
        with self._lock:
            self._counts.pop(context_id, None)
            self._stamps.pop(context_id, None)


_turns: TurnTracker | None = None


def _tracker() -> TurnTracker:
    global _turns
    if _turns is None:
        from ..config import settings

        _turns = TurnTracker(max_turns=settings.max_pingpong_turns)
    return _turns


def init_turn_tracker(max_turns: int) -> None:
    global _turns
    _turns = TurnTracker(max_turns=max_turns)


def allow_turn(context_id: str) -> bool:
    return _tracker().register_turn(context_id)


def reset_turns(context_id: str) -> None:
    _tracker().reset(context_id)
