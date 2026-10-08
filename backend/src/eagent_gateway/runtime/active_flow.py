# -*- coding: utf-8 -*-
"""总控会话的活跃流程（流程型绑定的粘性归属）。

一行 = 某个 (master, context, subject) 当前由哪个 FLOW 绑定接管。只存路由
元数据，不存输入输出。写入/清除规则由 ``after_dispatch`` 统一执行：

- 下游本轮返回卡片或要求补充信息 → 流程未结束，upsert 并续期；
- 两者都没有，或调用失败 → 流程结束，删除（下一轮回到总控分诊）；
- 超过 ``settings.flow_idle_s`` 未续期的行视为不存在。

``awaiting`` 记录流程在等什么（card / input），预留给以后的跨 Agent 路由器
作参考信息。
"""
from __future__ import annotations

from datetime import timedelta

from ..config import settings
from ..persistence.db import Database, now

FLOW_MODE = "FLOW"


def is_flow_binding(binding: dict | None) -> bool:
    return bool(binding) and binding.get("interaction_mode") == FLOW_MODE \
        and binding.get("source_type") == "HIAGENT"


def get(db: Database, *, master_id: str, context_id: str, subject_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_master_active_flow WHERE master_id=:mid"
        " AND context_id=:ctx AND subject_id=:sub AND expires_at>:ts",
        {"mid": master_id, "ctx": context_id, "sub": subject_id, "ts": now()},
    )


def upsert(db: Database, *, master_id: str, context_id: str, subject_id: str,
           binding: dict, awaiting: str) -> None:
    ts = now()
    db.execute(
        "INSERT INTO eag_master_active_flow"
        " (master_id, context_id, subject_id, binding_id, service_id, awaiting,"
        "  updated_at, expires_at)"
        " VALUES (:mid, :ctx, :sub, :bid, :sid, :aw, :ts, :exp)"
        " ON DUPLICATE KEY UPDATE binding_id=VALUES(binding_id),"
        " service_id=VALUES(service_id), awaiting=VALUES(awaiting),"
        " updated_at=VALUES(updated_at), expires_at=VALUES(expires_at)",
        {"mid": master_id, "ctx": context_id, "sub": subject_id,
         "bid": binding["binding_id"], "sid": binding["service_id"],
         "aw": awaiting, "ts": ts, "exp": ts + timedelta(seconds=settings.flow_idle_s)},
    )


def clear(db: Database, *, master_id: str, context_id: str, subject_id: str) -> None:
    db.execute(
        "DELETE FROM eag_master_active_flow WHERE master_id=:mid"
        " AND context_id=:ctx AND subject_id=:sub",
        {"mid": master_id, "ctx": context_id, "sub": subject_id},
    )


def after_dispatch(db: Database, *, master_id: str, context_id: str, subject_id: str,
                   binding: dict | None, status: str, cards: list, input_required: bool) -> None:
    """按本轮结果维护活跃流程。只对 FLOW 绑定生效；调用方负责 commit。"""
    if not is_flow_binding(binding):
        return
    key = {"master_id": master_id, "context_id": context_id, "subject_id": subject_id}
    if status == "SUCCEEDED" and (cards or input_required):
        upsert(db, **key, binding=binding, awaiting="input" if input_required else "card")
    else:
        clear(db, **key)
