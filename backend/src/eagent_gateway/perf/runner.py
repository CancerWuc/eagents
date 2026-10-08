# -*- coding: utf-8 -*-
"""性能测试执行引擎（T08 压测 / T09 仿真 / T11 样本落库 / T14 报告）。

- 请求协议：A2A JSON-RPC message/send（01 §7.6），httpx.AsyncClient。
- 压测：asyncio.Semaphore 控制并发，ramp-up 线性爬坡，总量/时长先到为止。
- 仿真：按用例每日时刻调度（今天未到今天发，已过明天发），同刻并发。
- 样本：SampleBuffer 批量 flush（默认 50 条或 1 秒）。
"""
from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx

from .. import errors
from ..config import settings
from ..persistence.db import Database, new_id, now
from . import dao

# 状态映射：A2A Task.status.state → 样本状态（01 §8.2）
_STATE_SUCCEEDED = ("completed",)
_STATE_REQUIRES_INPUT = ("input-required", "requires_input")
_STATE_REJECTED = ("rejected",)
_STATE_FAILED = ("failed", "canceled")

MAX_CONCURRENCY = 500


class TestCanceled(Exception):
    """管理面请求取消（T12）。"""

    __test__ = False  # pytest 不要把该异常类当测试用例收集


# ===========================================================================
# 目标解析（创建测试时执行一次并写入 config_json.endpoint）
# ===========================================================================
def resolve_target_endpoint(
    db: Database, target_type: str, target_id: str,
) -> dict:
    """MASTER → {public_url}/a2a/{slug}/；SERVICE → 已发布 release.endpoint，
    无 release 则从 agent_card_url 推导 A2A 根地址（01 §3）。"""
    if target_type == "MASTER":
        m = db.query_one(
            "SELECT master_id, slug, display_name FROM eag_master_agent"
            " WHERE master_id=:id AND enabled=1",
            {"id": target_id},
        )
        if not m:
            raise errors.NotFoundError("Agent Team 不存在或未启用")
        return {
            "endpoint": f"{settings.public_url.rstrip('/')}/a2a/{m['slug']}/rpc",
            "slug": m["slug"],
            "name": m["display_name"],
        }
    if target_type == "SERVICE":
        svc = db.query_one(
            "SELECT service_id, slug, display_name, agent_card_url"
            " FROM eag_a2a_service_registration WHERE service_id=:id",
            {"id": target_id},
        )
        if not svc:
            raise errors.NotFoundError("A2A 服务不存在")
        rel = db.query_one(
            "SELECT endpoint FROM eag_agent_release"
            " WHERE service_id=:sid AND state='PUBLISHED'"
            " ORDER BY published_at DESC LIMIT 1",
            {"sid": target_id},
        )
        if rel and rel.get("endpoint"):
            endpoint = rel["endpoint"]
        else:
            # agent_card_url 形如 http://host/.well-known/agent.json → 根地址
            endpoint = svc["agent_card_url"].split("/.well-known/")[0] + "/"
        endpoint = endpoint if endpoint.endswith("/") else endpoint + "/"
        _ssrf_check(endpoint)
        return {"endpoint": endpoint, "slug": svc["slug"],
                "name": svc["display_name"]}
    raise errors.ValidationError("targetType 必须为 MASTER 或 SERVICE")


def _ssrf_check(endpoint: str) -> None:
    """注册服务地址过现有 SSRF 地址校验（01 §10；MASTER 为网关自身地址免检）。"""
    from ..security.pinned_http import AddressBlockedError, check_hostname

    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise errors.ValidationError("被测地址仅支持 http/https")
    host = parsed.hostname or ""
    if not host:
        raise errors.ValidationError("被测地址缺少主机名")
    try:
        check_hostname(host, allow_private=settings.allow_private_addresses)
    except AddressBlockedError as exc:
        raise errors.ValidationError(f"被测地址未通过 SSRF 校验: {exc}") from exc


# ===========================================================================
# 仿真调度（01 §7.5 / 02 §5.4）
# ===========================================================================
def next_fire_at(now_dt: datetime, time_sec: int) -> datetime:
    """今天 HH:mm:ss 未到 → 今天该时刻；已到/已过 → 明天同一时刻。"""
    fire = now_dt.replace(
        hour=time_sec // 3600,
        minute=(time_sec % 3600) // 60,
        second=time_sec % 60, microsecond=0,
    )
    if fire <= now_dt:
        fire += timedelta(days=1)
    return fire


# ===========================================================================
# 取消检查点（T12）：读 DB 有成本，按 1s 缓存
# ===========================================================================
class CancelChecker:
    def __init__(self, db: Database, test_id: str) -> None:
        self._db = db
        self._test_id = test_id
        self._checked_at = 0.0
        self._canceled = False

    def should_stop(self) -> bool:
        if self._canceled:
            return True
        if time.monotonic() - self._checked_at < 1.0:
            return False
        self._checked_at = time.monotonic()
        self._canceled = dao.is_canceled(self._db, self._test_id)
        return self._canceled


# ===========================================================================
# 样本缓冲（T11：每 50 条或 1 秒 flush）
# ===========================================================================
class SampleBuffer:
    """样本批量落库（T11）：条数/间隔阈值触发；锁内只做交换，写库在锁外。

    注意：insert_samples 在事件循环线程同步执行（与 KB 任务循环同口径），
    避免跨线程共享 SQLAlchemy session；批量 50 条下阻塞可忽略。
    """

    def __init__(self, db: Database, test_id: str) -> None:
        self._db = db
        self._test_id = test_id
        self._rows: list[dict] = []
        self._lock = asyncio.Lock()
        self._last_flush = time.monotonic()

    async def add(self, row: dict) -> None:
        rows: list[dict] = []
        async with self._lock:
            self._rows.append(row)
            if (len(self._rows) >= settings.perf_sample_flush_size
                    or time.monotonic() - self._last_flush
                    >= settings.perf_sample_flush_interval_s):
                rows, self._rows = self._rows, []
                self._last_flush = time.monotonic()
        if rows:
            dao.insert_samples(self._db, rows)

    async def flush(self) -> None:
        rows: list[dict] = []
        async with self._lock:
            if self._rows:
                rows, self._rows = self._rows, []
                self._last_flush = time.monotonic()
        if rows:
            dao.insert_samples(self._db, rows)


# ===========================================================================
# 单请求发送与响应解析（01 §7.6 / 02 §5.3）
# ===========================================================================
def _norm_text(s: str | None) -> str:
    return re.sub(r"\s+", "", (s or "")).removeprefix("TASK_STATE_").lower()


def answer_matches(expected: str | None, actual: str | None) -> int | None:
    """归一化精确/包含匹配；无期望答案时返回 None（不参与 answerMatchRate）。"""
    if expected is None or expected == "":
        return None
    e, a = _norm_text(expected), _norm_text(actual or "")
    if not a:
        return 0
    return 1 if (e == a or e in a or a in e) else 0


def _extract_result_fields(result: dict) -> tuple[str, str | None, str | None, str | None]:
    """从 A2A Task 结果提取 (state, output_text, run_id, trace_id)。"""
    status = result.get("status") or {}
    state = str(
        status.get("state") or status.get("status") or "unknown",
    ).removeprefix("TASK_STATE_").lower()
    texts: list[str] = []
    run_id = trace_id = None
    for art in result.get("artifacts") or []:
        meta = art.get("metadata") or {}
        run_id = run_id or meta.get("run_id") or meta.get("runId")
        trace_id = trace_id or meta.get("trace_id") or meta.get("traceId")
        for part in art.get("parts") or []:
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    # 状态消息兜底（rejected/failed 时输出在 status.message）
    msg = status.get("message") or {}
    for part in (msg.get("parts") or []):
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
    return state, ("\n".join(texts) or None), run_id, trace_id


def build_sample(
    *, test_id: str, seq_no: int, case: dict, sent_at, finished_at,
    status: str, latency_ms: int | None = None, error_code: str | None = None,
    error_detail: str | None = None, actual_output: str | None = None,
    trace_id: str | None = None, run_id: str | None = None,
    context_id: str | None = None,
) -> dict:
    expected = case.get("a")
    return {
        "sample_id": new_id("smp_"),
        "perf_test_id": test_id,
        "seq_no": seq_no,
        "q": case["q"],
        "expected_a": expected,
        "actual_output": actual_output,
        "status": status,
        "latency_ms": latency_ms,
        "error_code": error_code,
        "error_detail": error_detail,
        "sent_at": sent_at,
        "finished_at": finished_at,
        "trace_id": trace_id,
        "run_id": run_id,
        "context_id": context_id,
        "answer_match": answer_matches(expected, actual_output),
    }


async def send_one(
    client: httpx.AsyncClient, endpoint: str, case: dict, *,
    seq_no: int, test_id: str, cap_user: str, timeout_s: float,
    extra_headers: dict,
) -> dict:
    """发送单条 A2A message/send 并解析为样本行（02 §5.3）。"""
    context_id = f"perf-{test_id}-{seq_no}-{uuid.uuid4().hex[:8]}"
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {"message": {
            "role": "ROLE_USER",
            "messageId": uuid.uuid4().hex,
            "parts": [{"text": case["q"], "mediaType": "text/plain"}],
        }},
    }
    headers = {**(extra_headers or {}), "A2A-Version": "1.0"}
    sent_at = now()
    t0 = time.monotonic()
    try:
        resp = await client.post(
            endpoint, json=payload, headers=headers, timeout=timeout_s,
        )
    except httpx.TimeoutException:
        return build_sample(
            test_id=test_id, seq_no=seq_no, case=case, sent_at=sent_at,
            finished_at=now(), status="TIMEOUT", error_code="TIMEOUT",
            error_detail=f"超过 {timeout_s:.0f}s 未响应", context_id=context_id,
        )
    except httpx.HTTPError as exc:
        return build_sample(
            test_id=test_id, seq_no=seq_no, case=case, sent_at=sent_at,
            finished_at=now(), status="FAILED", error_code="CONNECT_ERROR",
            error_detail=f"{type(exc).__name__}: {exc}", context_id=context_id,
        )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    finished_at = now()

    if resp.status_code != 200:
        return build_sample(
            test_id=test_id, seq_no=seq_no, case=case, sent_at=sent_at,
            finished_at=finished_at, status="FAILED",
            error_code=f"HTTP_{resp.status_code}",
            error_detail=resp.text[:200], context_id=context_id,
        )
    try:
        body = resp.json()
    except ValueError:
        return build_sample(
            test_id=test_id, seq_no=seq_no, case=case, sent_at=sent_at,
            finished_at=finished_at, status="FAILED", error_code="BAD_JSON",
            error_detail="响应不是合法 JSON", context_id=context_id,
        )
    rpc_error = body.get("error")
    if isinstance(rpc_error, dict):
        return build_sample(
            test_id=test_id, seq_no=seq_no, case=case, sent_at=sent_at,
            finished_at=finished_at, status="REJECTED",
            error_code=str(rpc_error.get("code") or "RPC_ERROR")[:64],
            error_detail=str(rpc_error.get("message") or "")[:500],
            context_id=context_id,
        )
    state, output, run_id, trace_id = _extract_result_fields(
        (body.get("result") or {}).get("task") or {},
    )
    latency = elapsed_ms
    if state in _STATE_REQUIRES_INPUT:
        return build_sample(
            test_id=test_id, seq_no=seq_no, case=case, sent_at=sent_at,
            finished_at=finished_at, status="SUCCEEDED", latency_ms=latency,
            error_code="REQUIRES_INPUT", actual_output=output,
            trace_id=trace_id, run_id=run_id, context_id=context_id,
        )
    if state in _STATE_SUCCEEDED:
        return build_sample(
            test_id=test_id, seq_no=seq_no, case=case, sent_at=sent_at,
            finished_at=finished_at, status="SUCCEEDED", latency_ms=latency,
            actual_output=output, trace_id=trace_id, run_id=run_id,
            context_id=context_id,
        )
    if state in _STATE_REJECTED:
        return build_sample(
            test_id=test_id, seq_no=seq_no, case=case, sent_at=sent_at,
            finished_at=finished_at, status="REJECTED", latency_ms=latency,
            error_code="REJECTED", actual_output=output,
            trace_id=trace_id, run_id=run_id, context_id=context_id,
        )
    return build_sample(
        test_id=test_id, seq_no=seq_no, case=case, sent_at=sent_at,
        finished_at=finished_at, status="FAILED", latency_ms=latency,
        error_code="STATE_" + state.upper()[:40], actual_output=output,
        error_detail=f"非成功终态: {state}", trace_id=trace_id,
        run_id=run_id, context_id=context_id,
    )


# ===========================================================================
# 压测执行器（T08）
# ===========================================================================
async def run_pressure(
    db: Database, test: dict, cases: list[dict], *,
    start_from: int = 0, duration_override: float | None = None,
) -> None:
    """压测执行（T08）。断点恢复：start_from 之前的序号已发（不重发），
    duration_override 为剩余时长（worker 重启后按原 started_at 折算）。"""
    cfg = (test.get("config") or {}).get("pressure") or {}
    test_id = test["perf_test_id"]
    endpoint = test["config"]["endpoint"]
    concurrency = max(1, min(int(cfg.get("concurrency") or 5), MAX_CONCURRENCY))
    total = int(cfg.get("totalRequests") or 0)
    duration = duration_override if duration_override is not None \
        else float(cfg.get("durationSec") or 0)
    ramp = float(cfg.get("rampUpSec") or 0)
    timeout_s = max(1.0, (cfg.get("timeoutMs") or 60000) / 1000.0)
    cap_user = cfg.get("capUser") or "perf-tester"
    order = (cfg.get("caseOrder") or "ROUND_ROBIN").upper()
    extra_headers = cfg.get("headers") or {}
    if not cases:
        raise errors.ValidationError("数据集没有用例，无法压测")
    if total <= 0 and duration <= 0 and duration_override is None:
        raise errors.ValidationError("totalRequests 与 durationSec 至少配置一项")

    rng = random.Random()
    checker = CancelChecker(db, test_id)
    buffer = SampleBuffer(db, test_id)
    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()
    deadline = started + duration if duration > 0 else None
    if total <= 0 and deadline is None:
        return  # 剩余时长已耗尽且无总量目标（重启恢复场景）
    inflight = 0
    launched = start_from
    tasks: list[asyncio.Task] = []

    def pick_case() -> dict:
        if order == "RANDOM":
            return rng.choice(cases)
        return cases[launched % len(cases)]

    limits = httpx.Limits(
        max_connections=concurrency + 16,
        max_keepalive_connections=concurrency,
    )
    async with httpx.AsyncClient(limits=limits) as client:
        while True:
            if total > 0 and launched >= total:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if checker.should_stop():
                raise TestCanceled()
            # ramp-up：允许的在途数线性爬到目标并发（01 §7.4）
            if ramp > 0 and inflight >= _ramp_allowed(
                concurrency, ramp, time.monotonic() - started,
            ):
                await asyncio.sleep(0.02)
                continue
            await sem.acquire()
            launched += 1
            seq = launched
            case = pick_case()

            async def _work(seq_no: int, c: dict) -> None:
                nonlocal inflight
                try:
                    row = await send_one(
                        client, endpoint, c, seq_no=seq_no, test_id=test_id,
                        cap_user=cap_user, timeout_s=timeout_s,
                        extra_headers=extra_headers,
                    )
                    await buffer.add(row)
                finally:
                    inflight -= 1
                    sem.release()

            inflight += 1
            tasks.append(asyncio.create_task(_work(seq, case)))
        if tasks:
            await asyncio.gather(*tasks)
    await buffer.flush()


def _ramp_allowed(concurrency: int, ramp: float, elapsed: float) -> int:
    if ramp <= 0:
        return concurrency
    return max(1, min(concurrency,
                      int(concurrency * elapsed / ramp) + 1))


# ===========================================================================
# 仿真执行器（T09；断点恢复语义见 worker）
# ===========================================================================
async def run_simulation(
    db: Database, test: dict, cases: list[dict], done_seqs: set[int],
) -> None:
    cfg = (test.get("config") or {}).get("simulation") or {}
    test_id = test["perf_test_id"]
    endpoint = test["config"]["endpoint"]
    timeout_s = max(1.0, (cfg.get("timeoutMs") or 60000) / 1000.0)
    cap_user = cfg.get("capUser") or "perf-tester"
    max_concurrency = int(cfg.get("maxConcurrency") or 0)
    extra_headers = cfg.get("headers") or {}

    pending = [c for c in cases if c["seq_no"] not in done_seqs]
    checker = CancelChecker(db, test_id)
    buffer = SampleBuffer(db, test_id)
    sem = asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None

    # 同一时刻的用例自然并发（01 §7.5）：按 fire_at 分组到点齐发
    groups: dict[datetime, list[dict]] = {}
    base = datetime.now()
    for c in pending:
        groups.setdefault(next_fire_at(base, c["time_sec"]), []).append(c)

    async with httpx.AsyncClient() as client:
        tasks: list[asyncio.Task] = []
        for fire_at in sorted(groups):
            while True:
                if checker.should_stop():
                    raise TestCanceled()
                remain = (fire_at - datetime.now()).total_seconds()
                if remain <= 0:
                    break
                await asyncio.sleep(min(0.5, remain))
            for case in groups[fire_at]:

                async def _work(c: dict = case) -> None:
                    if sem is not None:
                        await sem.acquire()
                    try:
                        row = await send_one(
                            client, endpoint, c, seq_no=c["seq_no"],
                            test_id=test_id, cap_user=cap_user,
                            timeout_s=timeout_s, extra_headers=extra_headers,
                        )
                        await buffer.add(row)
                    finally:
                        if sem is not None:
                            sem.release()

                tasks.append(asyncio.create_task(_work()))
        if tasks:
            await asyncio.gather(*tasks)
    await buffer.flush()


# ===========================================================================
# 报告计算（T14，口径见 01 §8）
# ===========================================================================
def _percentile(sorted_vals: list[int], pct: float) -> int | None:
    if not sorted_vals:
        return None
    rank = max(1, int(round(pct / 100.0 * len(sorted_vals))))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


def build_report(
    db: Database, test: dict, *, wall_seconds: float | None = None,
) -> dict:
    rows = dao.all_samples_for_report(db, test["perf_test_id"])
    total = len(rows)
    succeeded = sum(1 for r in rows if r["status"] == "SUCCEEDED")
    timed_out = sum(1 for r in rows if r["status"] == "TIMEOUT")
    rejected = sum(1 for r in rows if r["status"] == "REJECTED")
    failed = sum(1 for r in rows if r["status"] == "FAILED")
    lat = sorted(
        r["latency_ms"] for r in rows
        if r["status"] == "SUCCEEDED" and r.get("latency_ms") is not None
    )
    matched = [r for r in rows if r.get("answer_match") is not None]
    error_counter: Counter = Counter()
    for r in rows:
        if r["status"] == "SUCCEEDED":
            continue
        code = str(r.get("error_code") or "")
        if r["status"] == "TIMEOUT":
            bucket = "TIMEOUT"
        elif r["status"] == "REJECTED":
            bucket = "ANTI_LOOP_REJECTED" if "loop" in code.lower() \
                else "REJECTED"
        else:
            bucket = "OTHER"
        error_counter[bucket] += 1
    elapsed = wall_seconds
    if elapsed is None and test.get("started_at") and test.get("finished_at"):
        try:
            elapsed = (test["finished_at"] - test["started_at"]).total_seconds()
        except TypeError:
            elapsed = None
    return {
        "summary": {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "timedOut": timed_out,
            "rejected": rejected,
            "requiresInput": sum(
                1 for r in rows
                if r["status"] == "SUCCEEDED" and r.get("error_code")
                == "REQUIRES_INPUT"
            ),
            "successRate": round(succeeded / total, 4) if total else None,
            "throughput": round(total / elapsed, 2)
            if elapsed and elapsed > 0 else None,
            "latencyMs": {
                "min": lat[0] if lat else None,
                "avg": round(sum(lat) / len(lat)) if lat else None,
                "p50": _percentile(lat, 50),
                "p95": _percentile(lat, 95),
                "p99": _percentile(lat, 99),
                "max": lat[-1] if lat else None,
            },
            "answerMatchRate": round(
                sum(1 for r in matched if r["answer_match"]) / len(matched), 4,
            ) if matched else None,
            "errorBreakdown": dict(
                error_counter.most_common(8),
            ),
        },
        "target": {
            "targetType": test["target_type"],
            "targetId": test["target_id"],
            "slug": (test.get("config") or {}).get("slug"),
        },
    }
