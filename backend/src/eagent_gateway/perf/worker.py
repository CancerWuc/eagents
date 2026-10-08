# -*- coding: utf-8 -*-
"""perf-worker 独立进程（T10/T12/T13，01 §7）。

- 轮询 eag_perf_test 中 PENDING 任务；原子 UPDATE PENDING→RUNNING 领取
  （影响行数=1 才算抢到），多 worker 实例不会重复执行同一 PENDING。
- 全局锁：API 创建端拒绝并发（409）+ 原子领取双重保障；本进程一次只
  执行一个测试。
- 取消：管理面置 cancel_flag，runner 内 CancelChecker（1s 缓存）检查点
  抛 TestCanceled → 状态 CANCELED，已发样本保留。
- 断点恢复：启动发现遗留 RUNNING（上一进程崩溃）→ 跳过已完成样本继续；
  仿真未发用例按当前时间重算触发时刻。
- 一期为单实例部署（K8s replicas: 1）；RUNNING 恢复在多实例下不加锁。

本地启动：python -m eagent_gateway.perf_worker
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import settings
from ..persistence.db import Database, now
from . import dao
from .runner import TestCanceled, build_report, run_pressure, run_simulation

logger = logging.getLogger("eagent.perf.worker")

_stop = False


async def execute_test(test: dict) -> str:
    """执行一个已 RUNNING 的测试到终态，返回终态（COMPLETED/CANCELED/FAILED）。"""
    test_id = test["perf_test_id"]
    db = Database()
    t0 = time.monotonic()
    try:
        try:
            done = dao.done_seq_nos(db, test_id)
            cases = dao.all_cases(db, test["perf_dataset_id"])
            if test["test_type"] == "SIMULATION":
                await run_simulation(db, test, cases, done)
            else:
                cfg = (test.get("config") or {}).get("pressure") or {}
                duration = float(cfg.get("durationSec") or 0)
                remaining: float | None = None
                if duration > 0 and test.get("started_at"):
                    elapsed = (now() - test["started_at"]).total_seconds()
                    remaining = max(0.0, duration - elapsed)
                await run_pressure(
                    db, test, cases,
                    start_from=len(done), duration_override=remaining,
                )
        except TestCanceled:
            logger.info("perf test %s canceled", test_id)
            dao.set_finished(
                db, test_id, status="CANCELED",
                report=build_report(
                    db, dao.get_test(db, test_id) or test,
                    wall_seconds=time.monotonic() - t0,
                ),
            )
            return "CANCELED"
        dao.set_finished(
            db, test_id, status="COMPLETED",
            report=build_report(
                db, dao.get_test(db, test_id) or test,
                wall_seconds=time.monotonic() - t0,
            ),
        )
        return "COMPLETED"
    except Exception as exc:  # noqa: BLE001
        logger.exception("perf test %s failed", test_id)
        try:
            dao.set_finished(db, test_id, status="FAILED", report={
                "error": f"{type(exc).__name__}",
                "hint": "详见 perf-worker 日志",
            })
        except Exception:  # noqa: BLE001
            logger.exception("perf test %s 状态落库失败", test_id)
        return "FAILED"
    finally:
        db.close()


async def run_once() -> str | None:
    """扫描一轮：优先恢复遗留 RUNNING，否则原子领取最老 PENDING。"""
    db = Database()
    try:
        running = dao.list_running_tests(db)
        if running:
            # 遗留 RUNNING：上一进程中断，继续执行（跳过已完成样本）
            return await execute_test(running[0])
        pending = dao.oldest_pending_test(db)
        if pending and dao.claim_test(db, pending["perf_test_id"]):
            test = dao.get_test(db, pending["perf_test_id"])
            logger.info("claimed perf test %s (%s → %s)",
                        test["perf_test_id"], test["test_type"],
                        test["target_type"])
            return await execute_test(test)
        return None
    finally:
        db.close()


async def worker_loop() -> None:
    global _stop
    _stop = False
    logger.info(
        "perf-worker started (poll=%.1fs)", settings.perf_worker_poll_s,
    )
    while not _stop:
        try:
            await run_once()
        except Exception:  # noqa: BLE001
            logger.exception("perf worker loop iteration failed")
        await asyncio.sleep(settings.perf_worker_poll_s)


def stop_loop() -> None:
    global _stop
    _stop = True


def main() -> None:
    """独立进程入口：应用迁移（幂等）后进入轮询循环。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db = Database()
    try:
        from ..persistence.migrations import apply_pending_migrations

        applied = apply_pending_migrations(db)
        if applied:
            logger.info("applied schema migrations: %s", applied)
    finally:
        db.close()
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
