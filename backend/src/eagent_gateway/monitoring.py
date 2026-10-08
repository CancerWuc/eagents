# -*- coding: utf-8 -*-
"""DEV-12: 日志监控增强——定时探测调度 / 告警 / 远程日志纳管。

- 调度: health_probe_loop 进程内 asyncio 循环（与 instance sweep 同范式），
  对全部 enabled 服务跑 catalog.probe_health 并推导告警；间隔
  EAGENT_HEALTH_PROBE_INTERVAL_S（默认 60，0 关闭）。
- 告警规则: 连续 N 次（默认 2）UNREACHABLE → SERVICE_DOWN；已告警服务
  恢复 HEALTHY → SERVICE_RECOVERED 并关闭告警。同服务同类告警在静默窗口
  （默认 300s）内不重复。通道: EAGENT_ALERT_WEBHOOK_URL 配置则 POST JSON
  通知（失败只记日志，绝不影响主流程）。
- 远程日志: ingest_remote_logs 是唯一写入口——HTTP 端点与未来的 MQ 消费者
  都调它（消息体经 redact，与平台日志口径一致）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import httpx

from .config import settings
from .persistence.db import Database, new_id, now
from .security.redact import redact_text

logger = logging.getLogger("eagent.observability")

KIND_SERVICE_DOWN = "SERVICE_DOWN"
KIND_SERVICE_RECOVERED = "SERVICE_RECOVERED"
KIND_LATENCY_DEGRADED = "LATENCY_DEGRADED"

_WEBHOOK_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# 探测调度 + 告警推导
# ---------------------------------------------------------------------------
def _recent_observations(
    db: Database, service_id: str, limit: int = 10,
) -> list[dict]:
    return db.query_all(
        "SELECT health, latency_ms, detail, observed_at"
        " FROM eag_a2a_health_observation WHERE service_id=:sid"
        " ORDER BY observed_at DESC LIMIT :lim",
        {"sid": service_id, "lim": limit},
    )


def _open_alert(db: Database, service_id: str, kind: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_alert_event WHERE service_id=:sid AND kind=:k"
        " AND status='FIRED' ORDER BY created_at DESC LIMIT 1",
        {"sid": service_id, "k": kind},
    )


def _insert_alert(
    db: Database, *, service_id: str, kind: str, detail: str,
    status: str = "FIRED",
) -> dict:
    alert_id = new_id("alr_")
    db.execute(
        """INSERT INTO eag_alert_event
             (alert_id, service_id, kind, status, detail, notified,
              resolved_at)
           VALUES (:aid, :sid, :kind, :st, :detail, 0,
                   CASE WHEN :st = 'RESOLVED' THEN :ts ELSE NULL END)""",
        {"aid": alert_id, "sid": service_id, "kind": kind, "st": status,
         "detail": (detail or "")[:1000], "ts": now()},
    )
    return {"alert_id": alert_id, "service_id": service_id, "kind": kind,
            "detail": detail}


async def _notify_webhook(payload: dict) -> bool:
    url = settings.alert_webhook_url
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_S) as client:
            resp = await client.post(url, json=payload)
            return 200 <= resp.status_code < 300
    except Exception as exc:  # noqa: BLE001  通知失败不影响主流程
        logger.warning("alert webhook failed: %s", exc)
        return False


def derive_alerts(db: Database, service_id: str) -> list[dict]:
    """按最近观测推导告警动作（落库，不提交；返回动作列表供调度层通知）。

    规则（wiki「健康观测定时探测」跳过项的重新设计）:
    - 最近 N 次全部 UNREACHABLE 且无未关闭 SERVICE_DOWN → 触发（受静默窗）
    - 存在未关闭 SERVICE_DOWN 且最新观测 HEALTHY → 恢复并关闭
    """
    from .persistence.db import now as db_now

    actions: list[dict] = []
    recent = _recent_observations(db, service_id)
    threshold = max(1, int(settings.health_probe_consecutive_down))
    open_down = _open_alert(db, service_id, KIND_SERVICE_DOWN)

    if (not open_down and len(recent) >= threshold
            and all(r["health"] == "UNREACHABLE" for r in recent[:threshold])):
        # 静默窗口: 上一条同类已关闭告警距今不足窗口则跳过
        last = db.query_one(
            "SELECT resolved_at, created_at FROM eag_alert_event"
            " WHERE service_id=:sid AND kind=:k"
            " ORDER BY created_at DESC LIMIT 1",
            {"sid": service_id, "k": KIND_SERVICE_DOWN},
        )
        if last:
            ref = last["resolved_at"] or last["created_at"]
            if (datetime.now() - ref).total_seconds() < settings.alert_silence_s:
                return []
        detail = f"连续 {threshold} 次探测不可达: {recent[0]['detail']}"
        actions.append(_insert_alert(db, service_id=service_id,
                                     kind=KIND_SERVICE_DOWN, detail=detail))
    elif open_down and recent and recent[0]["health"] == "HEALTHY":
        db.execute(
            "UPDATE eag_alert_event SET status='RESOLVED', resolved_at=:ts"
            " WHERE alert_id=:aid",
            {"ts": db_now(), "aid": open_down["alert_id"]},
        )
        # 恢复本身也是一条告警事件（2026-08-25 实测缺陷：此前只翻状态不落
        # 行，UI 与文档承诺的 SERVICE_RECOVERED 绿标行不存在）——立即以
        # RESOLVED 落库，供列表/审计检索
        actions.append(_insert_alert(
            db, service_id=service_id, kind=KIND_SERVICE_RECOVERED,
            detail="服务恢复可达，SERVICE_DOWN 已关闭",
            status="RESOLVED",
        ))
        actions[-1]["resolved_alert_id"] = open_down["alert_id"]
    return actions


async def probe_and_derive(db: Database) -> dict:
    """一轮: 探测全部 enabled 服务 + 推导告警 + webhook 通知。"""
    from .catalog import service as catalog

    services = catalog.list_services(db)
    fired: list[dict] = []
    for svc in services:
        if not svc.get("enabled"):
            continue
        try:
            if svc.get("source_type") == "HIAGENT":
                # HIAGENT 来源：探测 = 连通性测试（建会话+发「你好」），
                # 不是 Card 拉取（hiagent-integration/01 §3.2；否则必然
                # 误报「Agent Card 拉取失败」UNREACHABLE）
                from .hiagent import service as hia_service

                await hia_service.probe_service(db, svc["service_id"])
            else:
                await catalog.probe_health(db, svc["service_id"])
        except Exception as exc:  # noqa: BLE001  单服务失败不阻断整轮
            logger.warning("probe %s failed: %s", svc["slug"], exc)
            continue
        fired.extend(derive_alerts(db, svc["service_id"]))
    db.commit()
    for action in fired:
        ok = await _notify_webhook({
            "kind": action["kind"],
            "serviceId": action["service_id"],
            "detail": action.get("detail"),
            "at": datetime.now().isoformat(),
        })
        if ok and action.get("alert_id"):
            db.execute(
                "UPDATE eag_alert_event SET notified=1 WHERE alert_id=:aid",
                {"aid": action["alert_id"]},
            )
    if fired:
        db.commit()
    return {"probed": len(services), "alert_actions": fired}


async def health_probe_loop() -> None:
    if settings.health_probe_interval_s <= 0:
        return
    logger.info(
        "health probe loop started (interval=%ss)",
        settings.health_probe_interval_s,
    )
    while True:
        db = Database()
        try:
            await probe_and_derive(db)
        except Exception:  # noqa: BLE001  循环体绝不因单轮异常退出
            logger.exception("health probe round failed")
        finally:
            db.close()
        await asyncio.sleep(settings.health_probe_interval_s)


# ---------------------------------------------------------------------------
# 远程日志纳管（HTTP 端点与未来 MQ 消费者共用入口）
# ---------------------------------------------------------------------------
def ingest_remote_logs(
    db: Database, entries: list[dict], *, source: str = "http",
) -> dict:
    """批量写入远程日志事件（redact 后落库）。

    entry: {serviceId?, level?, traceId?, runId?, message, occurredAt?}
    """
    inserted = 0
    skipped = 0
    for e in entries or []:
        message = redact_text(e.get("message") or "").strip()
        if not message:
            skipped += 1
            continue
        occurred = e.get("occurredAt") or now()
        db.execute(
            """INSERT INTO eag_remote_log_event
                 (log_id, service_id, source, level, trace_id, run_id,
                  message, occurred_at)
               VALUES (:lid, :sid, :src, :lvl, :tr, :run, :msg, :occ)""",
            {"lid": new_id("rlog_"),
             "sid": (e.get("serviceId") or e.get("service_id") or None),
             "src": source,
             "lvl": (e.get("level") or "INFO")[:16],
             "tr": (e.get("traceId") or e.get("trace_id") or None),
             "run": (e.get("runId") or e.get("run_id") or None),
             "msg": message[:2000], "occ": occurred},
        )
        inserted += 1
    db.commit()
    return {"inserted": inserted, "skipped": skipped}


def query_remote_logs(
    db: Database, *, service_id: str | None = None, trace_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    sql = "SELECT * FROM eag_remote_log_event WHERE 1=1"
    params: dict = {"lim": min(max(limit, 1), 500)}
    if service_id:
        sql += " AND service_id=:sid"
        params["sid"] = service_id
    if trace_id:
        sql += " AND trace_id=:tr"
        params["tr"] = trace_id
    sql += " ORDER BY occurred_at DESC LIMIT :lim"
    return db.query_all(sql, params)
