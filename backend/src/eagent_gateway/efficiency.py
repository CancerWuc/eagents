# -*- coding: utf-8 -*-
"""DEV-17: 人效提升工具——FAQ 自动生成 / 知识缺失告警 / 审批入库。

- FAQ 挖掘: SUCCEEDED 且走了 KB 工具的运行记录按归一化问题聚类，
  Q/A 逐字取自真实运行（redact 后落库，不做任何改写——挖掘的是历史，
  不是生成，无幻觉面）。
- 知识缺口: kb_search 拒答（no_reliable_hit 出现在工具输出或最终回复）
  聚类计数，master+归一化问题 唯一，重复出现累加。
- 审批入库: APPROVED 的 FAQ 合成 Markdown 源文件走 kb service
  upload_source（触发入库规划），状态 INGESTED 并记录 source_id。
"""
from __future__ import annotations

import json
import re

from .errors import ConflictError, NotFoundError
from .persistence.db import Database, new_id, now
from .security.redact import redact_text

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS_RE.sub("", (text or "")).lower()[:500]


def _kb_direct_answer_runs(db: Database, master_id: str | None) -> list[dict]:
    sql = """SELECT run_id, master_id, input_text, final_output,
                    routed_services
               FROM eag_run_record
              WHERE status='SUCCEEDED'
                AND final_output IS NOT NULL
                AND routed_services LIKE '%kb_%'"""
    params: dict = {}
    if master_id:
        sql += " AND master_id=:mid"
        params["mid"] = master_id
    sql += " ORDER BY started_at DESC LIMIT 2000"
    return db.query_all(sql, params)


# ---------------------------------------------------------------------------
# FAQ 自动生成
# ---------------------------------------------------------------------------
def generate_faqs(db: Database, *, master_id: str | None = None) -> dict:
    """从运行记录挖高频问答对 → 候选 FAQ（跳过已存在的非 REJECTED 问题）。"""
    clusters: dict[str, dict] = {}
    for r in _kb_direct_answer_runs(db, master_id):
        key = _norm(r["input_text"])
        if not key:
            continue
        c = clusters.setdefault(key, {
            "master_id": r["master_id"], "question": r["input_text"],
            "answer": r["final_output"], "freq": 0, "run_id": r["run_id"],
        })
        c["freq"] += 1

    # 去重键 = (master_id, 问题)：同一问题在不同总控下是独立候选
    existing = {
        (row["mid"], row["qn"]) for row in db.query_all(
            "SELECT master_id AS mid, question AS qn"
            " FROM eag_faq_candidate"
            " WHERE status IN ('PENDING','APPROVED','INGESTED')",
        )
    }
    created = 0
    for key, c in sorted(clusters.items(), key=lambda kv: -kv[1]["freq"]):
        if (c["master_id"], c["question"]) in existing:
            continue
        db.execute(
            """INSERT INTO eag_faq_candidate
                 (faq_id, master_id, question, answer, freq, source_run_id)
               VALUES (:fid, :mid, :q, :a, :freq, :run)""",
            {"fid": new_id("faq_"), "mid": c["master_id"],
             "q": redact_text(c["question"])[:1000],
             "a": redact_text(c["answer"])[:60000],
             "freq": c["freq"], "run": c["run_id"]},
        )
        created += 1
    db.commit()
    return {"clusters": len(clusters), "created": created}


def list_faqs(db: Database, *, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM eag_faq_candidate"
    params: dict = {}
    if status:
        sql += " WHERE status=:st"
        params["st"] = status
    sql += " ORDER BY freq DESC, created_at DESC LIMIT 200"
    return db.query_all(sql, params)


async def decide_faq(
    db: Database, faq_id: str, *, user: str, approve: bool,
    kb_id: str | None = None,
) -> dict:
    """审批。通过且给出 kb_id 时自动进入入库规划（合成 Markdown 源上传）。"""
    faq = db.query_one(
        "SELECT * FROM eag_faq_candidate WHERE faq_id=:fid", {"fid": faq_id},
    )
    if not faq:
        raise NotFoundError("FAQ 候选不存在")
    if faq["status"] != "PENDING":
        raise ConflictError(f"FAQ 已处理过（{faq['status']}）")
    if approve:
        db.execute(
            "UPDATE eag_faq_candidate SET status='APPROVED', decided_at=:ts"
            " WHERE faq_id=:fid",
            {"ts": now(), "fid": faq_id},
        )
        source_id = None
        if kb_id:
            from .kb import service as kb_service

            doc = (
                f"# {faq['question'].strip()}\n\n"
                "## 答案\n\n" + faq["answer"].strip() + "\n"
            )
            result = await kb_service.upload_source(
                db, user=user, kb_id=kb_id,
                file_name=f"faq-{faq_id}.md",
                data=doc.encode("utf-8"), mime_type="text/markdown",
                trigger_planning=True,
            )
            source_id = result.get("source_id")
            db.execute(
                "UPDATE eag_faq_candidate SET status='INGESTED',"
                " ingested_source_id=:sid WHERE faq_id=:fid",
                {"sid": source_id, "fid": faq_id},
            )
        db.commit()
        row = db.query_one(
            "SELECT * FROM eag_faq_candidate WHERE faq_id=:fid",
            {"fid": faq_id},
        )
        return {"faq_id": faq_id, "status": row["status"],
                "ingested_source_id": source_id}
    db.execute(
        "UPDATE eag_faq_candidate SET status='REJECTED', decided_at=:ts"
        " WHERE faq_id=:fid",
        {"ts": now(), "fid": faq_id},
    )
    db.commit()
    return {"faq_id": faq_id, "status": "REJECTED"}


# ---------------------------------------------------------------------------
# 知识缺失告警（no_reliable_hit 拒答聚类）
# ---------------------------------------------------------------------------
_NO_HIT_MARKERS = ("no_reliable_hit", "没有可靠命中", "无可靠资料")


def scan_gaps(db: Database, *, master_id: str | None = None) -> dict:
    """扫运行记录里的 KB 拒答，聚类进知识缺口清单。

    水位线语义（2026-08-25 实测缺陷修复）：
    - 只统计 started_at 晚于该缺口 last_seen_at 的**新增**拒答——修复
      重复扫描把同一批旧运行反复累加的虚高计数；
    - 已 RESOLVED 的缺口再次出现新拒答时自动回到 OPEN——修复"扫描
      说聚类 N 组、清单却为空"的自相矛盾体感。
    """
    sql = """SELECT run_id, master_id, input_text, final_output, tool_calls,
                    started_at
               FROM eag_run_record
              WHERE routed_services LIKE '%kb_%'"""
    params: dict = {}
    if master_id:
        sql += " AND master_id=:mid"
        params["mid"] = master_id
    sql += " ORDER BY started_at DESC LIMIT 2000"
    rows = db.query_all(sql, params)

    clusters: dict[tuple, dict] = {}
    for r in rows:
        hay = " ".join(
            [r["final_output"] or "", r["tool_calls"] or ""],
        )
        if not any(m in hay for m in _NO_HIT_MARKERS):
            continue
        key = (r["master_id"], _norm(r["input_text"]))
        c = clusters.setdefault(key, {
            "master_id": r["master_id"], "query_text": r["input_text"],
            "seen_at": [], "last_seen_at": r["started_at"],
        })
        c["seen_at"].append(r["started_at"])
        if r["started_at"] > c["last_seen_at"]:
            c["last_seen_at"] = r["started_at"]

    existing = {
        (row["master_id"], row["query_norm"]): row
        for row in db.query_all(
            "SELECT gap_id, master_id, query_norm, last_seen_at, hit_count"
            " FROM eag_knowledge_gap",
        )
    }
    upserted = 0
    for (mid, qn), c in clusters.items():
        prev = existing.get((mid, qn))
        if prev is not None:
            new_hits = [t for t in c["seen_at"] if t > prev["last_seen_at"]]
            if not new_hits:
                continue  # 无新增：不动计数、不动状态（resolved 保持）
            db.execute(
                """UPDATE eag_knowledge_gap
                     SET hit_count=hit_count+:n, last_seen_at=:ls,
                         query_text=:qt,
                         status=CASE WHEN status='RESOLVED'
                                     THEN 'OPEN' ELSE status END
                   WHERE gap_id=:gid""",
                {"n": len(new_hits), "ls": c["last_seen_at"],
                 "qt": c["query_text"][:1000], "gid": prev["gap_id"]},
            )
            upserted += 1
            continue
        db.execute(
            """INSERT INTO eag_knowledge_gap
                 (gap_id, master_id, query_norm, query_text, hit_count,
                  last_seen_at)
               VALUES (:gid, :mid, :qn, :qt, :hc, :ls)""",
            {"gid": new_id("gap_"), "mid": mid, "qn": qn,
             "qt": c["query_text"][:1000], "hc": len(c["seen_at"]),
             "ls": c["last_seen_at"]},
        )
        upserted += 1
    db.commit()
    return {"clusters": len(clusters), "upserted": upserted}


def list_gaps(db: Database, *, status: str = "OPEN") -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_knowledge_gap WHERE status=:st"
        " ORDER BY hit_count DESC, last_seen_at DESC LIMIT 200",
        {"st": status},
    )


def resolve_gap(db: Database, gap_id: str) -> dict:
    result = db.execute(
        "UPDATE eag_knowledge_gap SET status='RESOLVED' WHERE gap_id=:gid",
        {"gid": gap_id},
    )
    db.commit()
    if not getattr(result, "rowcount", 0):
        raise NotFoundError("知识缺口不存在")
    return {"gap_id": gap_id, "status": "RESOLVED"}
