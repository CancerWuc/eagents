# -*- coding: utf-8 -*-
"""KB 编译任务执行器（03 §2，评审 B4）。

- 同 KB 串行：同一 KB 同时只允许一个 RUNNING，新任务 PENDING 排队（02 §3.2）。
- 失败重试：retry_count<3 → 回 PENDING 整任务重跑（全流程按指纹/缓存幂等）；
  第 3 次失败终态 FAILED，对应 Rev FAILED（D9：current_rev 不动）。
- 一期形态：进程内后台循环 claim（多 worker 部署时由 claim SQL 的
  条件更新兜底串行）；二期评估持久队列（Redis Streams）。
"""
from __future__ import annotations

import asyncio
import logging

from ..config import settings
from ..persistence.db import Database
from . import compile as kb_compile
from . import dao
from .constants import TASK_MAX_RETRY

logger = logging.getLogger("eagent.kb.tasks")

# 单进程 claim 锁（多 worker 由 UPDATE ... AND status='PENDING' 兜底）；
# 按事件循环惰性创建（进程内多 loop 场景如测试复用模块）
_claim_locks: dict[int, asyncio.Lock] = {}


def _claim_lock() -> asyncio.Lock:
    key = id(asyncio.get_running_loop())
    lock = _claim_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _claim_locks[key] = lock
    return lock


_stop = False


def enqueue_task(
    db: Database, *, kb_id: str, kind: str, trigger_by: str,
) -> dict:
    """落 PENDING 任务（同 KB 已有 RUNNING 也只是排队，不拒绝）。"""
    task = dao.create_task(db, kb_id=kb_id, kind=kind, trigger_by=trigger_by)
    db.commit()
    return task


def kickoff() -> None:
    """API 返回 202 后立即尝试驱动一次执行（缩短轮询延迟）。"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(run_pending_once())
    except RuntimeError:
        pass


async def run_pending_once() -> None:
    """扫描一遍有 PENDING 任务且无 RUNNING 的 KB，逐个 claim 执行。"""
    async with _claim_lock():
        db = Database()
        try:
            kb_rows = db.query_all(
                """SELECT t.kb_id FROM eag_kb_task t
                    WHERE t.status='PENDING'
                      AND NOT EXISTS (
                          SELECT 1 FROM eag_kb_task r
                           WHERE r.kb_id=t.kb_id AND r.status='RUNNING'
                      )
                    GROUP BY t.kb_id ORDER BY MIN(t.created_at) LIMIT 10""",
            )
        finally:
            db.close()
        for row in kb_rows:
            await _claim_and_run(row["kb_id"])


async def _claim_and_run(kb_id: str) -> None:
    db = Database()
    try:
        task = dao.claim_next_pending(db, kb_id)
    finally:
        db.close()
    if task is None:
        return
    await _execute_with_retry(task)


async def _execute_with_retry(task: dict) -> None:
    task_id = task["task_id"]
    try:
        await kb_compile.execute_task(task)
        return
    except kb_compile.TaskCanceled:
        logger.info("kb task %s canceled", task_id)
        return  # 取消语义已由 compile 落库（CANCELED + Rev FAILED）
    except Exception:  # noqa: BLE001
        logger.exception("kb task %s failed", task_id)
        db = Database()
        try:
            cur = dao.get_task(db, task_id)
            if not cur or cur["status"] in ("DONE", "CANCELED"):
                return
            retry = cur["retry_count"] + 1
            if retry < TASK_MAX_RETRY:
                dao.set_task_status(
                    db, task_id, "PENDING", bump_retry=True,
                    progress_msg=f"第 {retry} 次失败，等待重试",
                )
                db.commit()
                await asyncio.sleep(min(2 ** retry, 8))
                return await _claim_and_run(cur["kb_id"])
            dao.set_task_status(
                db, task_id, "FAILED", bump_retry=True,
                progress_msg="重试 3 次后终态失败", set_finished=True,
            )
            # 对应 Rev FAILED（D9：不影响 current_rev）
            if cur.get("rev"):
                dao.finish_rev(
                    db, kb_id=cur["kb_id"], rev=cur["rev"], status="FAILED",
                    report={"note": "task failed after max retries"},
                )
                db.execute(
                    "UPDATE eag_knowledge_base SET status='FAILED'"
                    " WHERE kb_id=:k AND current_rev IS NULL",
                    {"k": cur["kb_id"]},
                )
            db.commit()
        finally:
            db.close()


async def kb_task_loop() -> None:
    """常驻循环（lifespan 启动）：0.5s 轮询 PENDING 任务。"""
    global _stop
    _stop = False
    while not _stop:
        try:
            await run_pending_once()
        except Exception:  # noqa: BLE001
            logger.exception("kb task loop iteration failed")
        await asyncio.sleep(settings.kb_task_poll_s)


def stop_loop() -> None:
    global _stop
    _stop = True


def startup_sweep() -> int:
    """进程启动清扫：上一进程滞留的 RUNNING → PENDING（幂等重跑，05 §7）。"""
    db = Database()
    try:
        n = dao.reset_stale_running(db)
        if n:
            logger.info("kb tasks: %d stale RUNNING reset to PENDING", n)
        return n
    finally:
        db.close()
