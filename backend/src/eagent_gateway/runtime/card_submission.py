"""Master card provenance and one-shot dispatch. No business payload is stored here."""
from __future__ import annotations

from datetime import timedelta

from ..errors import EAgentError
from ..hiagent import service as hiagent
from ..persistence.db import new_id, now


class CardSubmissionError(EAgentError):
    http_status = 409

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def issue_cards(db, results, *, master_id, context_id, subject_id):
    cards = []
    try:
        for item in results:
            result = item["result"]
            for card in result.cards:
                if not all((result.conversation_id, result.identity_digest, result.config_version)):
                    raise CardSubmissionError("CARD_SOURCE_INVALID", "卡片缺少可靠的来源信息")
                card_id, created = new_id(), now()
                db.execute(
                    "INSERT INTO eag_master_card_submission"
                    " (card_id, subject_id, master_id, context_id, service_id, conversation_id,"
                    " identity_digest, config_version, created_at, expires_at, state)"
                    " VALUES (:cid,:uid,:mid,:ctx,:sid,:conv,:identity,:version,:created,:expires,'OPEN')",
                    {"cid": card_id, "uid": subject_id, "mid": master_id, "ctx": context_id,
                     "sid": item["service_id"], "conv": result.conversation_id,
                     "identity": result.identity_digest, "version": result.config_version,
                     "created": created, "expires": created + timedelta(hours=48)},
                )
                cards.append({**card, "cardId": card_id, "source": {
                    "serviceId": item["service_id"], "serviceSlug": item["service_slug"],
                }})
        if cards:
            db.commit()
    except Exception:
        db.rollback()
        raise
    return cards


def _lookup(db, card_id, master_id, context_id, subject_id):
    row = db.query_one(
        "SELECT * FROM eag_master_card_submission WHERE card_id=:cid"
        " AND master_id=:mid AND context_id=:ctx AND subject_id=:uid",
        {"cid": card_id, "mid": master_id, "ctx": context_id, "uid": subject_id},
    )
    if not row:
        raise CardSubmissionError("CARD_NOT_ACCESSIBLE", "卡片不存在或不属于当前会话")
    if row["expires_at"] <= now():
        raise CardSubmissionError("CARD_EXPIRED", "卡片已过期，请重新查询或生成卡片")
    if row["state"] != "OPEN":
        raise CardSubmissionError("CARD_ALREADY_SUBMITTED", "卡片已提交，请核实原请求结果，勿重复执行")
    return row


def _binding(db, row):
    from .master_service import get_master, resolve_visible_bindings
    master = get_master(db, row["master_id"])
    if not master or not master["enabled"]:
        raise CardSubmissionError("CARD_SERVICE_UNAVAILABLE", "卡片所属总控已不可用")
    for binding in resolve_visible_bindings(db, row["master_id"], row["subject_id"]):
        if binding["service_id"] == row["service_id"] and binding.get("source_type") == "HIAGENT":
            return binding
    raise CardSubmissionError("CARD_SERVICE_UNAVAILABLE", "卡片来源服务已不可用或无权调用")


def claim(db, submission, principal, context_id, config_version, identity_digest):
    row = _lookup(db, submission["card_id"], submission["master_id"], context_id, principal.user_id)
    _binding(db, row)
    if row["config_version"] != config_version or row["identity_digest"] != identity_digest:
        raise CardSubmissionError("CARD_SOURCE_CHANGED", "服务配置或身份已变化，请重新生成卡片")
    conversation = hiagent.get_conversation(db, row["service_id"], context_id,
                                           principal.user_id, identity_digest)
    if not conversation or conversation != row["conversation_id"]:
        raise CardSubmissionError("CARD_SESSION_EXPIRED", "原业务会话已失效，请核实业务状态后重新开始")
    updated = db.execute(
        "UPDATE eag_master_card_submission SET state='PROCESSING', submission_run_id=:rid"
        " WHERE card_id=:cid AND state='OPEN' AND expires_at>:ts",
        {"rid": submission["run_id"], "cid": row["card_id"], "ts": now()},
    )
    won = updated.rowcount == 1
    db.commit()  # Durable before ANY outbound operation; never automatically reopened.
    if not won:
        raise CardSubmissionError("CARD_ALREADY_SUBMITTED", "卡片已提交或过期，请核实原请求结果")
    submission["claimed"] = True
    return conversation


async def run_confirmation(db, *, master_id, context_id, principal, text, data, ctx, stream_sink=None):
    """Separate path before model construction, with the same run/child audit chain."""
    from .direct_dispatch import dispatch

    submission = {"card_id": data["cardId"], "master_id": master_id, "claimed": False}

    def resolve_binding():
        row = _lookup(db, data["cardId"], master_id, context_id, principal.user_id)
        return _binding(db, row)

    return await dispatch(
        db, master_id=master_id, context_id=context_id, principal=principal, text=text,
        inputs=data["inputs"], ctx=ctx, resolve_binding=resolve_binding,
        stream_sink=stream_sink, submission=submission, audit_meta={"cardId": data["cardId"]},
    )
