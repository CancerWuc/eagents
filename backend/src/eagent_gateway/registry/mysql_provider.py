# -*- coding: utf-8 -*-
"""MySQL 实例目录（from_nekiro/06，P1-2 一期 Provider）。

- 注册按 (service_id, endpoint) 幂等 upsert，写入租约。
- 心跳续租并刷新 last_heartbeat_at。
- 路由只选 READY 且租约未过期的实例。
- 过期实例由后台清扫任务标记 UNAVAILABLE（sweep_expired_instances）。
"""
from __future__ import annotations

import json
from datetime import timedelta

from ..errors import ConflictError, NotFoundError
from ..persistence.db import Database, new_id, now
from .model import DRAINING, READY, UNAVAILABLE, Instance, InstanceInput


class MysqlInstanceDirectory:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    def _get(self, instance_id: str) -> dict | None:
        return self._db.query_one(
            "SELECT * FROM eag_agent_instance WHERE instance_id=:iid",
            {"iid": instance_id},
        )

    def _require(self, instance_id: str) -> dict:
        row = self._get(instance_id)
        if not row:
            raise NotFoundError("实例不存在")
        return row

    # ------------------------------------------------------------------
    def snapshot(self, service_id: str) -> list[Instance]:
        rows = self._db.query_all(
            "SELECT * FROM eag_agent_instance WHERE service_id=:sid"
            " AND (lease_expires_at IS NULL OR lease_expires_at > NOW(6))"
            " ORDER BY created_at",
            {"sid": service_id},
        )
        return [Instance.from_row(r) for r in rows]

    def ready_instances(self, service_id: str) -> list[Instance]:
        rows = self._db.query_all(
            "SELECT * FROM eag_agent_instance WHERE service_id=:sid"
            " AND state=:ready AND ready=1 AND serving=1 AND terminating=0"
            " AND lease_expires_at > NOW(6)"
            " ORDER BY created_at",
            {"sid": service_id, "ready": READY},
        )
        return [Instance.from_row(r) for r in rows]

    def has_instances(self, service_id: str) -> bool:
        return self._db.query_one(
            "SELECT 1 AS x FROM eag_agent_instance WHERE service_id=:sid LIMIT 1",
            {"sid": service_id},
        ) is not None

    # ------------------------------------------------------------------
    def register(self, instance: InstanceInput) -> Instance:
        existing = self._db.query_one(
            "SELECT * FROM eag_agent_instance WHERE service_id=:sid"
            " AND endpoint=:ep",
            {"sid": instance.service_id, "ep": instance.endpoint},
        )
        ts = now()
        lease = ts + timedelta(seconds=instance.lease_ttl_seconds)
        ready = 1 if instance.state == READY else 0
        if existing:
            if existing["terminating"]:
                raise ConflictError("实例已注销登记中，不允许重复注册同地址")
            self._db.execute(
                """
                UPDATE eag_agent_instance
                   SET instance_name=:name, state=:state, ready=:ready,
                       serving=1, terminating=0, lease_expires_at=:lease,
                       last_heartbeat_at=:ts, metadata_json=:meta,
                       updated_at=:ts
                 WHERE instance_id=:iid
                """,
                {
                    "name": instance.instance_name, "state": instance.state,
                    "ready": ready, "lease": lease, "ts": ts,
                    "meta": json.dumps(instance.metadata, ensure_ascii=False)
                    if instance.metadata else None,
                    "iid": existing["instance_id"],
                },
            )
            self._db.commit()
            return Instance.from_row(self._require(existing["instance_id"]))
        instance_id = new_id("ins_")
        self._db.execute(
            """
            INSERT INTO eag_agent_instance
                (instance_id, service_id, instance_name, endpoint, state,
                 ready, serving, terminating, lease_expires_at,
                 last_heartbeat_at, metadata_json, created_at, updated_at)
            VALUES (:iid, :sid, :name, :ep, :state, :ready, 1, 0, :lease,
                    :ts, :meta, :ts, :ts)
            """,
            {
                "iid": instance_id, "sid": instance.service_id,
                "name": instance.instance_name, "ep": instance.endpoint,
                "state": instance.state, "ready": ready, "lease": lease,
                "ts": ts,
                "meta": json.dumps(instance.metadata, ensure_ascii=False)
                if instance.metadata else None,
            },
        )
        self._db.commit()
        return Instance.from_row(self._require(instance_id))

    def heartbeat(self, instance_id: str, ttl_seconds: int) -> Instance:
        row = self._require(instance_id)
        if row["terminating"]:
            raise ConflictError("实例已注销，无法续租")
        ts = now()
        self._db.execute(
            """
            UPDATE eag_agent_instance
               SET lease_expires_at=:lease, last_heartbeat_at=:ts,
                   state=CASE WHEN state=:draining THEN :draining ELSE :ready END,
                   ready=CASE WHEN state=:draining THEN 0 ELSE 1 END,
                   updated_at=:ts
             WHERE instance_id=:iid
            """,
            {
                "lease": ts + timedelta(seconds=ttl_seconds), "ts": ts,
                "draining": DRAINING, "ready": READY, "iid": instance_id,
            },
        )
        self._db.commit()
        return Instance.from_row(self._require(instance_id))

    def mark_draining(self, instance_id: str) -> Instance:
        """排空: 不再接收新调用，已注册地址保留到租约过期。"""
        self._require(instance_id)
        self._db.execute(
            "UPDATE eag_agent_instance SET state=:draining, ready=0,"
            " updated_at=:ts WHERE instance_id=:iid",
            {"draining": DRAINING, "ts": now(), "iid": instance_id},
        )
        self._db.commit()
        return Instance.from_row(self._require(instance_id))

    def deregister(self, instance_id: str) -> Instance:
        self._require(instance_id)
        self._db.execute(
            "UPDATE eag_agent_instance SET state=:unavailable, ready=0,"
            " serving=0, terminating=1, updated_at=:ts WHERE instance_id=:iid",
            {"unavailable": UNAVAILABLE, "ts": now(), "iid": instance_id},
        )
        self._db.commit()
        return Instance.from_row(self._require(instance_id))


def sweep_expired_instances(db: Database) -> int:
    """把租约已过期的实例标记 UNAVAILABLE（后台任务调用）。返回行数。"""
    result = db.execute(
        "UPDATE eag_agent_instance SET state=:unavailable, ready=0"
        " WHERE lease_expires_at IS NOT NULL AND lease_expires_at <= NOW(6)"
        " AND state IN (:ready, :unknown, :draining)",
        {
            "unavailable": UNAVAILABLE, "ready": READY,
            "unknown": "UNKNOWN", "draining": DRAINING,
        },
    )
    db.commit()
    return result.rowcount
