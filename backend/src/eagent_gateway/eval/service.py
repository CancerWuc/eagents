# -*- coding: utf-8 -*-
"""能力④-1: 评测集（来自运行数据抽样）+ 人工标注。"""
import json

from ..persistence.db import Database, new_id, now
from ..security.redact import redact_text


def create_dataset_from_runs(
    db: Database, *, name: str, master_id: str, run_ids: list[str],
) -> dict:
    """从运行记录抽样创建评测集（脱敏后入集）。"""
    if not run_ids:
        runs = db.query_all(
            "SELECT * FROM eag_run_record WHERE master_id=:mid"
            " AND status='SUCCEEDED' ORDER BY started_at DESC LIMIT 50",
            {"mid": master_id},
        )
    else:
        placeholders = ", ".join(f":r{i}" for i in range(len(run_ids)))
        params = {f"r{i}": r for i, r in enumerate(run_ids)}
        runs = db.query_all(
            f"SELECT * FROM eag_run_record WHERE run_id IN ({placeholders})",
            params,
        )
    last = db.query_one(
        "SELECT MAX(version_no) AS v FROM eag_eval_dataset WHERE name=:n",
        {"n": name},
    )
    version_no = (last["v"] or 0) + 1
    dataset_id = new_id("ds_")
    db.execute(
        "INSERT INTO eag_eval_dataset (dataset_id, master_id, name, version_no,"
        " status, created_at) VALUES (:did, :mid, :n, :v, 'ACTIVE', :ts)",
        {"did": dataset_id, "mid": master_id, "n": name,
         "v": version_no, "ts": now()},
    )
    routed = None
    for run in runs:
        routed = json.loads(run["routed_services"] or "[]")
        db.execute(
            """INSERT INTO eag_eval_case
                 (case_id, dataset_id, input_text, expected_service,
                  source_run_id, created_at)
               VALUES (:cid, :did, :inp, :exp, :run, :ts)""",
            {
                "cid": new_id("case_"), "did": dataset_id,
                "inp": run["input_text"] or "",
                # 默认期望值取实际命中第一个（标注者可纠正）
                "exp": (routed[0] if routed else None),
                "run": run["run_id"], "ts": now(),
            },
        )
    db.commit()
    return {"dataset_id": dataset_id, "name": name,
            "version_no": version_no, "cases": len(runs)}


def list_datasets(db: Database, master_id: str | None = None) -> list[dict]:
    if master_id:
        return db.query_all(
            "SELECT * FROM eag_eval_dataset WHERE master_id=:mid"
            " ORDER BY created_at DESC", {"mid": master_id},
        )
    return db.query_all(
        "SELECT * FROM eag_eval_dataset ORDER BY created_at DESC",
    )


def list_cases(db: Database, dataset_id: str) -> list[dict]:
    """用例 + 标注历史 + 源运行的 trace_id。

    带出 trace_id 是为了让标注工作台能直接跳到调用链: 只看一句用户原话就要
    判"意图识别对不对"是判不了的——调了哪个工具、返回了什么、走了几轮，
    都在 Trace 里。
    """
    cases = db.query_all(
        "SELECT c.*, r.trace_id AS source_trace_id,"
        "       r.status AS source_status, r.error_code AS source_error_code"
        "  FROM eag_eval_case c"
        "  LEFT JOIN eag_run_record r ON r.run_id = c.source_run_id"
        " WHERE c.dataset_id=:did ORDER BY c.created_at",
        {"did": dataset_id},
    )
    for c in cases:
        c["annotations"] = db.query_all(
            "SELECT * FROM eag_annotation WHERE case_id=:cid"
            " ORDER BY created_at DESC",
            {"cid": c["case_id"]},
        )
    return cases


def add_annotation(
    db: Database, *, case_id: str, cap_user_name: str,
    intent_correct: bool | None, subjective_note: str | None,
    corrected_service: str | None = None,
    expected_text: str | None = None,
) -> dict:
    """标注以追加记录关联原用例，不改写原始记录（§7.1）。

    两个例外都会回写用例（理由同 013/014 迁移注释——期望值建集时取自
    源运行，源运行本身错了就永远错，必须给标注者纠正入口）：
    - corrected_service: "正确答案该是哪个服务" → 回写 expected_service
    - expected_text（DEV-07）: "标准答案文本" → 回写 expected_text，
      参与 BLEU / ROUGE / F1 文本指标；同样经 redact（人工输入口径一致）
    标注记录本身仍是只追加的。
    """
    annotation_id = new_id("ann_")
    corrected = (corrected_service or "").strip() or None
    answer_text = redact_text(expected_text).strip() or None
    db.execute(
        """INSERT INTO eag_annotation
             (annotation_id, case_id, cap_user_name, intent_correct,
              corrected_service, corrected_text, subjective_note, created_at)
           VALUES (:aid, :cid, :u, :ic, :fix, :fixtext, :note, :ts)""",
        {"aid": annotation_id, "cid": case_id, "u": cap_user_name,
         "ic": None if intent_correct is None else int(intent_correct),
         "fix": corrected,
         "fixtext": answer_text,
         # 人工输入同样脱敏（可能粘贴了含敏感信息的原文）
         "note": redact_text(subjective_note), "ts": now()},
    )
    if corrected:
        db.execute(
            "UPDATE eag_eval_case SET expected_service=:exp"
            " WHERE case_id=:cid",
            {"exp": corrected, "cid": case_id},
        )
    if answer_text:
        db.execute(
            "UPDATE eag_eval_case SET expected_text=:exp"
            " WHERE case_id=:cid",
            {"exp": answer_text, "cid": case_id},
        )
    db.commit()
    return {"annotation_id": annotation_id,
            "expected_service_updated": bool(corrected),
            "expected_text_updated": bool(answer_text)}


def rename_dataset(db: Database, dataset_id: str, name: str) -> dict:
    db.execute(
        "UPDATE eag_eval_dataset SET name=:n WHERE dataset_id=:did",
        {"n": name, "did": dataset_id},
    )
    db.commit()
    return {"dataset_id": dataset_id, "name": name}


def delete_dataset(db: Database, dataset_id: str) -> dict:
    """删除评测集及其用例、标注（优化运行记录保留，供追溯）。"""
    db.execute(
        "DELETE FROM eag_annotation WHERE case_id IN"
        " (SELECT case_id FROM eag_eval_case WHERE dataset_id=:did)",
        {"did": dataset_id},
    )
    db.execute(
        "DELETE FROM eag_eval_case WHERE dataset_id=:did", {"did": dataset_id},
    )
    db.execute(
        "DELETE FROM eag_eval_dataset WHERE dataset_id=:did",
        {"did": dataset_id},
    )
    db.commit()
    return {"deleted": dataset_id}


def append_cases_from_runs(
    db: Database, *, dataset_id: str, run_ids: list[str],
) -> dict:
    """把指定运行记录追加进已有评测集（去重：同一 run 只进一次）。

    联动入口: 运行监控/调用链上发现一条路由错了，直接加进测试集，
    不必回到评测页重新按"最近 N 条"整批抓。
    """
    if not run_ids:
        return {"added": 0, "skipped": 0}
    existing = {
        r["source_run_id"] for r in db.query_all(
            "SELECT source_run_id FROM eag_eval_case WHERE dataset_id=:did",
            {"did": dataset_id},
        ) if r["source_run_id"]
    }
    placeholders = ", ".join(f":r{i}" for i in range(len(run_ids)))
    params = {f"r{i}": r for i, r in enumerate(run_ids)}
    runs = db.query_all(
        f"SELECT * FROM eag_run_record WHERE run_id IN ({placeholders})",
        params,
    )
    added = 0
    for run in runs:
        if run["run_id"] in existing:
            continue
        routed = json.loads(run["routed_services"] or "[]")
        db.execute(
            """INSERT INTO eag_eval_case
                 (case_id, dataset_id, input_text, expected_service,
                  source_run_id, created_at)
               VALUES (:cid, :did, :inp, :exp, :run, :ts)""",
            {"cid": new_id("case_"), "did": dataset_id,
             "inp": run["input_text"] or "",
             "exp": (routed[0] if routed else None),
             "run": run["run_id"], "ts": now()},
        )
        added += 1
    db.commit()
    return {"added": added, "skipped": len(runs) - added}
