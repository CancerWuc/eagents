"""Knowledge-base HTTP response projections shared by list views."""
from . import dao


def _task_out(t: dict) -> dict:
    t = dict(t)
    t["created_at"] = str(t["created_at"])
    t["finished_at"] = str(t["finished_at"]) if t.get("finished_at") else None
    t["cancel_flag"] = bool(t["cancel_flag"])
    return t


# ===========================================================================
# 入库规划（04 §9/§10）
# ===========================================================================
def _plan_out(p: dict) -> dict:
    return {
        "plan_id": p["plan_id"], "kb_id": p["kb_id"],
        "status": p["status"], "plan_type": p["plan_type"],
        "base_rev": p.get("base_rev"),
        "source_ids": dao.jload(p.get("source_ids_json")) or [],
        "proposal": dao.jload(p.get("proposal_json")),
        "preview": dao.jload(p.get("preview_json")),
        "rationale_md": p.get("rationale_md"), "risk_md": p.get("risk_md"),
        "created_by": p.get("created_by"), "confirm_by": p.get("confirm_by"),
        "confirm_note": p.get("confirm_note"),
        "applied_rev": p.get("applied_rev"),
        "created_at": str(p["created_at"]),
        "confirmed_at": str(p["confirmed_at"]) if p.get("confirmed_at") else None,
        "applied_at": str(p["applied_at"]) if p.get("applied_at") else None,
    }
