# -*- coding: utf-8 -*-
"""KB 三方对账与恢复（08 §4.3，D-2026-08-16-02 / R-06）。

对账 = DB 预期清单 ↔ 本地文件 ↔ OSS 对象，恢复矩阵：

| 本地 | DB | OSS | 动作 |
|---|---|---|---|
| ✅ | ✅ | ✅ | 正常 |
| ❌ | ✅（hash 匹配） | ✅ | 从 OSS 拉回 |
| ❌ | ✅ | ❌ | wiki: 从 DB body_md 重建；raw: 标记 FAILED 人工重传 |
| ✅ | ✅ | ❌ | 推送 OSS（补同步） |
| ✅ | ❌ | —  | 孤儿文件：标记（仅审计，不自动删） |

结果落审计 kb.file.reconcile；本地缺失修复后不自动 REBUILD（先不自动，08 §4.3）。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..config import settings
from ..persistence.db import Database
from . import dao, storage
from .constants import sha256_hex
from .oss_sync import OssClient, build_client, oss_configured
from .oss_sync import _object_key  # 同一 key 规则

logger = logging.getLogger("eagent.kb.reconcile")


def reconcile_kb(
    db: Database, kb_id: str, *, client: OssClient | None = None,
    oss_enabled: bool | None = None,
) -> dict:
    """单 KB 三方对账 + 恢复；返回计数并审计 kb.file.reconcile。"""
    if not dao.get_kb(db, kb_id):
        return {"kb_missing": True}
    if oss_enabled is None:
        oss_enabled = client is not None or oss_configured()
    if client is None and oss_enabled:
        client = build_client()

    counts = {"ok": 0, "pulled": 0, "rebuilt": 0, "pushed": 0,
              "raw_lost": 0, "corrupt": 0, "orphans": 0}
    expected_keys: set[str] = set()

    # ---- raw 源（不可再生：OSS 优先恢复，双缺标记 FAILED 人工重传）----
    for src in dao.list_sources(db, kb_id):
        local = storage.raw_path(kb_id, src["source_id"], src["file_name"])
        relkey = f"raw/{local.name}"
        expected_keys.add(relkey)
        local_ok = local.is_file() and \
            sha256_hex(local.read_bytes()) == src["sha256"]
        oss_ok = client.exists(_object_key(kb_id, relkey)) if client else None
        if local_ok:
            if client is not None and oss_ok is False:
                client.upload_file(_object_key(kb_id, relkey), local)
                counts["pushed"] += 1
            else:
                counts["ok"] += 1
        else:
            local.unlink(missing_ok=True)  # 清掉可能损坏的半文件
            if oss_ok:
                try:
                    client.download_file(_object_key(kb_id, relkey), local)
                    if sha256_hex(local.read_bytes()) == src["sha256"]:
                        counts["pulled"] += 1
                        continue
                    counts["corrupt"] += 1
                except Exception:  # noqa: BLE001
                    counts["corrupt"] += 1
            dao.set_source_status(
                db, src["source_id"], "FAILED", error_code="FILE_LOST",
                error_detail="raw 本地与对象存储双缺/损坏，需人工重新上传",
            )
            counts["raw_lost"] += 1
        # parsed 产物（可再生：缺失即重建，不入恢复矩阵）
        parsed = Path(settings.kb_storage_dir) / kb_id / "parsed" / \
            f"{src['source_id']}.md"
        expected_keys.add(f"parsed/{src['source_id']}.md")
        if not parsed.is_file() and local.is_file():
            from .parser import guess_mime, parse_bytes
            try:
                md = parse_bytes(
                    local.read_bytes(), file_name=src["file_name"],
                    mime_type=src.get("mime_type") or guess_mime(
                        src["file_name"], None),
                )
                storage.put_parsed(kb_id, src["source_id"], md)
            except Exception:  # noqa: BLE001 - 解析失败由编译任务兜底
                pass
    db.commit()

    # ---- wiki 页（可再生：DB body_md 是事实源，08 §2）----
    for page in dao.current_pages(db, kb_id):
        relkey = page["rel_path"]  # wiki 落盘 key = rel_path（storage.wiki_path）
        expected_keys.add(relkey)
        local = storage.wiki_path(kb_id, relkey)
        local_ok = local.is_file() and \
            local.read_text(encoding="utf-8", errors="replace") == \
            (page["body_md"] or "")
        oss_ok = client.exists(_object_key(kb_id, relkey)) if client else None
        if local_ok:
            if client is not None and oss_ok is False:
                client.upload_file(_object_key(kb_id, relkey), local)
                counts["pushed"] += 1
            else:
                counts["ok"] += 1
        elif client is not None and oss_ok:
            # 本地缺失/不一致 + OSS 有 → 拉回（OSS 为最近同步副本）
            try:
                client.download_file(_object_key(kb_id, relkey), local)
                counts["pulled"] += 1
                continue
            except Exception:  # noqa: BLE001
                counts["corrupt"] += 1
        # OSS 不可用/缺失 → 从 DB body_md 重建文件（事实源）
        storage.write_wiki_page(kb_id, relkey, page["body_md"] or "")
        counts["rebuilt"] += 1

    # ---- 孤儿文件（本地有、DB 无预期）：标记不删（GC/人工裁决）----
    for relkey, _path in storage.iter_local_files(kb_id):
        if relkey not in expected_keys:
            counts["orphans"] += 1

    dao.audit(
        db, user="system", action="kb.file.reconcile", resource_type="kb",
        resource_id=kb_id, detail=counts,
    )
    db.commit()
    logger.info("kb reconcile %s: %s", kb_id, counts)
    return counts


def reconcile_all(client: OssClient | None = None) -> list[dict]:
    db = Database()
    try:
        return [
            {"kb_id": kb["kb_id"],
             **reconcile_kb(db, kb["kb_id"], client=client)}
            for kb in dao.list_kbs(db)
        ]
    finally:
        db.close()


async def kb_reconcile_loop() -> None:
    """定时对账（默认每日；0 = 禁用，08 §4.3）。"""
    if settings.kb_reconcile_interval_s <= 0:
        return
    while True:
        await asyncio.sleep(settings.kb_reconcile_interval_s)
        try:
            reconcile_all()
        except Exception:  # noqa: BLE001
            logger.exception("kb reconcile loop failed")
