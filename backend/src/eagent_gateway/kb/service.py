# -*- coding: utf-8 -*-
"""KB 管理面服务（02 §5 管理 REST 的业务层）。

- 全部写操作落审计（kb.* 事件，02 §7.3）。
- 上传：multipart 文件或 URL 抓取（SSRF 校验）；SHA256 重复内容检测（D7）。
- confirm：依赖闭包校验（D4）+ base_rev 乐观校验（04 §10.3）→ 202 建任务。
- reject：reason 必填 → confirm_note（S3）。
"""
from __future__ import annotations

import logging

from .. import errors
from ..contracts import KB_SCHEMA, ContractViolation, validate_or_raise
from ..persistence.db import Database, new_id
from ..security.pinned_http import AddressBlockedError, guarded_async_client
from . import dao, planner, storage, tasks
from .constants import (
    DEFAULT_SEARCH_CONFIG,
    normalize_chunking_config,
    normalize_embedding_config,
    normalize_search_config,
    sha256_hex,
)

logger = logging.getLogger("eagent.kb.service")


# ---------------------------------------------------------------------------
# KB CRUD
# ---------------------------------------------------------------------------
def _validate_kb_payload(body: dict, *, partial: bool = False) -> dict:
    # None 值一律剔除（可选字段缺省即可，契约内 nullable 字段由服务端填充）
    payload = {k: v for k, v in body.items() if v is not None}
    if not partial:
        payload.setdefault("slug", body.get("slug"))
        payload.setdefault("display_name", body.get("display_name"))
    try:
        validate_or_raise(payload, KB_SCHEMA)
    except ContractViolation as exc:
        # 契约校验失败按仓库既有错误约定映射（platform-error.v1, 400）
        raise errors.ValidationError(str(exc)) from exc
    return payload


def create_kb(db: Database, *, user: str, body: dict) -> dict:
    payload = _validate_kb_payload(body)
    if dao.get_kb_by_slug(db, payload["slug"]):
        raise errors.ConflictError(f"slug 已存在: {payload['slug']}")
    kb_id = new_id("kb_")
    db.execute(
        """INSERT INTO eag_knowledge_base
             (kb_id, slug, display_name, description, classification,
              owner_app, status, schema_text, purpose_text, embedding_config,
              chunking_config, search_config, created_at, updated_at)
           VALUES (:kid, :slug, :dn, :dsc, :cls, :owner, 'CREATED', :schema,
                   :purpose, :emb, :chunk, :search, :ts, :ts)""",
        {
            "kid": kb_id, "slug": payload["slug"],
            "dn": payload["display_name"],
            "dsc": payload.get("description"),
            "cls": payload.get("classification", "INTERNAL"),
            "owner": payload.get("owner_app"),
            "schema": payload.get("schema_text"),
            "purpose": payload.get("purpose_text"),
            "emb": dao.jdump(normalize_embedding_config(
                payload.get("embedding_config"))),
            "chunk": dao.jdump(normalize_chunking_config(
                payload.get("chunking_config"))),
            "search": dao.jdump(normalize_search_config(
                payload.get("search_config"))),
            "ts": dao.now(),
        },
    )
    db.commit()
    dao.audit(db, user=user, action="kb.create", resource_type="kb",
              resource_id=kb_id, detail={"slug": payload["slug"]})
    db.commit()
    return kb_out(db, dao.get_kb(db, kb_id))


def update_kb(db: Database, *, user: str, kb_id: str, body: dict) -> dict:
    kb = dao.get_kb(db, kb_id)
    if not kb:
        raise errors.NotFoundError("KB 不存在")
    diff: dict = {}
    field_map = {
        "display_name": "display_name",
        "description": "description",
        "classification": "classification",
        "schema_text": "schema_text",
        "purpose_text": "purpose_text",
    }
    sets = []
    params: dict = {"kid": kb_id, "ts": dao.now()}
    for key, col in field_map.items():
        if key in body and body[key] is not None and body[key] != kb.get(col):
            sets.append(f"{col}=:{key}")
            params[key] = body[key]
            diff[key] = {"from": str(kb.get(col))[:200],
                         "to": str(body[key])[:200]}
    for key, normalizer in (
        ("embedding_config", normalize_embedding_config),
        ("chunking_config", normalize_chunking_config),
        ("search_config", normalize_search_config),
    ):
        if key in body and body[key]:
            value = dao.jdump(normalizer(body[key]))
            if value != (kb.get(key) or ""):
                sets.append(f"{key}=:{key}")
                params[key] = value
                diff[key] = "updated"
    # 011 schema/purpose 归属切换: 用户提交非空 → 人工权威（AI 不再改写）；
    # 显式提交空串 → 交回 AI 维护
    for key, flag in (
        ("schema_text", "schema_ai_managed"),
        ("purpose_text", "purpose_ai_managed"),
    ):
        if key in body and body[key] is not None:
            sets.append(f"{flag}=:{flag}")
            params[flag] = 0 if str(body[key]).strip() else 1
    if sets:
        db.execute(
            f"UPDATE eag_knowledge_base SET {', '.join(sets)}, updated_at=:ts"
            " WHERE kb_id=:kid",
            params,
        )
        db.commit()
        dao.audit(db, user=user, action="kb.update", resource_type="kb",
                  resource_id=kb_id, detail={"diff": diff})
        db.commit()
    return kb_out(db, dao.get_kb(db, kb_id))


def kb_out(db: Database, kb: dict) -> dict:
    out = dict(kb)
    for col in ("embedding_config", "chunking_config", "search_config"):
        out[col] = dao.jload(kb.get(col))
    counts = dao.kb_counts(db, kb["kb_id"])
    out.update(counts)
    out["active_work"] = bool(db.query_one(
        "SELECT EXISTS(SELECT 1 FROM eag_kb_task WHERE kb_id=:k AND status IN ('PENDING','RUNNING')) "
        "OR EXISTS(SELECT 1 FROM eag_kb_ingest_plan WHERE kb_id=:k AND status='DRAFT') "
        "OR EXISTS(SELECT 1 FROM eag_kb_source WHERE kb_id=:k AND status IN ('PENDING','PLANNING')) AS active",
        {"k": kb["kb_id"]},
    )["active"])
    out["created_at"] = str(kb["created_at"])
    out["updated_at"] = str(kb["updated_at"])
    return out


def require_kb(db: Database, kb_id: str) -> dict:
    kb = dao.get_kb(db, kb_id)
    if not kb:
        raise errors.NotFoundError("KB 不存在")
    return kb


def delete_kb(db: Database, *, user: str, kb_id: str,
              force: bool = False) -> dict:
    """物理删除 KB：清空 11 张子表 + 主表 + 本地存储目录（不可恢复）。

    两道前置闸门（force=True 可越过，用于清理测试残留）：
    - 仍被总控/Agent 绑定 → 409，避免删掉线上正在用的检索工具；
    - 仍有 PENDING/RUNNING 编译任务 → 409，避免任务执行到一半失去主表行。
    """
    kb = require_kb(db, kb_id)
    bindings = dao.binding_targets(db, kb_id)
    active = dao.active_task_count(db, kb_id)
    if not force:
        if bindings:
            targets = ", ".join(
                f"{b['target_type']}:{b['target_id']}" for b in bindings[:5]
            )
            raise errors.ConflictError(
                f"知识库仍被 {len(bindings)} 个目标绑定（{targets}），"
                "请先解绑或使用 force=true",
            )
        if active:
            raise errors.ConflictError(
                f"知识库仍有 {active} 个进行中的编译任务，"
                "请等待完成/取消或使用 force=true",
            )
    deleted = dao.purge_kb(db, kb_id)
    db.commit()
    # 文件清理放在数据库提交之后：目录删除失败只留孤儿文件（GC 可回收），
    # 而先删文件后回滚会留下指向空目录的可检索 Rev。
    storage_error = None
    try:
        storage.delete_kb_dir(kb_id)
    except OSError as exc:  # noqa: BLE001
        storage_error = f"{type(exc).__name__}: {exc}"
        logger.warning("KB %s 存储目录清理失败，遗留孤儿文件: %s", kb_id, exc)
    dao.audit(db, user=user, action="kb.delete", resource_type="kb",
              resource_id=kb_id,
              detail={"slug": kb["slug"], "display_name": kb["display_name"],
                      "force": force, "unbound": len(bindings),
                      "canceled_tasks": active, "deleted_rows": deleted,
                      "storage_error": storage_error})
    db.commit()
    return {"kb_id": kb_id, "slug": kb["slug"], "status": "DELETED",
            "deleted_rows": deleted, "unbound": len(bindings),
            "storage_error": storage_error}


# ---------------------------------------------------------------------------
# Source 上传/删除（D7：重复内容检测 + 自动触发规划）
# ---------------------------------------------------------------------------
async def upload_source(
    db: Database, *, user: str, kb_id: str, file_name: str,
    data: bytes, mime_type: str | None, trigger_planning: bool = True,
) -> dict:
    kb = require_kb(db, kb_id)
    sha = sha256_hex(data)
    # D7：同 KB 重复内容 → 提示已有源，跳过规划
    dup = dao.find_source_by_sha(db, kb_id, sha)
    if dup:
        return {
            "source_id": dup["source_id"], "kb_id": kb_id,
            "status": dup["status"], "sha256": sha,
            "duplicate_of": dup["source_id"], "plan_pending": False,
            "message": f"内容已存在于 {dup['source_id']}，跳过入库规划",
            "created_at": str(dup["created_at"]),
        }
    source_id = new_id("src_")
    rel_path = storage.put_raw(kb_id, source_id, file_name, data)
    db.execute(
        """INSERT INTO eag_kb_source
             (source_id, kb_id, rel_path, file_name, mime_type, sha256,
              status, created_at, updated_at)
           VALUES (:sid, :kid, :rp, :fn, :mt, :sha, 'PENDING', :ts, :ts)""",
        {"sid": source_id, "kid": kb_id, "rp": rel_path, "fn": file_name[:250],
         "mt": mime_type, "sha": sha, "ts": dao.now()},
    )
    db.commit()
    dao.audit(db, user=user, action="kb.source.upload",
              resource_type="kb_source", resource_id=source_id,
              detail={"sha256": sha, "size": len(data),
                      "trigger_plan": trigger_planning})
    db.commit()
    if trigger_planning:
        planner.notify_upload(kb_id, source_id)
    return {
        "source_id": source_id, "kb_id": kb_id, "status": "PENDING",
        "sha256": sha, "plan_pending": trigger_planning,
        "created_at": str(dao.now()),
    }


async def upload_source_from_url(
    db: Database, *, user: str, kb_id: str, url: str,
) -> dict:
    """URL 抓取上传（SSRF 校验；拒绝内网/保留地址 → 400）。"""
    try:
        async with guarded_async_client(timeout=30.0) as client:
            resp = await client.get(url)
        resp.raise_for_status()
    except AddressBlockedError as exc:
        raise errors.ValidationError(f"URL 抓取被 SSRF 策略拒绝: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise errors.DependencyError(f"URL 抓取失败: {type(exc).__name__}") from exc
    name = url.rsplit("/", 1)[-1].split("?")[0] or "fetched.txt"
    mime = resp.headers.get("content-type", "").split(";")[0] or None
    return await upload_source(
        db, user=user, kb_id=kb_id, file_name=name, data=resp.content,
        mime_type=mime,
    )


def delete_source(db: Database, *, user: str, kb_id: str, source_id: str) -> dict:
    require_kb(db, kb_id)
    src = dao.get_source(db, source_id)
    if not src or src["kb_id"] != kb_id:
        raise errors.NotFoundError("源不存在")
    dao.set_source_status(db, source_id, "DELETED")
    db.commit()
    pages_hint = 0
    dao.audit(db, user=user, action="kb.source.delete",
              resource_type="kb_source", resource_id=source_id,
              detail={"affected_pages_est": pages_hint,
                      "note": "软删；重构规划二期（RESTRUCTURE）"})
    db.commit()
    return {"source_id": source_id, "status": "DELETED"}


def replan_source(db: Database, *, user: str, kb_id: str,
                  source_id: str) -> dict:
    """手动（重新）规划：跳过防抖窗口立即执行（04 §2.2）。"""
    require_kb(db, kb_id)
    src = dao.get_source(db, source_id)
    if not src or src["kb_id"] != kb_id:
        raise errors.NotFoundError("源不存在")
    dao.audit(db, user=user, action="kb.plan.create",
              resource_type="kb_ingest_plan", resource_id=source_id,
              detail={"trigger": "manual"})
    db.commit()
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(planner.run_plan_for_sources(kb_id, [source_id]))
    except RuntimeError:
        pass
    return {"source_id": source_id, "plan_pending": True}


# ---------------------------------------------------------------------------
# 编译触发（202 + 任务入队，B4）
# ---------------------------------------------------------------------------
def trigger_compile(
    db: Database, *, user: str, kb_id: str, kind: str,
) -> dict:
    require_kb(db, kb_id)
    task = tasks.enqueue_task(db, kb_id=kb_id, kind=kind, trigger_by=user)
    dao.audit(
        db, user=user, action=f"kb.{'ingest' if kind == 'INGEST' else kind.lower()}.start",
        resource_type="kb_task", resource_id=task["task_id"],
        detail={"kind": kind, "trigger_by": user},
    )
    db.commit()
    tasks.kickoff()
    return {
        "task_id": task["task_id"], "kind": kind, "status": task["status"],
        "kb_id": kb_id,
        "message": "编译任务已入队；轮询"
                   f" GET /api/v1/knowledge-bases/{kb_id}/tasks/{task['task_id']}",
    }


# ---------------------------------------------------------------------------
# 规划确认/驳回（D4/D8/S3，04 §9/§10）
# ---------------------------------------------------------------------------
def confirm_plan(
    db: Database, *, user: str, kb_id: str, plan_id: str,
    selected_action_ids: list[str] | None, note: str | None,
) -> dict:
    kb = require_kb(db, kb_id)
    plan = dao.get_plan(db, plan_id)
    if not plan or plan["kb_id"] != kb_id:
        raise errors.NotFoundError("规划不存在")
    if plan["status"] != "DRAFT":
        raise errors.ConflictError(f"规划状态 {plan['status']} 不可确认")
    # 乐观校验（04 §10.3）：base_rev 与 current_rev 不符 → 409
    if (plan.get("base_rev") or None) != (kb.get("current_rev") or None):
        raise errors.ConflictError(
            "base_rev 与当前版本不一致（知识库已变化），请重新规划",
        )
    proposal = dao.jload(plan["proposal_json"], {}) or {}
    try:
        chosen, auto = planner.validate_dependency_closure(
            proposal, selected_action_ids or [
                c["action_id"] for c in proposal.get("changes") or []
            ],
        )
    except KeyError as exc:
        raise errors.ValidationError(str(exc)) from exc

    selection_note = note or ""
    selection_note += (
        f"\n[selected:{','.join(chosen)}]"
        f"\n[auto_selected:{','.join(auto)}]"
    )
    dao.update_plan_status(
        db, plan_id, "CONFIRMED", confirm_by=user,
        confirm_note=selection_note,
    )
    db.commit()
    task = tasks.enqueue_task(
        db, kb_id=kb_id, kind="INGEST", trigger_by=user,
    )
    dao.audit(
        db, user=user, action="kb.plan.confirm",
        resource_type="kb_ingest_plan", resource_id=plan_id,
        detail={"selected": chosen, "auto_selected": auto, "note": note,
                "task_id": task["task_id"]},
    )
    db.commit()
    tasks.kickoff()
    return {
        "task_id": task["task_id"], "status": task["status"],
        "plan_id": plan_id, "auto_selected": auto,
        "selected_action_ids": chosen,
        "message": "编译任务已入队；轮询"
                   f" GET /api/v1/knowledge-bases/{kb_id}/tasks/{task['task_id']}",
    }


def reject_plan(
    db: Database, *, user: str, kb_id: str, plan_id: str, reason: str,
) -> dict:
    require_kb(db, kb_id)
    plan = dao.get_plan(db, plan_id)
    if not plan or plan["kb_id"] != kb_id:
        raise errors.NotFoundError("规划不存在")
    if plan["status"] != "DRAFT":
        raise errors.ConflictError(f"规划状态 {plan['status']} 不可驳回")
    # S3：驳回原因必填 → confirm_note（注入后续规划 prompt）
    dao.update_plan_status(
        db, plan_id, "REJECTED", confirm_by=user, confirm_note=reason,
    )
    # 相关源回退可重新规划（04 §10.2）
    for sid in dao.jload(plan["source_ids_json"], []) or []:
        src = dao.get_source(db, sid)
        if src and src["status"] == "PLANNING":
            dao.set_source_status(db, sid, "PENDING")
    db.commit()
    dao.audit(db, user=user, action="kb.plan.reject",
              resource_type="kb_ingest_plan", resource_id=plan_id,
              detail={"reason": reason})
    db.commit()
    return {"plan_id": plan_id, "status": "REJECTED"}


def resolve_review(
    db: Database, *, user: str, kb_id: str, review_id: str,
    status: str, note: str | None,
) -> dict:
    require_kb(db, kb_id)
    review = dao.get_review(db, review_id)
    if not review or review["kb_id"] != kb_id:
        raise errors.NotFoundError("Review 不存在")
    if status not in ("RESOLVED", "DISMISSED"):
        raise errors.ValidationError("status 仅支持 RESOLVED/DISMISSED")
    dao.resolve_review(db, review_id, status=status, resolved_by=user)
    db.commit()
    dao.audit(db, user=user, action="kb.review.resolve",
              resource_type="kb_review", resource_id=review_id,
              detail={"status": status, "note": note})
    db.commit()
    return {"review_id": review_id, "status": status}


# ---------------------------------------------------------------------------
# 绑定（02 §5.1：覆盖式）
# ---------------------------------------------------------------------------
def set_bindings(
    db: Database, *, user: str, master_id: str, items: list[dict],
) -> dict:
    for it in items:
        if not dao.get_kb(db, it.get("kb_id", "")):
            raise errors.ValidationError(f"KB 不存在: {it.get('kb_id')}")
    before = dao.kb_bindings_for_target(
        db, "MASTER", master_id, enabled_only=False,
    )
    dao.replace_bindings(db, target_type="MASTER", target_id=master_id,
                         items=items)
    db.commit()
    after = dao.kb_bindings_for_target(
        db, "MASTER", master_id, enabled_only=False,
    )
    dao.audit(
        db, user=user, action="kb.binding.set", resource_type="kb_binding",
        resource_id=master_id,
        detail={"before": [b["kb_id"] for b in before],
                "after": [b["kb_id"] for b in after]},
    )
    db.commit()
    return {"bindings": [
        {"kb_id": b["kb_id"], "slug": b["slug"], "enabled": bool(b["enabled"]),
         "tool_prefix": b.get("tool_prefix"), "sort_order": b["sort_order"]}
        for b in after
    ]}


# ---------------------------------------------------------------------------
# 检索预览（02 §5.1：管理面可显式传 top_k/signals，D2）
# ---------------------------------------------------------------------------
async def preview_search(
    db: Database, *, kb_id: str, q: str, top_k: int | None,
    signals: list[str] | None,
) -> dict:
    from . import retrieval as kb_retrieval
    from .backends import MySQLBackend

    require_kb(db, kb_id)
    return await kb_retrieval.search_kb(
        MySQLBackend(db), kb_id, q, rev="current",
        top_k_override=top_k, signals_override=signals,
    )
