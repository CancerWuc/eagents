# -*- coding: utf-8 -*-
"""KB 管理 REST（02 §5.1 端点总表；管理令牌 + scope 鉴权）。

- 写 API 全部落 eag_audit_event（kb.* 事件，T18）。
- 上传成功 body 带 plan_pending=true；前端轮询 ingest-plans?status=DRAFT。
- 检索预览复用 Runtime 只读检索（管理上下文受限调用，可显式 top_k/signals）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, UploadFile
from pydantic import BaseModel, Field

from .. import errors
from ..config import settings
from ..kb import dao, service, tasks
from ..permission.scopes import (
    require_agent_manage,
    require_kb_manage,
    require_kb_review,
)
from ..persistence.db import Database
from .pagination import Page, optional_page
from .list_pages import list_page

router = APIRouter(prefix="/api/v1")


def _user(cap_user_name: str | None) -> str:
    return cap_user_name or "anonymous"


# ===========================================================================
# KB CRUD
# ===========================================================================
class KbIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    classification: str | None = None
    owner_app: str | None = None
    schema_text: str | None = None
    purpose_text: str | None = None
    embedding_config: dict | None = None
    chunking_config: dict | None = None
    search_config: dict | None = None


class KbUpdateIn(BaseModel):
    display_name: str | None = None
    description: str | None = None
    classification: str | None = None
    schema_text: str | None = None
    purpose_text: str | None = None
    embedding_config: dict | None = None
    chunking_config: dict | None = None
    search_config: dict | None = None


@router.post("/knowledge-bases", status_code=201)
def create_kb(
    body: KbIn, user: str = Depends(require_kb_manage),
):
    db = Database()
    try:
        return service.create_kb(db, user=user, body=body.model_dump())
    finally:
        db.close()


@router.get("/knowledge-bases", dependencies=[Depends(require_kb_manage)])
def list_kbs(scope: str | None = None, paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        if isinstance(paging, Page):
            return list_page(db, "kbs", paging)
        return {"knowledge_bases": [service.kb_out(db, kb) for kb in dao.list_kbs(db)]}
    finally:
        db.close()


@router.get("/knowledge-bases/{kb_id}")
def get_kb(kb_id: str):
    db = Database()
    try:
        return service.kb_out(db, service.require_kb(db, kb_id))
    finally:
        db.close()


@router.put("/knowledge-bases/{kb_id}")
def update_kb(
    kb_id: str, body: KbUpdateIn, user: str = Depends(require_kb_manage),
):
    db = Database()
    try:
        return service.update_kb(
            db, user=user, kb_id=kb_id, body=body.model_dump(exclude_none=True),
        )
    finally:
        db.close()


@router.delete("/knowledge-bases/{kb_id}")
def delete_kb(
    kb_id: str, force: bool = False, user: str = Depends(require_kb_manage),
):
    """物理删除知识库（不可恢复）。force=true 越过绑定/在跑任务的前置检查。"""
    db = Database()
    try:
        return service.delete_kb(db, user=user, kb_id=kb_id, force=force)
    finally:
        db.close()


# ===========================================================================
# Sources
# ===========================================================================
class SourceUrlIn(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


@router.post("/knowledge-bases/{kb_id}/sources", status_code=201)
async def upload_source(
    kb_id: str,
    file: UploadFile | None = None,
    body: SourceUrlIn | None = None,
    user: str = Depends(require_kb_manage),
):
    db = Database()
    try:
        if file is not None:
            data = await file.read()
            if len(data) > settings.kb_max_upload_bytes:
                raise errors.PayloadTooLargeError("上传文件超过大小上限")
            if not data:
                raise errors.ValidationError("上传文件为空")
            return await service.upload_source(
                db, user=user, kb_id=kb_id,
                file_name=file.filename or "upload.bin",
                data=data, mime_type=file.content_type,
            )
        if body is not None:
            return await service.upload_source_from_url(
                db, user=user, kb_id=kb_id, url=body.url,
            )
        raise errors.ValidationError("需提供 multipart file 或 JSON body url")
    finally:
        db.close()


@router.get("/knowledge-bases/{kb_id}/sources")
def list_sources(kb_id: str, paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        service.require_kb(db, kb_id)
        if isinstance(paging, Page):
            return list_page(db, "kb-sources", paging, kb_id=kb_id)
        rows = dao.list_sources(db, kb_id)
        for r in rows:
            r["created_at"] = str(r["created_at"])
            r["updated_at"] = str(r["updated_at"])
        return {"sources": rows}
    finally:
        db.close()


@router.delete("/knowledge-bases/{kb_id}/sources/{source_id}")
def delete_source(
    kb_id: str, source_id: str, user: str = Depends(require_kb_manage),
):
    db = Database()
    try:
        return service.delete_source(
            db, user=user, kb_id=kb_id, source_id=source_id,
        )
    finally:
        db.close()


@router.post("/knowledge-bases/{kb_id}/sources/{source_id}:plan")
def replan_source(
    kb_id: str, source_id: str, user: str = Depends(require_kb_manage),
):
    db = Database()
    try:
        return service.replan_source(
            db, user=user, kb_id=kb_id, source_id=source_id,
        )
    finally:
        db.close()


# ===========================================================================
# 编译触发（202）与任务轮询
# ===========================================================================
@router.post("/knowledge-bases/{kb_id}:ingest", status_code=202)
def trigger_ingest(kb_id: str, user: str = Depends(require_kb_manage)):
    db = Database()
    try:
        return service.trigger_compile(db, user=user, kb_id=kb_id, kind="INGEST")
    finally:
        db.close()


@router.post("/knowledge-bases/{kb_id}:rebuild", status_code=202)
def trigger_rebuild(kb_id: str, user: str = Depends(require_kb_manage)):
    db = Database()
    try:
        return service.trigger_compile(db, user=user, kb_id=kb_id, kind="REBUILD")
    finally:
        db.close()


@router.post("/knowledge-bases/{kb_id}:embed", status_code=202)
def trigger_embed(kb_id: str, user: str = Depends(require_kb_manage)):
    db = Database()
    try:
        return service.trigger_compile(db, user=user, kb_id=kb_id, kind="EMBED")
    finally:
        db.close()


def _task_out(t: dict) -> dict:
    t = dict(t)
    t["created_at"] = str(t["created_at"])
    t["finished_at"] = str(t["finished_at"]) if t.get("finished_at") else None
    t["cancel_flag"] = bool(t["cancel_flag"])
    return t


@router.get("/knowledge-bases/{kb_id}/tasks")
def list_tasks(kb_id: str, paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        service.require_kb(db, kb_id)
        if isinstance(paging, Page):
            return list_page(db, "kb-tasks", paging, kb_id=kb_id)
        return {"tasks": [_task_out(t) for t in dao.list_tasks(db, kb_id)]}
    finally:
        db.close()


@router.get("/knowledge-bases/{kb_id}/tasks/{task_id}", dependencies=[Depends(require_kb_manage)])
def get_task(kb_id: str, task_id: str):
    db = Database()
    try:
        task = dao.get_task(db, task_id)
        if not task or task["kb_id"] != kb_id:
            raise errors.NotFoundError("任务不存在")
        return _task_out(task)
    finally:
        db.close()


@router.post("/knowledge-bases/{kb_id}/tasks/{task_id}:cancel")
def cancel_task(
    kb_id: str, task_id: str, user: str = Depends(require_kb_manage),
):
    """管理面动作直接置 cancel_flag（03 §2.2；契约扩展登记于 05 §3.1）。"""
    db = Database()
    try:
        task = dao.get_task(db, task_id)
        if not task or task["kb_id"] != kb_id:
            raise errors.NotFoundError("任务不存在")
        dao.request_cancel(db, task_id)
        return {"task_id": task_id, "cancel_flag": True}
    finally:
        db.close()


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


@router.get("/knowledge-bases/{kb_id}/ingest-plans")
def list_plans(kb_id: str, status: str | None = None, paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        service.require_kb(db, kb_id)
        if isinstance(paging, Page):
            return list_page(db, "kb-plans", paging, kb_id=kb_id, status=status)
        return {"plans": [_plan_out(p) for p in dao.list_plans(db, kb_id, status)]}
    finally:
        db.close()


@router.get("/knowledge-bases/{kb_id}/ingest-plans/{plan_id}", dependencies=[Depends(require_kb_manage)])
def get_plan(kb_id: str, plan_id: str):
    db = Database()
    try:
        plan = dao.get_plan(db, plan_id)
        if not plan or plan["kb_id"] != kb_id:
            raise errors.NotFoundError("规划不存在")
        return _plan_out(plan)
    finally:
        db.close()


class ConfirmIn(BaseModel):
    selected_action_ids: list[str] | None = None
    note: str | None = None


@router.post("/knowledge-bases/{kb_id}/ingest-plans/{plan_id}:confirm", status_code=202)
def confirm_plan(
    kb_id: str, plan_id: str, body: ConfirmIn,
    user: str = Depends(require_kb_review),
):
    db = Database()
    try:
        return service.confirm_plan(
            db, user=user, kb_id=kb_id, plan_id=plan_id,
            selected_action_ids=body.selected_action_ids,
            note=body.note,
        )
    finally:
        db.close()


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)  # S3：必填


@router.post("/knowledge-bases/{kb_id}/ingest-plans/{plan_id}:reject")
def reject_plan(
    kb_id: str, plan_id: str, body: RejectIn,
    user: str = Depends(require_kb_review),
):
    db = Database()
    try:
        return service.reject_plan(
            db, user=user, kb_id=kb_id, plan_id=plan_id,
            reason=body.reason,
        )
    finally:
        db.close()


# ===========================================================================
# Review
# ===========================================================================
@router.get("/knowledge-bases/{kb_id}/reviews")
def list_reviews(kb_id: str, status: str | None = None, paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        service.require_kb(db, kb_id)
        if isinstance(paging, Page):
            return list_page(db, "kb-reviews", paging, kb_id=kb_id, status=status)
        rows = dao.list_reviews(db, kb_id, status)
        for r in rows:
            r["created_at"] = str(r["created_at"])
            r["payload"] = dao.jload(r.get("payload_json"))
            r.pop("payload_json", None)
        return {"reviews": rows}
    finally:
        db.close()


class ResolveIn(BaseModel):
    status: str = Field(pattern="^(RESOLVED|DISMISSED)$")
    note: str | None = None


@router.post("/knowledge-bases/{kb_id}/reviews/{review_id}:resolve")
def resolve_review(
    kb_id: str, review_id: str, body: ResolveIn,
    user: str = Depends(require_kb_review),
):
    db = Database()
    try:
        return service.resolve_review(
            db, user=user, kb_id=kb_id, review_id=review_id,
            status=body.status, note=body.note,
        )
    finally:
        db.close()


# ===========================================================================
# Wiki Pages / Revs（详情子 Tab 数据）
# ===========================================================================
@router.get("/knowledge-bases/{kb_id}/pages")
def list_pages(kb_id: str, paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        service.require_kb(db, kb_id)
        if isinstance(paging, Page):
            return list_page(db, "kb-pages", paging, kb_id=kb_id)
        pages = []
        for p in dao.current_pages(db, kb_id):
            pages.append({
                "page_id": p["page_id"], "rel_path": p["rel_path"],
                "title": p["title"], "content_hash": p["content_hash"][:12],
                "chunk_count": p["chunk_count"],
                "embedded": bool(p["embedded"]),
                "updated_at": str(p["updated_at"]),
            })
        return {"pages": pages}
    finally:
        db.close()


@router.get("/knowledge-bases/{kb_id}/revs")
def list_revs(kb_id: str, paging: Page | None = Depends(optional_page)):
    db = Database()
    try:
        service.require_kb(db, kb_id)
        if isinstance(paging, Page):
            return list_page(db, "kb-revs", paging, kb_id=kb_id)
        rows = []
        for r in dao.list_revs(db, kb_id):
            rows.append({
                "rev": r["rev"], "status": r["status"],
                "summary": dao.jload(r.get("summary_json")),
                "report": dao.jload(r.get("report_json")),
                "created_by": r["created_by"],
                "created_at": str(r["created_at"]),
                "finished_at": str(r["finished_at"]) if r.get("finished_at") else None,
            })
        return {"revs": rows}
    finally:
        db.close()


# ===========================================================================
# 绑定（agent:manage）
# ===========================================================================
class BindingsIn(BaseModel):
    bindings: list[dict]


@router.post("/agents/{master_id}/kb-bindings")
def set_kb_bindings(
    master_id: str, body: BindingsIn,
    user: str = Depends(require_agent_manage),
):
    db = Database()
    try:
        # 不查存在性就会给已删除的总控写出孤儿绑定（与 routes._require_master
        # 同一口径；这里就地查，避免 kb 模块反向依赖管理台路由模块）
        from ..runtime import master_service

        if not master_service.get_master(db, master_id):
            raise errors.NotFoundError("Agent Team 不存在")
        return service.set_bindings(
            db, user=user, master_id=master_id, items=body.bindings,
        )
    finally:
        db.close()


@router.get("/agents/{master_id}/kb-bindings")
def get_kb_bindings(master_id: str):
    db = Database()
    try:
        rows = dao.kb_bindings_for_target(
            db, "MASTER", master_id, enabled_only=False,
        )
        return {"bindings": [
            {"kb_id": b["kb_id"], "slug": b["slug"],
             "display_name": b["display_name"], "enabled": bool(b["enabled"]),
             "tool_prefix": b.get("tool_prefix"), "sort_order": b["sort_order"]}
            for b in rows
        ]}
    finally:
        db.close()


# ===========================================================================
# 检索预览（D2：仅管理面可显式传 top_k/signals）
# ===========================================================================
@router.get("/knowledge-bases/{kb_id}/preview/search", dependencies=[Depends(require_kb_manage)])
async def preview_search(
    kb_id: str, q: str, top_k: int | None = None,
    signals: str | None = None,
):
    db = Database()
    try:
        sig = [s.strip() for s in signals.split(",") if s.strip()] if signals else None
        return await service.preview_search(
            db, kb_id=kb_id, q=q, top_k=top_k, signals=sig,
        )
    finally:
        db.close()
