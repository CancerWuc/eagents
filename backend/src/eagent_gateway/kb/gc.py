# -*- coding: utf-8 -*-
"""KB Rev 保留窗口与 GC（03 §4.3，评审 B5/T25）。

保留集 = 最近 N 个 READY Rev（N=settings.kb_gc_retain_revs，默认 5）
        + 标记保留的 Rev（summary_json 约定键 {"retain": true}，不新增列）
        + 被 eag_kb_ingest_plan.base_rev/applied_rev 引用的 Rev。
清理: 窗口外 Rev 头 + manifest + 无人引用的旧 page 行（非当前版本且不在
任何保留 manifest）+ 对应 chunk 行 + 无引用 embed_cache（表已废弃，D-2026-08-16-02
08 §3.4，仅清历史行）。审计 kb.gc。
"""
from __future__ import annotations

import logging

from ..config import settings
from ..persistence.db import Database
from . import dao

logger = logging.getLogger("eagent.kb.gc")


def gc_knowledge_base(db: Database, kb_id: str) -> dict:
    kb = dao.get_kb(db, kb_id)
    if not kb:
        return {"revs_deleted": 0}
    counts = {"revs": 0, "rev_entries": 0, "pages": 0, "chunks": 0,
              "embed_cache": 0}

    ready = dao.ready_revs(db, kb_id)
    retain_n = max(1, settings.kb_gc_retain_revs)
    keep_revs = {r["rev"] for r in ready[:retain_n]}
    for r in ready:
        summary = dao.jload(r.get("summary_json"), {}) or {}
        if summary.get("retain"):
            keep_revs.add(r["rev"])
    # plan 引用保护（03 §4.3-3）
    for p in dao.list_plans(db, kb_id):
        for key in ("base_rev", "applied_rev"):
            if p.get(key):
                keep_revs.add(p[key])
    if kb.get("current_rev"):
        keep_revs.add(kb["current_rev"])

    drop_revs = [r["rev"] for r in ready if r["rev"] not in keep_revs]
    if not drop_revs:
        return {k: 0 for k in ("revs", "rev_entries", "pages", "chunks",
                               "embed_cache")}

    # 保留 manifest 引用的 page 集合（含当前版本）
    kept_page_ids: set[str] = set()
    for rev in keep_revs:
        kept_page_ids.update(dao.manifest_of(db, kb_id, rev).keys())
    current_ids = {p["page_id"] for p in dao.current_pages(db, kb_id)}
    kept_page_ids |= current_ids

    for rev in drop_revs:
        manifest = dao.manifest_of(db, kb_id, rev)
        db.execute(
            "DELETE FROM eag_kb_rev_entry WHERE kb_id=:k AND rev=:r",
            {"k": kb_id, "r": rev},
        )
        counts["rev_entries"] += len(manifest)
        db.execute(
            "DELETE FROM eag_kb_rev WHERE kb_id=:k AND rev=:r",
            {"k": kb_id, "r": rev},
        )
        counts["revs"] += 1
        for page_id in manifest.keys():
            if page_id in kept_page_ids:
                continue
            page = dao.get_page(db, page_id)
            if not page:
                continue
            chunks = dao.chunks_of_page(db, page_id)
            if chunks:
                # D-2026-08-16-02：无向量库，仅清 chunk 行（历史行清理）
                db.execute(
                    "DELETE FROM eag_kb_chunk WHERE page_id=:p", {"p": page_id},
                )
                counts["chunks"] += len(chunks)
            db.execute(
                "DELETE FROM eag_kb_page WHERE page_id=:p", {"p": page_id},
            )
            counts["pages"] += 1

    # 无引用 embed_cache：内容 hash 不再被任何保留页面引用
    used_hashes = {
        p["content_hash"] for p in dao.current_pages(db, kb_id)
    }
    for rev in keep_revs:
        used_hashes.update(dao.manifest_of(db, kb_id, rev).values())
    rows = db.query_all(
        "SELECT cache_id, content_hash FROM eag_kb_embed_cache WHERE kb_id=:k",
        {"k": kb_id},
    )
    for r in rows:
        if r["content_hash"] not in used_hashes:
            db.execute(
                "DELETE FROM eag_kb_embed_cache WHERE cache_id=:c",
                {"c": r["cache_id"]},
            )
            counts["embed_cache"] += 1

    db.commit()
    dao.audit(
        db, user="system", action="kb.gc", resource_type="kb",
        resource_id=kb_id, detail=counts,
    )
    db.commit()
    logger.info("kb gc %s: %s", kb_id, counts)
    return counts


async def kb_gc_loop() -> None:
    """每日空闲时段执行（03 §8.4）；0 = 禁用。"""
    import asyncio

    if settings.kb_gc_interval_s <= 0:
        return
    while True:
        await asyncio.sleep(settings.kb_gc_interval_s)
        try:
            db = Database()
            try:
                for kb in dao.list_kbs(db):
                    gc_knowledge_base(db, kb["kb_id"])
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("kb gc loop failed")
