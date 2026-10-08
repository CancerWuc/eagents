# -*- coding: utf-8 -*-
"""单次 run 的阶段耗时埋点（LLM 每轮 / 工具每次 / 其余开销）。

背景: 账本原先只有两个尺度——A2A 出站的 elapsed_ms（毫秒级）和整个 run
的总时长（可能几十秒），中间"模型在想"的时间没有任何埋点，排查时只能靠
事件时间戳倒推空档。这里把 ReAct 循环拆成可归因的阶段:

- llm : 每轮模型往返（instrument_model 挂在实例的 _call_api 上，
        框架内部重试的每次尝试各记一条）
- tool: 每次工具执行（A2ATool / KBReadTool 自报；覆盖 KB 这类不出站、
        因而账本里没有事件的工具）
- 其余 = 总时长 - llm - tool，即提示词组装、上下文拼接、落库等开销

只记时间与工具名，绝不记 prompt 与输入输出（§10.5 原文不进库）。
并发工具调用会时间重叠，所以聚合用区间并集而不是求和，otherMs 才不会被
算成负数。
"""
from __future__ import annotations

import contextvars
import inspect
import time
from contextlib import aclosing, contextmanager

PHASE_LLM = "llm"
PHASE_TOOL = "tool"

# 单次 run 内最多记多少条 span（防止长会话把 run_record 撑大）
MAX_SPANS = 200

# 当前 run 的 timeline。工具在 Toolkit 的子任务里执行，contextvars 会随
# asyncio 任务继承，所以不必把 timeline 穿进每个工具的构造参数
# （standalone 容器复用同一批工具类，那条路径没有 timeline 时自动空转）。
_current: contextvars.ContextVar = contextvars.ContextVar(
    "eagent_run_timeline", default=None,
)

# DEV-04: span 开始时的进度回调（同步、绝不抛错），供 A2A message/stream
# 把 ReAct 过程推成中间事件。同样走 contextvars，工具子任务自动继承。
_progress: contextvars.ContextVar = contextvars.ContextVar(
    "eagent_run_progress", default=None,
)


@contextmanager
def use_progress(hook):
    """在这段作用域内，span 开始时会调用 hook(phase, name)。

    hook 是普通同步函数（在事件循环协程内被同步调用，需要异步动作时自行
    create_task）；hook 抛错会被吞掉——进度推送绝不允许影响业务 run。
    """
    token = _progress.set(hook)
    try:
        yield
    finally:
        _progress.reset(token)


class RunTimeline:
    """一次 run 的阶段计时器（进程内，不跨请求）。"""

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._spans: list[dict] = []
        self._dropped = 0

    # ------------------------------------------------------------------
    def add(self, phase: str, name: str, at_ms: int, elapsed_ms: int,
            *, ok: bool = True) -> None:
        if len(self._spans) >= MAX_SPANS:
            self._dropped += 1
            return
        span = {"phase": phase, "name": name, "atMs": at_ms, "ms": elapsed_ms}
        if not ok:
            span["failed"] = True
        self._spans.append(span)

    @contextmanager
    def span(self, phase: str, name: str):
        """给一段（可能含 await 的）代码计时；异常照常上抛，只标记失败。"""
        _fire_progress(phase, name)
        at = time.monotonic()
        ok = True
        try:
            yield
        except BaseException:
            ok = False
            raise
        finally:
            self.add(
                phase, name,
                int((at - self._t0) * 1000),
                int((time.monotonic() - at) * 1000),
                ok=ok,
            )

    # ------------------------------------------------------------------
    def summary(self, total_ms: int) -> dict:
        """聚合成可入库、可直接渲染的形状。"""
        llm = [s for s in self._spans if s["phase"] == PHASE_LLM]
        tool = [s for s in self._spans if s["phase"] == PHASE_TOOL]
        llm_ms = _union_ms(llm)
        tool_ms = _union_ms(tool)
        return {
            "totalMs": total_ms,
            "llmMs": llm_ms,
            "llmCalls": len(llm),
            "llmMaxMs": max((s["ms"] for s in llm), default=0),
            "toolMs": tool_ms,
            "toolCalls": len(tool),
            # 并集口径下三者相加即总时长；负数只可能来自计时误差，夹到 0
            "otherMs": max(0, total_ms - llm_ms - tool_ms),
            "droppedSpans": self._dropped,
            "spans": self._spans,
        }


def _union_ms(spans: list[dict]) -> int:
    """区间并集长度（并发 span 重叠部分只算一次）。"""
    if not spans:
        return 0
    intervals = sorted((s["atMs"], s["atMs"] + s["ms"]) for s in spans)
    total = 0
    cur_start, cur_end = intervals[0]
    for start, end in intervals[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    return total + (cur_end - cur_start)


# ---------------------------------------------------------------------------
# 采集入口
# ---------------------------------------------------------------------------
@contextmanager
def use_timeline(timeline: RunTimeline):
    """run_master 侧: 在这段作用域内工具上报会落到这个 timeline。"""
    token = _current.set(timeline)
    try:
        yield timeline
    finally:
        _current.reset(token)


@contextmanager
def record(phase: str, name: str):
    """工具侧: 没有活动 timeline 时是空操作（standalone / 单测路径）。"""
    timeline = _current.get()
    if timeline is None:
        # 无 timeline 也要让进度钩子有机会触发（A2A 流式只需要事件，
        # 不依赖计时汇总）——代价极小，两条 contextvar 判断
        _fire_progress(phase, name)
        yield
        return
    with timeline.span(phase, name):
        yield


def _fire_progress(phase: str, name: str) -> None:
    hook = _progress.get()
    if hook is None:
        return
    try:
        hook(phase, name)
    except Exception:  # noqa: BLE001  进度推送失败绝不影响业务
        pass


def instrument_model(model, timeline: RunTimeline):
    """给模型实例挂上每轮往返计时，返回同一个实例。

    ChatModelBase.__call__ 内部调的是 self._call_api，普通方法查找会先命中
    实例属性，所以覆写实例即可，不用改 AgentScope 源码，也不用包一层代理
    （代理会挡住 Agent 对 model 其他属性的访问）。重试由 __call__ 负责，
    每次尝试各记一条 span。

    Streaming models are measured until their generator is exhausted/closed.
    """
    inner = model._call_api
    label = str(getattr(model, "model", "") or "llm")

    async def timed(*args, **kwargs):
        if not getattr(model, "stream", False):
            with timeline.span(PHASE_LLM, label):
                return await inner(*args, **kwargs)
        response = await inner(*args, **kwargs)
        if not inspect.isasyncgen(response):
            return response

        async def measured_stream():
            with timeline.span(PHASE_LLM, label):
                async with aclosing(response):
                    async for chunk in response:
                        yield chunk

        return measured_stream()

    model._call_api = timed
    return model
