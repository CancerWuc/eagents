# -*- coding: utf-8 -*-
"""性能测试 DAO（perf-test 01 §4 四张表访问，T03）。

- 主键统一 new_id()（32 位）；不使用外键，服务内保证一致性。
- 全局锁语义见 claim_test / has_active_test（02 §5.2）。
"""
from __future__ import annotations

import json

from ..persistence.db import Database, new_id, now

DATASET_TYPES = ("PRESSURE", "SIMULATION")
TEST_TYPES = ("PRESSURE", "SIMULATION")
TARGET_TYPES = ("MASTER", "SERVICE")
# 终态集合（全局锁只看 PENDING/RUNNING）
ACTIVE_STATUSES = ("PENDING", "RUNNING")
FINISHED_STATUSES = ("COMPLETED", "FAILED", "CANCELED")


def jload(raw) -> dict | list | None:
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ===========================================================================
# 数据集
# ===========================================================================
def create_dataset(
    db: Database, *, master_id: str, name: str, dataset_type: str,
    created_by: str,
) -> dict:
    ds_id = new_id("pds_")
    db.execute(
        """INSERT INTO eag_perf_dataset
             (perf_dataset_id, master_id, name, dataset_type, status,
              case_count, created_by, created_at, updated_at)
           VALUES (:id, :mid, :name, :dtype, 'ACTIVE', 0, :u, :ts, :ts)""",
        {"id": ds_id, "mid": master_id, "name": name,
         "dtype": dataset_type, "u": created_by, "ts": now()},
    )
    db.commit()
    return get_dataset(db, ds_id)


def get_dataset(db: Database, dataset_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_perf_dataset WHERE perf_dataset_id=:id",
        {"id": dataset_id},
    )


def get_dataset_by_name(
    db: Database, master_id: str, name: str,
) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_perf_dataset WHERE master_id=:mid AND name=:n",
        {"mid": master_id, "n": name},
    )


def list_datasets(db: Database, master_id: str | None = None) -> list[dict]:
    if master_id:
        return db.query_all(
            "SELECT * FROM eag_perf_dataset WHERE master_id=:mid"
            " AND status='ACTIVE' ORDER BY created_at DESC",
            {"mid": master_id},
        )
    return db.query_all(
        "SELECT * FROM eag_perf_dataset WHERE status='ACTIVE'"
        " ORDER BY created_at DESC",
    )


def delete_dataset(db: Database, dataset_id: str) -> None:
    """物理删除数据集与全部用例（调用方负责引用校验）。"""
    db.execute(
        "DELETE FROM eag_perf_case WHERE perf_dataset_id=:id",
        {"id": dataset_id},
    )
    db.execute(
        "DELETE FROM eag_perf_dataset WHERE perf_dataset_id=:id",
        {"id": dataset_id},
    )
    db.commit()


def tests_referencing_dataset(db: Database, dataset_id: str) -> int:
    return db.query_one(
        "SELECT COUNT(*) AS n FROM eag_perf_test WHERE perf_dataset_id=:id",
        {"id": dataset_id},
    )["n"]


# ===========================================================================
# 用例
# ===========================================================================
CASE_INSERT_SQL = """INSERT INTO eag_perf_case
    (case_id, perf_dataset_id, seq_no, q, a, send_time, time_sec, created_at)
VALUES (:cid, :did, :seq, :q, :a, :st, :tsec, :ts)"""


def insert_cases(db: Database, dataset_id: str, rows: list[dict]) -> int:
    """批量写入用例（调用方已保证 seq_no 从 1 连续），返回写入条数。"""
    ts = now()
    payload = [
        {"cid": new_id("pcs_"), "did": dataset_id, "seq": r["seq_no"],
         "q": r["q"], "a": r.get("a"), "st": r.get("send_time"),
         "tsec": r.get("time_sec"), "ts": ts}
        for r in rows
    ]
    if not payload:
        return 0
    # 分批 executors，避免单条 SQL 过长
    for i in range(0, len(payload), 500):
        db.execute(CASE_INSERT_SQL, payload[i:i + 500])
    db.execute(
        "UPDATE eag_perf_dataset SET case_count=case_count+:n,"
        " updated_at=:ts WHERE perf_dataset_id=:id",
        {"n": len(payload), "ts": ts, "id": dataset_id},
    )
    db.commit()
    return len(payload)


def list_cases(
    db: Database, dataset_id: str, *, offset: int = 0, limit: int = 50,
) -> list[dict]:
    return db.query_all(
        "SELECT case_id, seq_no, q, a, send_time, time_sec FROM eag_perf_case"
        " WHERE perf_dataset_id=:id ORDER BY seq_no LIMIT :lim OFFSET :off",
        {"id": dataset_id, "lim": limit, "off": offset},
    )


def all_cases(db: Database, dataset_id: str) -> list[dict]:
    """runner 取全量用例（数据集已限 10 万行）。"""
    return db.query_all(
        "SELECT seq_no, q, a, send_time, time_sec FROM eag_perf_case"
        " WHERE perf_dataset_id=:id ORDER BY seq_no",
        {"id": dataset_id},
    )


# ===========================================================================
# 测试任务
# ===========================================================================
def create_test(
    db: Database, *, master_id: str, dataset_id: str, target_type: str,
    target_id: str, test_type: str, config: dict, created_by: str,
) -> dict:
    test_id = new_id("pt_")
    db.execute(
        """INSERT INTO eag_perf_test
             (perf_test_id, master_id, perf_dataset_id, target_type, target_id,
              test_type, status, config_json, created_by, created_at)
           VALUES (:id, :mid, :did, :tt, :tid, :ty, 'PENDING', :cfg, :u, :ts)""",
        {"id": test_id, "mid": master_id, "did": dataset_id,
         "tt": target_type, "tid": target_id, "ty": test_type,
         "cfg": json.dumps(config), "u": created_by, "ts": now()},
    )
    db.commit()
    return get_test(db, test_id)


def get_test(db: Database, test_id: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM eag_perf_test WHERE perf_test_id=:id", {"id": test_id},
    )
    if row:
        row["config"] = jload(row.get("config_json")) or {}
        row["report"] = jload(row.get("report_json"))
    return row


def list_tests(
    db: Database, master_id: str | None = None, limit: int = 50,
) -> list[dict]:
    rows = db.query_all(
        """SELECT perf_test_id, master_id, perf_dataset_id, target_type,
                  target_id, test_type, status, created_by, created_at,
                  started_at, finished_at
             FROM eag_perf_test {where}
            ORDER BY created_at DESC LIMIT :lim""".format(
            where="WHERE master_id=:mid" if master_id else "",
        ),
        {"mid": master_id, "lim": min(limit, 200)} if master_id
        else {"lim": min(limit, 200)},
    )
    return rows


def has_active_test(db: Database) -> bool:
    """全局锁：是否已有 PENDING/RUNNING 测试（02 §5.2）。"""
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM eag_perf_test"
        " WHERE status IN ('PENDING','RUNNING')",
    )
    return bool(row and row["n"] > 0)


def claim_test(db: Database, test_id: str) -> bool:
    """原子领取：PENDING → RUNNING（影响行数为 1 才算抢到）。"""
    result = db.execute(
        "UPDATE eag_perf_test SET status='RUNNING', started_at=:ts"
        " WHERE perf_test_id=:id AND status='PENDING'",
        {"ts": now(), "id": test_id},
    )
    db.commit()
    return result.rowcount == 1


def oldest_pending_test(db: Database) -> dict | None:
    row = db.query_one(
        "SELECT perf_test_id FROM eag_perf_test WHERE status='PENDING'"
        " ORDER BY created_at ASC LIMIT 1",
    )
    return get_test(db, row["perf_test_id"]) if row else None


def set_finished(
    db: Database, test_id: str, *, status: str, report: dict | None,
) -> None:
    db.execute(
        "UPDATE eag_perf_test SET status=:st, report_json=:r,"
        " finished_at=:ts, cancel_flag=0 WHERE perf_test_id=:id",
        {"st": status, "r": json.dumps(report) if report else None,
         "ts": now(), "id": test_id},
    )
    db.commit()


def request_cancel(db: Database, test_id: str) -> None:
    db.execute(
        "UPDATE eag_perf_test SET cancel_flag=1 WHERE perf_test_id=:id",
        {"id": test_id},
    )
    db.commit()


def is_canceled(db: Database, test_id: str) -> bool:
    row = db.query_one(
        "SELECT cancel_flag, status FROM eag_perf_test"
        " WHERE perf_test_id=:id",
        {"id": test_id},
    )
    if not row:
        return True  # 记录被删视为取消
    return bool(row["cancel_flag"]) or row["status"] in FINISHED_STATUSES


def list_running_tests(db: Database) -> list[dict]:
    rows = db.query_all(
        "SELECT perf_test_id FROM eag_perf_test WHERE status='RUNNING'",
    )
    return [get_test(db, r["perf_test_id"]) for r in rows]


# ===========================================================================
# 样本
# ===========================================================================
SAMPLE_INSERT_SQL = """INSERT INTO eag_perf_sample
    (sample_id, perf_test_id, seq_no, q, expected_a, actual_output, status,
     latency_ms, error_code, error_detail, sent_at, finished_at,
     trace_id, run_id, context_id, answer_match)
VALUES (:sid, :tid, :seq, :q, :ea, :ao, :st, :lat, :ec, :ed, :sent, :fin,
        :trace, :run, :ctx, :am)"""


def insert_samples(db: Database, rows: list[dict]) -> None:
    payload = [
        {"sid": r["sample_id"], "tid": r["perf_test_id"], "seq": r["seq_no"],
         "q": r["q"], "ea": r.get("expected_a"), "ao": r.get("actual_output"),
         "st": r["status"], "lat": r.get("latency_ms"),
         "ec": r.get("error_code"),
         "ed": (r.get("error_detail") or None)
         and str(r["error_detail"])[:1024],
         "sent": r.get("sent_at"), "fin": r.get("finished_at"),
         "trace": r.get("trace_id"), "run": r.get("run_id"),
         "ctx": r.get("context_id"), "am": r.get("answer_match")}
        for r in rows
    ]
    if not payload:
        return
    for i in range(0, len(payload), 200):
        db.execute(SAMPLE_INSERT_SQL, payload[i:i + 200])
    db.commit()


def list_samples(
    db: Database, test_id: str, *, status: str | None = None,
    offset: int = 0, limit: int = 50,
) -> list[dict]:
    where = "WHERE perf_test_id=:tid"
    params: dict = {"tid": test_id, "lim": limit, "off": offset}
    if status:
        where += " AND status=:st"
        params["st"] = status
    return db.query_all(
        f"SELECT sample_id, seq_no, q, expected_a, actual_output, status,"
        f" latency_ms, error_code, error_detail, sent_at, finished_at,"
        f" trace_id, run_id, context_id, answer_match"
        f" FROM eag_perf_sample {where}"
        f" ORDER BY seq_no LIMIT :lim OFFSET :off",
        params,
    )


def sample_progress(db: Database, test_id: str) -> dict:
    """实时进度：已发/成功/失败/超时/拒绝 + 平均延迟（一次聚合查询）。"""
    row = db.query_one(
        """SELECT COUNT(*) AS sent,
                  SUM(status='SUCCEEDED') AS succeeded,
                  SUM(status='FAILED') AS failed,
                  SUM(status='TIMEOUT') AS timed_out,
                  SUM(status='REJECTED') AS rejected,
                  AVG(CASE WHEN status='SUCCEEDED' THEN latency_ms END)
                    AS avg_latency_ms
             FROM eag_perf_sample WHERE perf_test_id=:tid""",
        {"tid": test_id},
    ) or {}
    return {
        "sent": int(row.get("sent") or 0),
        "succeeded": int(row.get("succeeded") or 0),
        "failed": int(row.get("failed") or 0),
        "timedOut": int(row.get("timed_out") or 0),
        "rejected": int(row.get("rejected") or 0),
        "avgLatencyMs": round(float(row["avg_latency_ms"]), 1)
        if row.get("avg_latency_ms") is not None else None,
    }


def done_seq_nos(db: Database, test_id: str) -> set[int]:
    """断点恢复：已完成（已有样本）的 seq_no 集合。"""
    return {
        r["seq_no"] for r in db.query_all(
            "SELECT seq_no FROM eag_perf_sample WHERE perf_test_id=:tid",
            {"tid": test_id},
        )
    }


def all_samples_for_report(db: Database, test_id: str) -> list[dict]:
    return db.query_all(
        "SELECT status, latency_ms, error_code, answer_match,"
        " expected_a FROM eag_perf_sample WHERE perf_test_id=:tid",
        {"tid": test_id},
    )
