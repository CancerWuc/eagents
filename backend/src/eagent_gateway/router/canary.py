# -*- coding: utf-8 -*-
"""DEV-11: 灰度测试（用户白名单 / 比例分组 + 指标对照 + 阈值守护）。

- 分组: 白名单用户恒走候选版；其余用户按 canary_id+工号 的稳定哈希落组
  （同一用户永远同组，多次调用不漂移）。
- 指标: 从 eag_invocation_event 按 release_id 聚合真实调用（成功率 /
  失败率 / 平均耗时 / 样本数），口径与调用链审计同源。
- 守护: evaluate 时候选样本 ≥ min_samples 且错误率超阈值 → 自动 ABORT
  （分组路由即刻失效，恢复默认解析链）；达标返回可全量标记。
- 全量: promote 把比例改 1.0（全部流量走候选版，保持观察）；稳定后 stop
  结束灰度回默认链。abort 为人工紧急停止。互斥: 同服务存在 ACTIVE
  流量策略时拒绝开灰度（两套机制不叠加）。
"""
from __future__ import annotations

import hashlib
import json

from ..errors import ConflictError, NotFoundError, ValidationError
from ..persistence.db import Database, new_id, now
from . import resolver, traffic

_DEFAULT_THRESHOLDS = {
    "max_error_rate": 0.05,
    "min_samples": 20,
}


def _audit(db: Database, *, user: str, action: str, canary: dict,
           detail: dict) -> None:
    db.execute(
        """INSERT INTO eag_audit_event
             (audit_id, cap_user_name, action, resource_type, resource_id,
              detail, created_at)
           VALUES (:a, :u, :act, 'CANARY', :cid, :d, :ts)""",
        {"a": new_id("aud_"), "u": user, "act": action,
         "cid": canary["canary_id"],
         "d": json.dumps(detail, ensure_ascii=False), "ts": now()},
    )


def get_running(db: Database, service_id: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM eag_canary_run WHERE service_id=:sid"
        " AND status='RUNNING'",
        {"sid": service_id},
    )
    return row


def _row_out(row: dict) -> dict:
    return {
        "canary_id": row["canary_id"],
        "service_id": row["service_id"],
        "candidate_release_id": row["candidate_release_id"],
        "baseline_release_id": row["baseline_release_id"],
        "status": row["status"],
        "group": json.loads(row["group_json"]),
        "thresholds": json.loads(row["thresholds_json"]),
        "created_by": row["created_by"],
        "created_at": str(row["created_at"]),
        "decided_at": str(row["decided_at"]) if row.get("decided_at") else None,
        "decision_note": row.get("decision_note"),
    }


def create_canary(
    db: Database, *, service_id: str, candidate_release_id: str,
    whitelist: list[str] | None, ratio: float,
    thresholds: dict | None, created_by: str,
) -> dict:
    if not 0.0 < ratio <= 1.0:
        raise ValidationError("ratio 必须在 (0, 1] 之间")
    cand = resolver.get_release(db, candidate_release_id)
    if not cand or cand["service_id"] != service_id:
        raise ValidationError("候选 release 不存在或不属于该服务")
    if cand["state"] != "PUBLISHED":
        raise ValidationError("候选 release 必须 PUBLISHED")
    if get_running(db, service_id):
        raise ConflictError("该服务已有进行中的灰度")
    if traffic.get_policy(db, service_id):
        raise ConflictError(
            "该服务存在生效中的流量策略（DEV-10），两套分流机制不叠加，"
            "请先删除流量策略",
        )
    # 基线 = 当前默认解析链命中的 release（此刻无灰度无策略，取的是
    # 工作空间链的稳定版本）
    base_target, reason = resolver.resolve_target(db, service_id=service_id)
    if base_target is None:
        raise ValidationError(f"无法确定基线版本（{reason}）")
    baseline_id = base_target["release"]["release_id"]
    if baseline_id == candidate_release_id:
        raise ValidationError("候选版本与当前默认版本相同，无灰度意义")
    th = {**_DEFAULT_THRESHOLDS, **(thresholds or {})}
    canary_id = new_id("cny_")
    db.execute(
        """INSERT INTO eag_canary_run
             (canary_id, service_id, candidate_release_id,
              baseline_release_id, status, group_json, thresholds_json,
              created_by, created_at)
           VALUES (:cid, :sid, :cand, :base, 'RUNNING', :grp, :th, :u, :ts)""",
        {"cid": canary_id, "sid": service_id, "cand": candidate_release_id,
         "base": baseline_id,
         "grp": json.dumps({"whitelist": whitelist or [],
                            "ratio": ratio}, ensure_ascii=False),
         "th": json.dumps(th, ensure_ascii=False),
         "u": created_by, "ts": now()},
    )
    row = db.query_one(
        "SELECT * FROM eag_canary_run WHERE canary_id=:cid", {"cid": canary_id},
    )
    _audit(db, user=created_by, action="canary.create", canary=row,
           detail={"ratio": ratio, "whitelist": whitelist or [],
                   "thresholds": th})
    db.commit()
    return _row_out(row)


def select_release_id(canary: dict, cap_user_name: str) -> str:
    """分组判定: 白名单优先；其余按稳定哈希比例。

    兼容两种入参形态: DB 原始行（group_json 字符串）与 _row_out 输出
    （"group" 已解析）。
    """
    group = (canary["group"] if "group" in canary
             else json.loads(canary["group_json"]))
    if cap_user_name in (group.get("whitelist") or []):
        return canary["candidate_release_id"]
    digest = hashlib.md5(
        f"{canary['canary_id']}:{cap_user_name}".encode("utf-8"),
    ).hexdigest()
    bucket = int(digest[:8], 16) % 10000
    return (canary["candidate_release_id"]
            if bucket < float(group["ratio"]) * 10000
            else canary["baseline_release_id"])


def metrics(db: Database, canary: dict) -> dict:
    """从调用账本按 release 聚合灰度窗口内的真实指标。"""
    rows = db.query_all(
        """SELECT release_id, status, started_at, finished_at
             FROM eag_invocation_event
            WHERE service_id=:sid AND release_id IN (:cand, :base)
              AND event_type='AGENT_CALL_FINISHED'
              AND started_at >= :since""",
        {"sid": canary["service_id"],
         "cand": canary["candidate_release_id"],
         "base": canary["baseline_release_id"],
         "since": canary["created_at"]},
    )
    out: dict[str, dict] = {}
    for rid, label in ((canary["candidate_release_id"], "candidate"),
                       (canary["baseline_release_id"], "baseline")):
        subset = [r for r in rows if r["release_id"] == rid]
        total = len(subset)
        ok = sum(1 for r in subset if r["status"] == "SUCCEEDED")
        elapsed = []
        for r in subset:
            try:
                if r["finished_at"] and r["started_at"]:
                    elapsed.append(
                        (r["finished_at"] - r["started_at"]).total_seconds()
                        * 1000,
                    )
            except (TypeError, AttributeError):
                pass
        out[label] = {
            "release_id": rid,
            "samples": total,
            "succeeded": ok,
            "error_rate": round(1 - ok / total, 4) if total else None,
            "avg_ms": round(sum(elapsed) / len(elapsed), 1)
            if elapsed else None,
        }
    return out


def evaluate(db: Database, canary_id: str, *, user: str) -> dict:
    """阈值守护: 超限自动 ABORT；达标给出可全量标记。人也可手动调。"""
    row = db.query_one(
        "SELECT * FROM eag_canary_run WHERE canary_id=:cid", {"cid": canary_id},
    )
    if not row:
        raise NotFoundError("灰度不存在")
    if row["status"] != "RUNNING":
        raise ConflictError(f"灰度已结束（{row['status']}）")
    th = json.loads(row["thresholds_json"])
    m = metrics(db, row)
    cand = m["candidate"]
    decision = {"metrics": m, "action": "HOLD", "reason": ""}
    if cand["samples"] >= int(th["min_samples"]):
        err = cand["error_rate"] or 0.0
        if err > float(th["max_error_rate"]):
            _close(db, row, "ABORTED",
                   f"候选版错误率 {err} 超阈值 {th['max_error_rate']}"
                   f"（{cand['samples']} 样本），自动暂停", user)
            decision = {"metrics": m, "action": "ABORTED",
                        "reason": "候选版超限，分组路由已失效"}
        else:
            decision = {"metrics": m, "action": "ELIGIBLE",
                        "reason": f"候选版错误率 {err} 未超阈值，可全量"}
    else:
        decision["reason"] = (
            f"样本不足（{cand['samples']}/{th['min_samples']}），继续观察"
        )
    db.commit()
    return decision


def _close(db: Database, row: dict, status: str, note: str, user: str) -> None:
    db.execute(
        "UPDATE eag_canary_run SET status=:st, decided_at=:ts,"
        " decision_note=:note WHERE canary_id=:cid",
        {"st": status, "ts": now(), "note": note[:500],
         "cid": row["canary_id"]},
    )
    _audit(db, user=user, action=f"canary.{status.lower()}", canary=row,
           detail={"note": note[:200]})


def promote(db: Database, canary_id: str, *, user: str) -> dict:
    """达标全量: 比例改 1.0（全部流量走候选版，保持灰度观察态）。"""
    row = db.query_one(
        "SELECT * FROM eag_canary_run WHERE canary_id=:cid", {"cid": canary_id},
    )
    if not row:
        raise NotFoundError("灰度不存在")
    if row["status"] != "RUNNING":
        raise ConflictError(f"灰度已结束（{row['status']}）")
    group = json.loads(row["group_json"])
    group["ratio"] = 1.0
    db.execute(
        "UPDATE eag_canary_run SET group_json=:g, decided_at=:ts,"
        " decision_note='promoted to full traffic' WHERE canary_id=:cid",
        {"g": json.dumps(group, ensure_ascii=False), "ts": now(),
         "cid": canary_id},
    )
    _audit(db, user=user, action="canary.promote", canary=row,
           detail={"ratio": 1.0})
    db.commit()
    fresh = db.query_one(
        "SELECT * FROM eag_canary_run WHERE canary_id=:cid", {"cid": canary_id},
    )
    return _row_out(fresh)


def stop(db: Database, canary_id: str, *, user: str, abort: bool) -> dict:
    """结束灰度: abort=True 紧急停止；否则正常收尾，恢复默认解析链。"""
    row = db.query_one(
        "SELECT * FROM eag_canary_run WHERE canary_id=:cid", {"cid": canary_id},
    )
    if not row:
        raise NotFoundError("灰度不存在")
    if row["status"] != "RUNNING":
        raise ConflictError(f"灰度已结束（{row['status']}）")
    status = "ABORTED" if abort else "STOPPED"
    note = "人工紧急停止" if abort else "灰度收尾"
    _close(db, row, status, note, user)
    db.commit()
    return {"canary_id": canary_id, "status": status}
