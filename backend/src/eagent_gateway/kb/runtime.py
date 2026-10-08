# -*- coding: utf-8 -*-
"""KB Runtime 只读路由（/internal/kb/*，02 §6，D6/L1）。

- 与管理写 API 路由表物理分离：本 router 只挂只读方法
  （search / read_page / graph / files / status / reviews），无任何写端点
  （路由表扫描测试覆盖，05 §4.3）。
- 鉴权：x-internal-token（独立密钥，与 x-admin-token 不同）+ x-caller
  （master:<id> / service:kb-planner，供审计与限流）。
- 失败语义 fail-closed：KB 无可用 Rev / 链路超时 → 503 KB_UNAVAILABLE。
- 只读实现只 import retrieval/dao（SELECT），不 import 管理写 service。
"""
from __future__ import annotations

import hmac
import secrets

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from .. import errors
from ..persistence.db import Database
from . import dao, retrieval
from .backends import MySQLBackend

router = APIRouter(prefix="/internal/kb")

# 未显式配置时进程内生成（L1 同进程：caller 与端点读同一值，等价持令牌；
# L2 独立部署必须显式配置 EAGENT_KB_INTERNAL_TOKEN）
_generated_token: str | None = None


def internal_token() -> str:
    from ..config import settings

    global _generated_token
    if settings.kb_internal_token:
        return settings.kb_internal_token
    if _generated_token is None:
        _generated_token = secrets.token_urlsafe(32)
    return _generated_token


async def require_internal(
    x_internal_token: str = Header(default=""),
    x_caller: str = Header(default=""),
) -> str:
    """内部只读面鉴权：独立令牌 + 调用方身份（返回 caller 供审计）。"""
    if not hmac.compare_digest(x_internal_token, internal_token()):
        raise errors.UnauthenticatedError("内部令牌无效或缺省（X-Internal-Token）")
    if not x_caller:
        raise errors.UnauthenticatedError("缺少调用方身份头（X-Caller）")
    return x_caller


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    rev: str | None = "current"


class KBUnavailableHTTP(errors.EAgentError):
    """503 KB_UNAVAILABLE（fail-closed）；错误处理器直接读 http_status，
    不改写全局 ERROR_STATUS（该表与 07 文档错误码表做契约对账）。"""

    code = "KB_UNAVAILABLE"
    http_status = 503
    safe_message = "知识库暂不可用"


@router.post("/{kb_id}/search")
async def search(
    kb_id: str, body: SearchIn,
    caller: str = Depends(require_internal),
):
    backend = MySQLBackend()
    try:
        return await retrieval.search_kb(
            backend, kb_id, body.query, rev=body.rev,
        )
    except retrieval.KBUnavailableError as exc:
        raise KBUnavailableHTTP(str(exc)) from exc
    finally:
        backend.close()


@router.get("/{kb_id}/read_page")
def read_page(
    kb_id: str, path: str, rev: str | None = "current",
    caller: str = Depends(require_internal),
):
    backend = MySQLBackend()
    try:
        return retrieval.read_page(backend, kb_id, path, rev=rev)
    except retrieval.KBUnavailableError as exc:
        raise KBUnavailableHTTP(str(exc)) from exc
    finally:
        backend.close()


@router.get("/{kb_id}/graph")
def graph(
    kb_id: str, path: str | None = None, hops: int = 1,
    caller: str = Depends(require_internal),
):
    """一跳邻接（hops>1 二期；一期固定 1，与检索 graph 信号同源）。"""
    db = Database()
    try:
        kb = dao.get_kb(db, kb_id)
        if not kb:
            raise errors.ValidationError("KB 不存在")
        rev = retrieval.resolve_rev(MySQLBackend(db), kb, "current")
        pages = dao.manifest_pages(db, kb_id, rev)
        paths = {p["rel_path"] for p in pages}
        if path:
            seeds = {retrieval.normalize_wiki_path(path)} & paths
        else:
            seeds = paths  # 全图（节点多时调用方应显式传 path）
        adj = dao.neighbors_of(db, kb_id, seeds)
        nodes = sorted(seeds | {n for nbrs in adj.values() for n in nbrs})
        edges = [
            {"from": s, "to": n}
            for s, nbrs in adj.items() for n in sorted(nbrs)
        ]
        return {"rev": rev, "nodes": nodes[:500], "edges": edges[:1000]}
    except retrieval.KBUnavailableError as exc:
        raise KBUnavailableHTTP(str(exc)) from exc
    finally:
        db.close()


@router.get("/{kb_id}/files")
def files(kb_id: str, caller: str = Depends(require_internal)):
    db = Database()
    try:
        kb = dao.get_kb(db, kb_id)
        if not kb:
            raise errors.ValidationError("KB 不存在")
        rev = retrieval.resolve_rev(MySQLBackend(db), kb, "current")
        pages = dao.manifest_pages(db, kb_id, rev)
        return {"rev": rev, "files": [
            {"path": p["rel_path"], "title": p["title"],
             "updated_at": str(p["updated_at"])}
            for p in pages
        ]}
    except retrieval.KBUnavailableError as exc:
        raise KBUnavailableHTTP(str(exc)) from exc
    finally:
        db.close()


@router.get("/{kb_id}/status")
def status(kb_id: str, caller: str = Depends(require_internal)):
    db = Database()
    try:
        kb = dao.get_kb(db, kb_id)
        if not kb:
            raise errors.ValidationError("KB 不存在")
        count = len(dao.current_pages(db, kb_id)) if kb.get("current_rev") else 0
        return {
            "kb_id": kb_id, "status": kb["status"],
            "current_rev": kb.get("current_rev"), "page_count": count,
            "updated_at": str(kb["updated_at"]),
        }
    finally:
        db.close()


@router.get("/{kb_id}/reviews")
def reviews(
    kb_id: str, status: str | None = "OPEN",
    caller: str = Depends(require_internal),
):
    """Review 只读过滤（llm-wiki MCP reviews 语义）。"""
    db = Database()
    try:
        rows = dao.list_reviews(db, kb_id, status=status)
        return {"reviews": [_review_out(r) for r in rows]}
    finally:
        db.close()


def _review_out(r: dict) -> dict:
    return {
        "review_id": r["review_id"], "kb_id": r["kb_id"], "rev": r.get("rev"),
        "page_id": r.get("page_id"), "type": r["type"],
        "payload": dao.jload(r.get("payload_json")), "status": r["status"],
        "created_at": str(r["created_at"]),
    }
