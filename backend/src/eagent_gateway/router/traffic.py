# -*- coding: utf-8 -*-
"""DEV-10: 多版本流量比例切换。

- 策略: 一个服务至多一条 ACTIVE 的 eag_traffic_policy，entries 为
  [{release_id, weight}]，合计必须 100、至少两个版本、全部 PUBLISHED 且
  属于该服务。
- 分流: resolve_target 未 pin 时优先走策略（加权随机）；选中条目不可用
  （状态/安装 fail-closed）自动让位剩余条目，全部不可用则落回默认解析链
  ——策略绝不把服务变成不可用。
- 回切: 删除（DISABLE）策略即回到默认链（工作空间最新 ENABLED 安装）。
- 审计: set/disable 全程记 eag_audit_event。
"""
from __future__ import annotations

import json
import random

from ..errors import ValidationError
from ..persistence.db import Database, new_id, now
from . import resolver


def _audit(db: Database, *, user: str, action: str, service_id: str,
           detail: dict) -> None:
    db.execute(
        """INSERT INTO eag_audit_event
             (audit_id, cap_user_name, action, resource_type, resource_id,
              detail, created_at)
           VALUES (:a, :u, :act, 'TRAFFIC_POLICY', :sid, :d, :ts)""",
        {"a": new_id("aud_"), "u": user, "act": action, "sid": service_id,
         "d": json.dumps(detail, ensure_ascii=False), "ts": now()},
    )


def set_policy(
    db: Database, *, service_id: str, entries: list[dict], created_by: str,
) -> dict:
    """设置（整体替换）服务的 ACTIVE 流量策略。

    entries: [{"releaseId"/"release_id", "weight"}]，weight 为正整数，
    合计必须 100；至少两个版本；所有 release 必须属于该服务且 PUBLISHED。
    """
    clean: list[dict] = []
    for e in entries or []:
        rid = (e.get("releaseId") or e.get("release_id") or "").strip()
        try:
            weight = int(e.get("weight", 0))
        except (TypeError, ValueError):
            raise ValidationError("weight 必须是整数") from None
        if not rid or weight <= 0:
            raise ValidationError(
                f"流量条目非法: release_id/weight 必须为非空正数（{e}）",
            )
        clean.append({"release_id": rid, "weight": weight})
    if len(clean) < 2:
        raise ValidationError("流量策略至少需要两个版本（单一版本请删除策略回切默认）")
    if sum(x["weight"] for x in clean) != 100:
        raise ValidationError("权重合计必须为 100")
    for x in clean:
        rel = resolver.get_release(db, x["release_id"])
        if not rel or rel["service_id"] != service_id:
            raise ValidationError(
                f"release {x['release_id']} 不存在或不属于该服务",
            )
        if rel["state"] != "PUBLISHED":
            raise ValidationError(
                f"release {x['release_id']} 状态为 {rel['state']}，"
                "只有 PUBLISHED 可参与分流",
            )
    # 同服务只保留一条 ACTIVE：旧行置 DISABLED（留痕不删）
    db.execute(
        "UPDATE eag_traffic_policy SET status='DISABLED', updated_at=:ts"
        " WHERE service_id=:sid AND status='ACTIVE'",
        {"sid": service_id, "ts": now()},
    )
    db.execute(
        """INSERT INTO eag_traffic_policy
             (policy_id, service_id, entries_json, status, created_by,
              created_at)
           VALUES (:pid, :sid, :entries, 'ACTIVE', :u, :ts)""",
        {"pid": new_id("tp_"), "sid": service_id,
         "entries": json.dumps(clean, ensure_ascii=False),
         "u": created_by, "ts": now()},
    )
    _audit(db, user=created_by, action="traffic.policy.set",
           service_id=service_id, detail={"entries": clean})
    db.commit()
    return get_policy(db, service_id)


def get_policy(db: Database, service_id: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM eag_traffic_policy WHERE service_id=:sid"
        " AND status='ACTIVE'",
        {"sid": service_id},
    )
    if not row:
        return None
    return _row_out(row)


def disable_policy(db: Database, *, service_id: str, user: str) -> dict:
    """一键回切：停用策略，恢复默认解析链。"""
    row = get_policy(db, service_id)
    if not row:
        from ..errors import NotFoundError

        raise NotFoundError("该服务没有生效中的流量策略")
    db.execute(
        "UPDATE eag_traffic_policy SET status='DISABLED', updated_at=:ts"
        " WHERE policy_id=:pid",
        {"pid": row["policy_id"], "ts": now()},
    )
    _audit(db, user=user, action="traffic.policy.disable",
           service_id=service_id, detail={"entries": row["entries"]})
    db.commit()
    return {"service_id": service_id, "status": "DISABLED"}


def list_policies(db: Database) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM eag_traffic_policy WHERE status='ACTIVE'"
        " ORDER BY created_at DESC",
    )
    return [_row_out(r) for r in rows]


def _row_out(row: dict) -> dict:
    return {
        "policy_id": row["policy_id"],
        "service_id": row["service_id"],
        "entries": json.loads(row["entries_json"]),
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": str(row["created_at"]),
    }


# ---------------------------------------------------------------------------
# 调用期分流
# ---------------------------------------------------------------------------
def pick_weighted(
    entries: list[dict], rng: random.Random | None = None,
) -> dict:
    """按权重选一个条目（测试可注入确定性 rng）。"""
    total = sum(e["weight"] for e in entries)
    roll = (rng or random).random() * total
    acc = 0
    for e in entries:
        acc += e["weight"]
        if roll < acc:
            return e
    return entries[-1]


def pick_weighted_target(
    db: Database, policy: dict, *, master_id: str | None = None,
    rng: random.Random | None = None,
) -> tuple[dict | None, str | None]:
    """按策略解析调用目标；选中条目不可用时让位剩余条目。

    返回 (target, None) 或 (None, reason)；全部不可用时 (None, reason)，
    调用方（resolver）此时落回默认解析链。
    """
    remaining = list(policy["entries"])
    last_reason = resolver.RELEASE_NOT_FOUND
    while remaining:
        entry = pick_weighted(remaining, rng=rng)
        remaining.remove(entry)
        target, reason = resolver.resolve_target(
            db,
            service_id=policy["service_id"],
            release_id=entry["release_id"],
            master_id=master_id,
        )
        if target is not None:
            return target, None
        last_reason = reason
    return None, last_reason
