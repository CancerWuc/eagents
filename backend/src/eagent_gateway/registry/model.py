# -*- coding: utf-8 -*-
"""实例模型（from_nekiro/06 §5，P1-2）。"""
from __future__ import annotations

from dataclasses import dataclass, field

UNKNOWN = "UNKNOWN"
READY = "READY"
DRAINING = "DRAINING"
UNAVAILABLE = "UNAVAILABLE"

LIFECYCLE_STATES = (UNKNOWN, READY, DRAINING, UNAVAILABLE)


@dataclass
class InstanceInput:
    """实例注册请求。"""

    service_id: str
    instance_name: str
    endpoint: str
    lease_ttl_seconds: int = 30
    metadata: dict = field(default_factory=dict)
    state: str = READY  # 注册即 READY; DRAINING 表示排空中


@dataclass
class Instance:
    """实例快照行。"""

    instance_id: str
    service_id: str
    instance_name: str
    endpoint: str
    state: str
    ready: bool
    serving: bool
    terminating: bool
    lease_expires_at: str | None
    last_heartbeat_at: str | None
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict) -> "Instance":
        import json

        return cls(
            instance_id=row["instance_id"],
            service_id=row["service_id"],
            instance_name=row["instance_name"],
            endpoint=row["endpoint"],
            state=row["state"],
            ready=bool(row["ready"]),
            serving=bool(row["serving"]),
            terminating=bool(row["terminating"]),
            lease_expires_at=str(row["lease_expires_at"])
            if row.get("lease_expires_at") else None,
            last_heartbeat_at=str(row["last_heartbeat_at"])
            if row.get("last_heartbeat_at") else None,
            metadata=json.loads(row["metadata_json"])
            if row.get("metadata_json") else {},
        )
