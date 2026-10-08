# -*- coding: utf-8 -*-
"""KB 数据访问层（SQLAlchemy Core，与仓库既有风格一致）。

- 只做表访问，不含业务规则；业务规则在 kb.service / kb.compile / kb.planner。
- JSON 列一律应用层解析（MySQL 5.7 不建索引，B1）。
- 「当前版本页面」= (kb_id, rel_path) 取 updated_at 最新行（02 §3.2）。
"""
from __future__ import annotations

import json

from ..persistence.db import Database, new_id, now
from .constants import (
    REV_RETAIN_WINDOW,
    config_fingerprint,
    page_content_hash,
)

# JSON 编码助手（utf-8，保持中文可读）


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def jload(raw, default=None):
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# 审计（02 §7.3：全部 kb.* 写事件落 eag_audit_event）
# ---------------------------------------------------------------------------
def audit(
    db: Database, *, user: str, action: str, resource_type: str,
    resource_id: str | None, detail: dict | None = None,
) -> None:
    db.execute(
        """INSERT INTO eag_audit_event
             (audit_id, cap_user_name, action, resource_type, resource_id,
              detail, created_at)
           VALUES (:aid, :u, :act, :rt, :rid, :d, :ts)""",
        {"aid": new_id("aud_"), "u": user or "anonymous", "act": action,
         "rt": resource_type, "rid": resource_id,
         "d": jdump(detail) if detail is not None else None, "ts": now()},
    )


# ---------------------------------------------------------------------------
# KB
# ---------------------------------------------------------------------------
def get_kb(db: Database, kb_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_knowledge_base WHERE kb_id=:kid", {"kid": kb_id},
    )


def update_kb_config(
    db: Database, kb_id: str, *,
    schema_text: str | None = None,
    purpose_text: str | None = None,
    schema_ai_managed: int | None = None,
    purpose_ai_managed: int | None = None,
) -> None:
    """schema/purpose 演进写入（011）：仅更新显式提供的字段。"""
    sets: list[str] = []
    params: dict[str, object] = {"kid": kb_id, "ts": now()}
    if schema_text is not None:
        sets.append("schema_text=:schema_text")
        params["schema_text"] = schema_text
    if purpose_text is not None:
        sets.append("purpose_text=:purpose_text")
        params["purpose_text"] = purpose_text
    if schema_ai_managed is not None:
        sets.append("schema_ai_managed=:schema_ai")
        params["schema_ai"] = schema_ai_managed
    if purpose_ai_managed is not None:
        sets.append("purpose_ai_managed=:purpose_ai")
        params["purpose_ai"] = purpose_ai_managed
    if not sets:
        return
    db.execute(
        f"UPDATE eag_knowledge_base SET {', '.join(sets)}, updated_at=:ts"
        " WHERE kb_id=:kid",
        params,
    )


def get_kb_by_slug(db: Database, slug: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_knowledge_base WHERE slug=:s", {"s": slug},
    )


def list_kbs(db: Database) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_knowledge_base ORDER BY created_at DESC",
    )


def kb_counts(db: Database, kb_id: str) -> dict:
    src = db.query_one(
        "SELECT COUNT(*) AS c FROM eag_kb_source"
        " WHERE kb_id=:k AND status<>'DELETED'", {"k": kb_id},
    )["c"]
    pages = db.query_one(
        "SELECT COUNT(*) AS c FROM eag_kb_page_latest WHERE kb_id=:k",
        {"k": kb_id},
    )["c"] if _latest_view_exists(db) else _latest_page_count(db, kb_id)
    binds = db.query_one(
        "SELECT COUNT(*) AS c FROM eag_kb_binding"
        " WHERE kb_id=:k AND enabled=1", {"k": kb_id},
    )["c"]
    return {"source_count": src, "page_count": pages, "binding_count": binds}


def _latest_view_exists(db: Database) -> bool:
    return False  # 一期不建视图（禁止发明 02 之外的库对象）


def _latest_page_count(db: Database, kb_id: str) -> int:
    return int(db.query_one(
        "SELECT COUNT(DISTINCT rel_path) AS n FROM eag_kb_page WHERE kb_id=:k",
        {"k": kb_id},
    )["n"])


# 硬删除（管理面 DELETE /knowledge-bases/{kb_id}）：11 张子表均以 kb_id 为
# 归属列，基础表无外键（架构现状），故按「子表 → 主表」顺序显式清理。
KB_CHILD_TABLES = (
    "eag_kb_rev_entry",
    "eag_kb_chunk",
    "eag_kb_graph_link",
    "eag_kb_review",
    "eag_kb_page",
    "eag_kb_rev",
    "eag_kb_task",
    "eag_kb_ingest_plan",
    "eag_kb_source",
    "eag_kb_binding",
    "eag_kb_embed_cache",
)


def active_task_count(db: Database, kb_id: str) -> int:
    return db.query_one(
        "SELECT COUNT(*) AS c FROM eag_kb_task"
        " WHERE kb_id=:k AND status IN ('PENDING', 'RUNNING')", {"k": kb_id},
    )["c"]


def binding_targets(db: Database, kb_id: str) -> list[dict]:
    """该 KB 被哪些总控/Agent 绑定（删除前置检查用）。"""
    return db.query_all(
        "SELECT target_type, target_id, enabled FROM eag_kb_binding"
        " WHERE kb_id=:k", {"k": kb_id},
    )


def purge_kb(db: Database, kb_id: str) -> dict:
    """物理删除 KB 及其全部子行；返回每表删除行数（审计用）。调用方负责提交。"""
    deleted: dict[str, int] = {}
    for table in KB_CHILD_TABLES:
        result = db.execute(
            f"DELETE FROM {table} WHERE kb_id=:k", {"k": kb_id},
        )
        deleted[table] = getattr(result, "rowcount", 0) or 0
    result = db.execute(
        "DELETE FROM eag_knowledge_base WHERE kb_id=:k", {"k": kb_id},
    )
    deleted["eag_knowledge_base"] = getattr(result, "rowcount", 0) or 0
    return deleted


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------
def get_source(db: Database, source_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_kb_source WHERE source_id=:sid", {"sid": source_id},
    )


def list_sources(db: Database, kb_id: str) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_kb_source WHERE kb_id=:k AND status<>'DELETED'"
        " ORDER BY created_at DESC",
        {"k": kb_id},
    )


def find_source_by_sha(db: Database, kb_id: str, sha256: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_kb_source WHERE kb_id=:k AND sha256=:s"
        " AND status<>'DELETED' LIMIT 1",
        {"k": kb_id, "s": sha256},
    )


def set_source_status(
    db: Database, source_id: str, status: str, *,
    error_code: str | None = None, error_detail: str | None = None,
    last_rev: str | None = None,
) -> None:
    sets = ["status=:st", "updated_at=:ts"]
    params: dict = {"sid": source_id, "st": status, "ts": now()}
    if error_code is not None:
        sets.append("error_code=:ec")
        params["ec"] = error_code
    if error_detail is not None:
        sets.append("error_detail=:ed")
        params["ed"] = error_detail[:1000]
    if last_rev is not None:
        sets.append("last_rev=:lr")
        params["lr"] = last_rev
    db.execute(
        f"UPDATE eag_kb_source SET {', '.join(sets)} WHERE source_id=:sid",
        params,
    )


# ---------------------------------------------------------------------------
# Page（内容寻址，B5）
# ---------------------------------------------------------------------------
def current_pages(db: Database, kb_id: str) -> list[dict]:
    """每 rel_path 的最新行 = 当前版本（MySQL 5.7 无窗口函数，应用层聚合）。"""
    rows = db.query_all(
        "SELECT * FROM eag_kb_page WHERE kb_id=:k"
        " ORDER BY rel_path, updated_at, page_id",
        {"k": kb_id},
    )
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["rel_path"]] = r
    return list(latest.values())


def current_page_by_path(db: Database, kb_id: str, rel_path: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_kb_page WHERE kb_id=:k AND rel_path=:p"
        " ORDER BY updated_at DESC, page_id DESC LIMIT 1",
        {"k": kb_id, "p": rel_path},
    )


def get_page(db: Database, page_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_kb_page WHERE page_id=:p", {"p": page_id},
    )


def upsert_page(
    db: Database, *, kb_id: str, rel_path: str, title: str,
    frontmatter: dict | None, body_md: str, source_hashes: list[str] | None,
) -> dict:
    """内容寻址写入：同 (kb_id, rel_path, content_hash) 复用行并顶为当前版本。"""
    fm = frontmatter or {}
    chash = page_content_hash(fm, body_md)
    existing = db.query_one(
        "SELECT * FROM eag_kb_page WHERE kb_id=:k AND rel_path=:p"
        " AND content_hash=:c",
        {"k": kb_id, "p": rel_path, "c": chash},
    )
    ts = now()
    if existing:
        # 复用旧行但把 updated_at 顶到最新（使其成为「当前版本」）
        db.execute(
            "UPDATE eag_kb_page SET updated_at=:ts, title=:t WHERE page_id=:pid",
            {"ts": ts, "t": title[:256], "pid": existing["page_id"]},
        )
        return dict(existing, updated_at=ts, title=title[:256])
    page_id = new_id("pg_")
    db.execute(
        """INSERT INTO eag_kb_page
             (page_id, kb_id, rel_path, title, content_hash, frontmatter_json,
              body_md, source_hashes_json, embedded, chunk_count, created_at,
              updated_at)
           VALUES (:pid, :k, :p, :t, :c, :fm, :body, :sh, 0, 0, :ts, :ts)""",
        {"pid": page_id, "k": kb_id, "p": rel_path, "t": title[:256],
         "c": chash, "fm": jdump(fm), "body": body_md,
         "sh": jdump(source_hashes or []), "ts": ts},
    )
    return get_page(db, page_id)


def mark_page_embedded(
    db: Database, page_id: str, *, ok: bool, chunk_count: int,
    error_code: str | None = None, error_detail: str | None = None,
) -> None:
    db.execute(
        """UPDATE eag_kb_page SET embedded=:ok, chunk_count=:cc,
             error_code=:ec, error_detail=:ed, updated_at=:ts
         WHERE page_id=:pid""",
        {"ok": 1 if ok else 0, "cc": chunk_count, "ec": error_code,
         "ed": error_detail, "ts": now(), "pid": page_id},
    )


# ---------------------------------------------------------------------------
# Rev 与 manifest（B5）
# ---------------------------------------------------------------------------
def next_rev_no(db: Database, kb_id: str) -> str:
    """rev_YYYYMMDD_NNN：按 kb 当日计数（同 KB 任务串行保证不冲突，02 §2.2）。"""
    today = now().strftime("%Y%m%d")
    prefix = f"rev_{today}_"
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM eag_kb_rev"
        " WHERE kb_id=:k AND rev LIKE :pfx",
        {"k": kb_id, "pfx": prefix + "%"},
    )
    return f"{prefix}{row['c'] + 1:03d}"


def create_rev(
    db: Database, *, kb_id: str, rev: str, created_by: str,
) -> None:
    db.execute(
        "INSERT INTO eag_kb_rev (rev, kb_id, status, created_by, created_at)"
        " VALUES (:rev, :k, 'BUILDING', :cb, :ts)"
        " ON DUPLICATE KEY UPDATE status='BUILDING'",
        {"rev": rev, "k": kb_id, "cb": created_by, "ts": now()},
    )


def finish_rev(
    db: Database, *, kb_id: str, rev: str, status: str,
    summary: dict | None = None, report: dict | None = None,
) -> None:
    db.execute(
        """UPDATE eag_kb_rev SET status=:st, summary_json=:sm, report_json=:rp,
             finished_at=:ts
         WHERE kb_id=:k AND rev=:rev""",
        {"st": status, "sm": jdump(summary) if summary else None,
         "rp": jdump(report) if report else None, "ts": now(),
         "k": kb_id, "rev": rev},
    )


def get_rev(db: Database, kb_id: str, rev: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_kb_rev WHERE kb_id=:k AND rev=:r",
        {"k": kb_id, "r": rev},
    )


def list_revs(db: Database, kb_id: str, limit: int = 20) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_kb_rev WHERE kb_id=:k"
        " ORDER BY created_at DESC LIMIT :n",
        {"k": kb_id, "n": limit},
    )


def write_manifest(
    db: Database, *, kb_id: str, rev: str,
    entries: list[dict],
) -> None:
    """publish 时一次性写入该 Rev 全部页面（02 §3.2）。"""
    db.execute(
        "DELETE FROM eag_kb_rev_entry WHERE kb_id=:k AND rev=:r",
        {"k": kb_id, "r": rev},
    )
    for e in entries:
        db.execute(
            """INSERT INTO eag_kb_rev_entry
                 (kb_id, rev, page_id, content_hash, chunk_ids_json)
               VALUES (:k, :r, :pid, :c, :ch)""",
            {"k": kb_id, "r": rev, "pid": e["page_id"], "c": e["content_hash"],
             "ch": jdump(e.get("chunk_ids") or [])},
        )


def manifest_of(db: Database, kb_id: str, rev: str) -> dict[str, str]:
    """rev → {page_id: content_hash}（当前 Rev 页面集合）。"""
    rows = db.query_all(
        "SELECT page_id, content_hash FROM eag_kb_rev_entry"
        " WHERE kb_id=:k AND rev=:r",
        {"k": kb_id, "r": rev},
    )
    return {r["page_id"]: r["content_hash"] for r in rows}


def manifest_pages(db: Database, kb_id: str, rev: str) -> list[dict]:
    """manifest join 页面行（检索候选过滤用，03 §5.2）。"""
    return db.query_all(
        """SELECT p.* FROM eag_kb_rev_entry e
            JOIN eag_kb_page p ON p.kb_id=e.kb_id AND p.page_id=e.page_id
           WHERE e.kb_id=:k AND e.rev=:r""",
        {"k": kb_id, "r": rev},
    )


def switch_current_rev(db: Database, kb_id: str, rev: str) -> None:
    db.execute(
        "UPDATE eag_knowledge_base SET current_rev=:r, status='READY',"
        " updated_at=:ts WHERE kb_id=:k",
        {"r": rev, "ts": now(), "k": kb_id},
    )


def ready_revs(db: Database, kb_id: str) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_kb_rev WHERE kb_id=:k AND status='READY'"
        " ORDER BY created_at DESC",
        {"k": kb_id},
    )


def revs_outside_retain_window(db: Database, kb_id: str) -> list[dict]:
    """GC 目标：最近 N 个 READY 之外的 Rev（03 §4.3；plan 引用保护在 gc 模块）。"""
    revs = ready_revs(db, kb_id)
    return revs[REV_RETAIN_WINDOW:]


# ---------------------------------------------------------------------------
# Chunk
# ---------------------------------------------------------------------------
def replace_page_chunks(
    db: Database, *, kb_id: str, page_id: str, page_content_hash: str,
    chunks: list[dict],
) -> list[str]:
    """页面内容变 → 整页 chunk 事务内先删后插（02 §3.2）。"""
    db.execute(
        "DELETE FROM eag_kb_chunk WHERE kb_id=:k AND page_id=:p",
        {"k": kb_id, "p": page_id},
    )
    ids = []
    for c in chunks:
        cid = c.get("chunk_id") or new_id("ch_")
        ids.append(cid)
        db.execute(
            """INSERT INTO eag_kb_chunk
                 (chunk_id, kb_id, page_id, page_content_hash, chunk_index,
                  text_md, token_count, vector_id, content_hash, created_at)
               VALUES (:cid, :k, :pid, :pch, :idx, :txt, :tc, :vid, :ch, :ts)""",
            {"cid": cid, "k": kb_id, "pid": page_id, "pch": page_content_hash,
             "idx": c["chunk_index"], "txt": c["text_md"],
             "tc": c.get("token_count", 0), "vid": c.get("vector_id"),
             "ch": c["content_hash"], "ts": now()},
        )
    return ids


def chunks_of_page(db: Database, page_id: str) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_kb_chunk WHERE page_id=:p ORDER BY chunk_index",
        {"p": page_id},
    )


# ---------------------------------------------------------------------------
# Embedding 缓存（B5/S8）
# ---------------------------------------------------------------------------
def embed_cache_get(
    db: Database, content_hash: str, config_fp: str,
) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_kb_embed_cache WHERE content_hash=:c"
        " AND config_fingerprint=:f",
        {"c": content_hash, "f": config_fp},
    )


def embed_cache_put(
    db: Database, *, kb_id: str, content_hash: str, config_fp: str,
    status: str, vectors_written: int,
) -> None:
    existing = embed_cache_get(db, content_hash, config_fp)
    if existing:
        db.execute(
            "UPDATE eag_kb_embed_cache SET status=:st, vectors_written=:vw,"
            " updated_at=:ts WHERE cache_id=:cid",
            {"st": status, "vw": vectors_written, "ts": now(),
             "cid": existing["cache_id"]},
        )
        return
    db.execute(
        """INSERT INTO eag_kb_embed_cache
             (cache_id, kb_id, content_hash, config_fingerprint, status,
              vectors_written, updated_at)
           VALUES (:cid, :k, :c, :f, :st, :vw, :ts)""",
        {"cid": new_id("ec_"), "k": kb_id, "c": content_hash, "f": config_fp,
         "st": status, "vw": vectors_written, "ts": now()},
    )


def embedding_config_of(kb: dict) -> dict:
    from .constants import normalize_embedding_config
    return normalize_embedding_config(jload(kb.get("embedding_config")))


def config_fp_of(kb: dict) -> str:
    return config_fingerprint(embedding_config_of(kb))


# ---------------------------------------------------------------------------
# Graph（wikilink 邻接表，编译期全量重建）
# ---------------------------------------------------------------------------
def rebuild_graph_links(db: Database, kb_id: str, links: list[tuple]) -> None:
    """links = [(src_path, dst_path)]；事务内 DELETE+INSERT 全量重建（02 §3.2）。"""
    db.execute("DELETE FROM eag_kb_graph_link WHERE kb_id=:k", {"k": kb_id})
    ts = now()
    for src, dst in links:
        db.execute(
            "INSERT IGNORE INTO eag_kb_graph_link"
            " (link_id, kb_id, src_path, dst_path, link_type, created_at)"
            " VALUES (:lid, :k, :s, :d, 'WIKILINK', :ts)",
            {"lid": new_id("lnk_"), "k": kb_id, "s": src, "d": dst, "ts": ts},
        )


def neighbors_of(db: Database, kb_id: str, paths: set[str]) -> dict[str, set[str]]:
    """对称邻接（边按无向处理）：path → 邻居集合（查询期只查表，03 §5.4）。"""
    if not paths:
        return {}
    rows = db.query_all(
        "SELECT src_path, dst_path FROM eag_kb_graph_link WHERE kb_id=:k",
        {"k": kb_id},
    )
    adj: dict[str, set[str]] = {}
    for r in rows:
        s, d = r["src_path"], r["dst_path"]
        if s in paths and d not in paths:
            adj.setdefault(s, set()).add(d)
        if d in paths and s not in paths:
            adj.setdefault(d, set()).add(s)
    return adj


def backlinks_of(db: Database, kb_id: str, path: str) -> list[str]:
    rows = db.query_all(
        "SELECT src_path FROM eag_kb_graph_link WHERE kb_id=:k AND dst_path=:p",
        {"k": kb_id, "p": path},
    )
    return [r["src_path"] for r in rows]


# ---------------------------------------------------------------------------
# Review（B2）
# ---------------------------------------------------------------------------
def insert_review(
    db: Database, *, kb_id: str, rev: str | None, page_id: str | None,
    type_: str, payload: dict | None,
) -> str:
    rid = new_id("rvw_")
    db.execute(
        """INSERT INTO eag_kb_review
             (review_id, kb_id, rev, page_id, type, payload_json, status,
              created_at)
           VALUES (:rid, :k, :rev, :pid, :t, :pl, 'OPEN', :ts)""",
        {"rid": rid, "k": kb_id, "rev": rev, "pid": page_id, "t": type_,
         "pl": jdump(payload) if payload else None, "ts": now()},
    )
    return rid


def list_reviews(
    db: Database, kb_id: str, status: str | None = None,
) -> list[dict]:
    if status:
        return db.query_all(
            "SELECT * FROM eag_kb_review WHERE kb_id=:k AND status=:st"
            " ORDER BY created_at DESC LIMIT 200",
            {"k": kb_id, "st": status},
        )
    return db.query_all(
        "SELECT * FROM eag_kb_review WHERE kb_id=:k"
        " ORDER BY created_at DESC LIMIT 200",
        {"k": kb_id},
    )


def get_review(db: Database, review_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_kb_review WHERE review_id=:r", {"r": review_id},
    )


def resolve_review(
    db: Database, review_id: str, *, status: str, resolved_by: str,
) -> None:
    db.execute(
        "UPDATE eag_kb_review SET status=:st, resolved_by=:u, resolved_at=:ts"
        " WHERE review_id=:r",
        {"st": status, "u": resolved_by, "ts": now(), "r": review_id},
    )


# ---------------------------------------------------------------------------
# Binding（S1）
# ---------------------------------------------------------------------------
def kb_bindings_for_target(
    db: Database, target_type: str, target_id: str, *, enabled_only: bool = True,
) -> list[dict]:
    sql = (
        "SELECT b.*, k.slug, k.display_name, k.status AS kb_status,"
        " k.current_rev FROM eag_kb_binding b"
        " JOIN eag_knowledge_base k ON k.kb_id = b.kb_id"
        f" WHERE b.target_type=:t AND b.target_id=:tid"
    )
    if enabled_only:
        sql += " AND b.enabled=1"
    return db.query_all(
        sql + " ORDER BY b.sort_order, b.created_at",
        {"t": target_type, "tid": target_id},
    )


def replace_bindings(
    db: Database, *, target_type: str, target_id: str, items: list[dict],
) -> None:
    """覆盖式设置（02 §5.1）：全删再插（事务内）。"""
    db.execute(
        "DELETE FROM eag_kb_binding WHERE target_type=:t AND target_id=:tid",
        {"t": target_type, "tid": target_id},
    )
    ts = now()
    for i, it in enumerate(items):
        db.execute(
            """INSERT INTO eag_kb_binding
                 (binding_id, target_type, target_id, kb_id, tool_prefix,
                  sort_order, enabled, created_at)
               VALUES (:bid, :t, :tid, :kid, :tp, :so, :en, :ts)""",
            {"bid": new_id("bdg_"), "t": target_type, "tid": target_id,
             "kid": it["kb_id"], "tp": it.get("tool_prefix"),
             "so": it.get("sort_order", i), "en": 1 if it.get("enabled", True) else 0,
             "ts": ts},
        )


# ---------------------------------------------------------------------------
# Ingest Plan
# ---------------------------------------------------------------------------
def insert_plan(
    db: Database, *, kb_id: str, source_ids: list[str], base_rev: str | None,
    proposal: dict, preview: dict | None, rationale_md: str,
    risk_md: str | None, created_by: str,
) -> str:
    pid = new_id("pln_")
    db.execute(
        """INSERT INTO eag_kb_ingest_plan
             (plan_id, kb_id, source_ids_json, status, plan_type, base_rev,
              proposal_json, preview_json, rationale_md, risk_md, created_by,
              created_at)
           VALUES (:pid, :k, :srcs, 'DRAFT', 'UPLOAD_PLAN', :br, :pr, :pv,
                   :rm, :rkm, :cb, :ts)""",
        {"pid": pid, "k": kb_id, "srcs": jdump(source_ids),
         "br": base_rev, "pr": jdump(proposal),
         "pv": jdump(preview) if preview else None,
         "rm": rationale_md, "rkm": risk_md, "cb": created_by, "ts": now()},
    )
    return pid


def get_plan(db: Database, plan_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_kb_ingest_plan WHERE plan_id=:p", {"p": plan_id},
    )


def list_plans(
    db: Database, kb_id: str, status: str | None = None,
) -> list[dict]:
    if status:
        return db.query_all(
            "SELECT * FROM eag_kb_ingest_plan WHERE kb_id=:k AND status=:st"
            " ORDER BY created_at DESC LIMIT 100",
            {"k": kb_id, "st": status},
        )
    return db.query_all(
        "SELECT * FROM eag_kb_ingest_plan WHERE kb_id=:k"
        " ORDER BY created_at DESC LIMIT 100",
        {"k": kb_id},
    )


def supersede_drafts(db: Database, kb_id: str) -> list[str]:
    """同 KB 未处理 DRAFT/REJECTED → SUPERSEDED（D7 互斥，调用方事务内）。"""
    rows = db.query_all(
        "SELECT plan_id FROM eag_kb_ingest_plan WHERE kb_id=:k"
        " AND status IN ('DRAFT','REJECTED')",
        {"k": kb_id},
    )
    for r in rows:
        db.execute(
            "UPDATE eag_kb_ingest_plan SET status='SUPERSEDED'"
            " WHERE plan_id=:p", {"p": r["plan_id"]},
        )
    return [r["plan_id"] for r in rows]


def update_plan_status(
    db: Database, plan_id: str, status: str, *,
    confirm_by: str | None = None, confirm_note: str | None = None,
    applied_rev: str | None = None, proposal: dict | None = None,
) -> None:
    sets = ["status=:st"]
    params: dict = {"st": status, "pid": plan_id}
    if confirm_by is not None:
        sets.append("confirm_by=:cb")
        params["cb"] = confirm_by
    if confirm_note is not None:
        sets.append("confirm_note=:cn")
        params["cn"] = confirm_note
    if applied_rev is not None:
        sets.extend(["applied_rev=:ar", "applied_at=:ts"])
        params.update({"ar": applied_rev, "ts": now()})
    if proposal is not None:
        sets.append("proposal_json=:pr")
        params["pr"] = jdump(proposal)
    if status in ("CONFIRMED", "REJECTED"):
        sets.append("confirmed_at=:ts2")
        params["ts2"] = now()
    db.execute(
        f"UPDATE eag_kb_ingest_plan SET {', '.join(sets)} WHERE plan_id=:pid",
        params,
    )


def recent_rejected_plans(
    db: Database, kb_id: str, limit: int,
) -> list[dict]:
    """S3：最近驳回原因注入后续规划 prompt（按 confirmed_at 倒序）。"""
    return db.query_all(
        "SELECT plan_id, confirm_note, rationale_md, proposal_json,"
        " confirmed_at FROM eag_kb_ingest_plan"
        " WHERE kb_id=:k AND status='REJECTED' AND confirm_note IS NOT NULL"
        " ORDER BY confirmed_at DESC LIMIT :n",
        {"k": kb_id, "n": limit},
    )


# ---------------------------------------------------------------------------
# Task（B4）
# ---------------------------------------------------------------------------
def create_task(
    db: Database, *, kb_id: str, kind: str, trigger_by: str,
    rev: str | None = None,
) -> dict:
    tid = new_id("tsk_")
    db.execute(
        """INSERT INTO eag_kb_task
             (task_id, kb_id, rev, kind, status, retry_count, progress_msg,
              cancel_flag, trigger_by, created_at)
           VALUES (:tid, :k, :rev, :kind, 'PENDING', 0, NULL, 0, :tb, :ts)""",
        {"tid": tid, "k": kb_id, "rev": rev, "kind": kind, "tb": trigger_by,
         "ts": now()},
    )
    return get_task(db, tid)


def get_task(db: Database, task_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_kb_task WHERE task_id=:t", {"t": task_id},
    )


def list_tasks(db: Database, kb_id: str, limit: int = 50) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_kb_task WHERE kb_id=:k"
        " ORDER BY created_at DESC LIMIT :n",
        {"k": kb_id, "n": limit},
    )


def has_running_task(db: Database, kb_id: str) -> bool:
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM eag_kb_task WHERE kb_id=:k"
        " AND status='RUNNING'",
        {"k": kb_id},
    )
    return row["c"] > 0


def set_task_status(
    db: Database, task_id: str, status: str, *,
    progress_msg: str | None = None, bump_retry: bool = False,
    set_finished: bool = False, rev: str | None = None,
) -> None:
    sets = ["status=:st"]
    params: dict = {"st": status, "tid": task_id}
    if progress_msg is not None:
        sets.append("progress_msg=:pm")
        params["pm"] = progress_msg[:1000]
    if bump_retry:
        sets.append("retry_count=retry_count+1")
    if set_finished:
        sets.append("finished_at=:fin")
        params["fin"] = now()
    if rev is not None:
        sets.append("rev=:rev")
        params["rev"] = rev
    db.execute(
        f"UPDATE eag_kb_task SET {', '.join(sets)} WHERE task_id=:tid", params,
    )


def set_task_progress(db: Database, task_id: str, msg: str) -> None:
    db.execute(
        "UPDATE eag_kb_task SET progress_msg=:pm WHERE task_id=:t",
        {"pm": msg[:1000], "t": task_id},
    )
    db.commit()


def request_cancel(db: Database, task_id: str) -> None:
    db.execute(
        "UPDATE eag_kb_task SET cancel_flag=1 WHERE task_id=:t",
        {"t": task_id},
    )
    db.commit()


def claim_next_pending(db: Database, kb_id: str) -> dict | None:
    """同 KB 串行：无 RUNNING 时取最早 PENDING 置 RUNNING（03 §2.3）。"""
    row = db.query_one(
        "SELECT task_id FROM eag_kb_task WHERE kb_id=:k AND status='PENDING'"
        " ORDER BY created_at, task_id LIMIT 1",
        {"k": kb_id},
    )
    if not row:
        return None
    db.execute(
        "UPDATE eag_kb_task SET status='RUNNING' WHERE task_id=:tid"
        " AND status='PENDING'",
        {"tid": row["task_id"]},
    )
    db.commit()
    return get_task(db, row["task_id"])


def reset_stale_running(db: Database) -> int:
    """进程重启清扫：滞留 RUNNING（上一进程崩溃）回 PENDING 重跑（幂等）。"""
    rows = db.query_all(
        "SELECT task_id FROM eag_kb_task WHERE status='RUNNING'",
    )
    for r in rows:
        db.execute(
            "UPDATE eag_kb_task SET status='PENDING' WHERE task_id=:t",
            {"t": r["task_id"]},
        )
    if rows:
        db.commit()
    return len(rows)


def pending_tasks(db: Database, limit: int = 20) -> list[dict]:
    return db.query_all(
        "SELECT DISTINCT kb_id FROM eag_kb_task WHERE status='PENDING'"
        " ORDER BY created_at LIMIT :n",
        {"n": limit},
    )
