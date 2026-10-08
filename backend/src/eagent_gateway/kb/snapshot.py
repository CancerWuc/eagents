# -*- coding: utf-8 -*-
"""KB 快照导出（standalone-deployment 01 §4，S-02）。

把一个 KB 的一个 READY Rev 导出为独立容器可检索的只读快照目录
（平台侧专用；容器读侧见 kb/backends.py SnapshotBackend）：

  <dest>/manifest.json   kb_slug/kb_id/rev/display_name/search_config/
                         snapshot_format_version/
                         pages[{page_id,path,title,hash,sources}]/exported_at
  <dest>/wiki/...        页面文件（**一律以 DB body_md 重写**——导出即对账，
                         §4.2：快照侧文件即唯一事实源，杜绝落盘/DB 双源分叉）
  <dest>/graph.json      对称邻接 {src:[dst,...]}（缺省→检索退化纯 grep 单路）
  <dest>/chunks.json     {page_id:[{chunk_id,chunk_index,text_md},...]}
  <dest>/sources.json    {source_id: rel_path}（results.sources 溯源不退化）

**确定性快照哈希**（§4.2）：对内容不对 tar 字节（tar 含 mtime 不可复现），
= sha256( manifest 页 hash 按 path 排序拼接 ‖ graph 规范化 ‖ chunks 规范化 )；
exported_at 等非内容字段不参与——同 Rev 跨机重导必得同 hash
（S-06「可复现部署包」与 deploy_digest 联合锁定的前提，单测覆盖）。
"""
from __future__ import annotations

import json
import posixpath
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import ValidationError
from . import dao
from .backends import (
    CHUNKS_FILE,
    GRAPH_FILE,
    INDEX_FILE,
    MANIFEST_FILE,
    SNAPSHOT_FORMAT_VERSION,
    SOURCES_FILE,
)
from .backends import MySQLBackend
from .retrieval import resolve_rev

if TYPE_CHECKING:
    from ..persistence.db import Database


def export_kb_snapshot(
    db: Database, kb_id: str, dest_dir: str | Path, *,
    rev: str | None = "current",
) -> dict:
    """导出一个 KB 的指定 Rev（READY）为快照目录，返回导出摘要。

    返回 {dir, kb_id, kb_slug, rev, pages, snapshot_hash,
    snapshot_format_version}；snapshot_hash 为确定性内容哈希（§4.2）。
    dest 已存在时整体重建（快照可由平台随时重新导出，不是唯一副本）。
    """
    dest = Path(dest_dir)
    kb = dao.get_kb(db, kb_id)
    if not kb:
        raise ValidationError("KB 不存在")
    cur_rev = resolve_rev(MySQLBackend(db), kb, rev)
    pages_rows = dao.manifest_pages(db, kb_id, cur_rev)

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    # wiki 文件：以 DB body_md 重写（导出即对账，§4.2）
    for p in pages_rows:
        _write_wiki_file(dest, p["rel_path"], p["body_md"] or "")

    graph = _export_graph(db, kb_id)
    chunks = {
        p["page_id"]: [
            {"chunk_id": c["chunk_id"], "chunk_index": c["chunk_index"],
             "text_md": c["text_md"]}
            for c in dao.chunks_of_page(db, p["page_id"])
        ]
        for p in pages_rows
    }
    sources = {
        r["source_id"]: r["rel_path"]
        for r in db.query_all(
            "SELECT source_id, rel_path FROM eag_kb_source WHERE kb_id=:k",
            {"k": kb_id},
        )
    }
    manifest = {
        "kb_slug": kb["slug"],
        "kb_id": kb_id,
        "rev": cur_rev,
        "display_name": kb["display_name"],
        "search_config": dao.jload(kb.get("search_config")) or {},
        "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
        "pages": [
            {
                "page_id": p["page_id"],
                "path": p["rel_path"],
                "title": p["title"],
                "hash": p["content_hash"],
                "sources": (dao.jload(p.get("frontmatter_json")) or {})
                .get("sources") or [],
            }
            for p in pages_rows
        ],
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }

    _dump_json(dest / MANIFEST_FILE, manifest)
    _dump_json(dest / GRAPH_FILE, {s: sorted(v) for s, v in graph.items()})
    _dump_json(dest / CHUNKS_FILE, chunks)
    _dump_json(dest / SOURCES_FILE, sources)

    snap_hash = snapshot_hash(manifest, graph, chunks)
    return {
        "dir": str(dest),
        "kb_id": kb_id,
        "kb_slug": kb["slug"],
        "rev": cur_rev,
        "pages": len(manifest["pages"]),
        "snapshot_hash": snap_hash,
        "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
    }


def write_snapshot_index(root: str | Path, entries: list[dict]) -> Path:
    """多 KB 快照根：写顶层 index.json（{slug: {dir, kb_id, rev}}，01 §4.1）。

    entries = [{slug, dir, kb_id, rev}]；容器侧 load_snapshot_index 读取
    （kb/backends.py，S-04 装配用）。
    """
    index = {e["slug"]: {
        "dir": e["dir"], "kb_id": e["kb_id"], "rev": e["rev"],
    } for e in entries}
    path = Path(root) / INDEX_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump_json(path, index)
    return path


# ---------------------------------------------------------------------------
# 确定性哈希（§4.2：对内容不对 tar 字节；同 Rev 跨机重导必得同值）
# ---------------------------------------------------------------------------
# 纯函数实现移至 kb/snapshot_hash.py（导出侧与容器侧复核共用，S-04）
from .snapshot_hash import snapshot_hash  # noqa: E402,F401


def snapshot_hash_of_dir(snapshot_dir: str | Path) -> str:
    """从快照目录重算哈希（导出侧复核 / S-06 打包前校验用）。

    实现与容器侧 kb/backends.snapshot_dir_hash 同一入口。
    """
    from .backends import snapshot_dir_hash

    return snapshot_dir_hash(snapshot_dir)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _write_wiki_file(dest: Path, rel_path: str, body_md: str) -> None:
    """快照内写 wiki 页（S6 同款命名空间校验；fail-closed 拒绝非 wiki/ 路径）。"""
    norm = posixpath.normpath(rel_path or "")
    if (not norm.startswith("wiki/") or ".." in norm or norm.startswith("/")
            or "\\" in norm or "\x00" in norm):
        raise ValidationError(f"非法 wiki 路径: {rel_path}")
    path = dest / norm
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body_md, encoding="utf-8")


def _export_graph(db: Database, kb_id: str) -> dict[str, set[str]]:
    """eag_kb_graph_link 边 → 对称邻接表（双向收录；查询期过滤在 backend）。"""
    rows = db.query_all(
        "SELECT src_path, dst_path FROM eag_kb_graph_link WHERE kb_id=:k",
        {"k": kb_id},
    )
    adj: dict[str, set[str]] = {}
    for r in rows:
        s, d = r["src_path"], r["dst_path"]
        adj.setdefault(s, set()).add(d)
        adj.setdefault(d, set()).add(s)
    return adj


def _dump_json(path: Path, obj) -> None:
    path.write_text(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _load_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
