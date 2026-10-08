# -*- coding: utf-8 -*-
"""Invocation Ledger（from_nekiro/04，P0-4）。

- append-only: 只插入，不修改不删除（保留期清理由后台任务负责）。
- 只存血缘与状态元数据; metadata 仅放安全字段（elapsed_ms 等），
  绝不放输入输出与密钥。
- 终端状态不可变，失败不伪装成功。
"""
from __future__ import annotations

import json

from ..persistence.db import Database, new_id, now

INVOCATION_STARTED = "INVOCATION_STARTED"
AGENT_CALL_STARTED = "AGENT_CALL_STARTED"
AGENT_CALL_FINISHED = "AGENT_CALL_FINISHED"
INVOCATION_FINISHED = "INVOCATION_FINISHED"

EVENT_TYPES = (
    INVOCATION_STARTED,
    AGENT_CALL_STARTED,
    AGENT_CALL_FINISHED,
    INVOCATION_FINISHED,
)


def append_event(
    db: Database,
    *,
    event_type: str,
    status: str,
    invocation_id: str,
    trace_id: str,
    root_task_id: str,
    parent_invocation_id: str | None = None,
    workspace_id: str | None = None,
    master_id: str | None = None,
    service_id: str | None = None,
    release_id: str | None = None,
    error_code: str | None = None,
    started_at=None,
    finished_at=None,
    metadata: dict | None = None,
) -> str:
    """追加一条调用事件并立即提交（append-only，独立于业务事务）。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type: {event_type}")
    event_id = new_id("evt_")
    db.execute(
        """
        INSERT INTO eag_invocation_event
            (event_id, invocation_id, trace_id, root_task_id,
             parent_invocation_id, workspace_id, master_id, service_id,
             release_id, event_type, status, error_code, started_at,
             finished_at, metadata_json)
        VALUES (:eid, :iid, :tid, :rid, :pid, :ws, :mid, :sid, :rel,
                :et, :st, :ec, :sat, :fat, :meta)
        """,
        {
            "eid": event_id, "iid": invocation_id, "tid": trace_id,
            "rid": root_task_id, "pid": parent_invocation_id, "ws": workspace_id,
            "mid": master_id, "sid": service_id, "rel": release_id,
            "et": event_type, "st": status, "ec": error_code,
            "sat": started_at or now(), "fat": finished_at,
            "meta": json.dumps(metadata, ensure_ascii=False) if metadata else None,
        },
    )
    db.commit()
    return event_id


_TERMINAL = {AGENT_CALL_FINISHED, INVOCATION_FINISHED}


def get_trace(db: Database, trace_id: str) -> dict | None:
    """按 trace 聚合调用树（from_nekiro/04 §6）。"""
    events = db.query_all(
        "SELECT * FROM eag_invocation_event WHERE trace_id=:t"
        " ORDER BY started_at, event_id",
        {"t": trace_id},
    )
    if not events:
        return None
    invocations: dict[str, dict] = {}
    order: list[str] = []
    root_task_id = events[0]["root_task_id"]
    for ev in events:
        iid = ev["invocation_id"]
        inv = invocations.get(iid)
        if inv is None:
            inv = {
                "invocationId": iid,
                "parentInvocationId": ev["parent_invocation_id"],
                "rootTaskId": ev["root_task_id"],
                "workspaceId": ev["workspace_id"],
                "masterId": ev["master_id"],
                "serviceId": ev["service_id"],
                "releaseId": ev["release_id"],
                "status": "RUNNING",
                "errorCode": None,
                "startedAt": ev["started_at"],
                "finishedAt": None,
                "events": [],
            }
            invocations[iid] = inv
            order.append(iid)
        inv["events"].append({
            "eventType": ev["event_type"],
            "status": ev["status"],
            "errorCode": ev["error_code"],
            "startedAt": ev["started_at"],
            "finishedAt": ev["finished_at"],
        })
        # 非空字段补齐（同 invocation 的事件可能只带部分字段）
        for src, dst in (
            ("master_id", "masterId"), ("service_id", "serviceId"),
            ("release_id", "releaseId"), ("workspace_id", "workspaceId"),
            ("parent_invocation_id", "parentInvocationId"),
        ):
            if ev[src] is not None and inv[dst] is None:
                inv[dst] = ev[src]
        if ev["event_type"] in _TERMINAL:
            inv["status"] = ev["status"]
            inv["errorCode"] = ev["error_code"]
            inv["finishedAt"] = ev["finished_at"] or ev["started_at"]
    return {
        "traceId": trace_id,
        "rootTaskId": root_task_id,
        "invocations": [invocations[iid] for iid in order],
    }
