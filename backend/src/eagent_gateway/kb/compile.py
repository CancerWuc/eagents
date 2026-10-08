# -*- coding: utf-8 -*-
"""KB 编译服务（03 §2/§3/§4）：七段管线 + Rev 发布 + 任务执行器衔接。

管线: parse → chunk → generate → lint → embed → index → publish rev
- INGEST  全七段（按已确认规划或默认单页入库）
- REBUILD 跳 generate/lint，从既有页面重 chunk→embed→index→publish
- EMBED   只跑 embed（embedded=0 / 带 error 的页），不发布新 Rev
任务状态机/重试/取消/串行 claim 在 kb/tasks.py（B4）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from ..persistence.db import Database
from . import dao, generator, storage
from .chunker import chunk_markdown
from .constants import (
    PLANNER_IDENTITY,
    TASK_MAX_RETRY,
    extract_wikilinks,
    normalize_chunking_config,
)
from .parser import ParseError, guess_mime, parse_bytes

logger = logging.getLogger("eagent.kb.compile")

_UNSAFE_PATH = re.compile(r"\.\.|^/|[\x00\\\s]")


class TaskCanceled(Exception):
    """检查点读到 cancel_flag=1（03 §2.2：任务内检查点响应取消）。"""


def _check_cancel(db: Database, task_id: str) -> None:
    task = dao.get_task(db, task_id)
    if task and task["cancel_flag"]:
        raise TaskCanceled()


def _slugify(name: str) -> str:
    stem = re.sub(r"\.(md|markdown|pdf|txt|docx?|json|csv)$", "", name or "doc")
    stem = re.sub(r"\s+", "-", stem.strip())
    stem = re.sub(r"[^\w\u4e00-\u9fff\-]", "", stem)
    return stem[:80] or "doc"


def route_dir_from_schema(schema_text: str | None, *, title: str,
                          file_name: str) -> str:
    """schema.md 目录路由（权威）：匹配 schema 中声明的目录关键词，兜底 wiki/sources/。"""
    text = schema_text or ""
    hint = f"{title} {file_name}"
    for m in re.finditer(r"[-*]\s*([a-z0-9_\-]+)\s*[/:：]\s*(.+)", text):
        dirname, desc = m.group(1), m.group(2)
        for kw in re.split(r"[\s,，、/]+", desc):
            if len(kw) >= 2 and kw in hint:
                return f"wiki/{dirname}/"
    return "wiki/sources/"


# ---------------------------------------------------------------------------
# 入口：执行一个已 claim（RUNNING）的任务
# ---------------------------------------------------------------------------
async def execute_task(task: dict) -> None:
    db = Database()
    try:
        if task["kind"] == "EMBED":
            await _run_embed_only(db, task)
        elif task["kind"] == "REBUILD":
            await _run_rebuild(db, task)
        else:
            await _run_ingest(db, task)
    finally:
        db.close()


async def _run_ingest(db: Database, task: dict) -> None:
    kb = dao.get_kb(db, task["kb_id"])
    if not kb:
        raise RuntimeError(f"KB 不存在: {task['kb_id']}")
    kb_id = kb["kb_id"]
    trigger = task["trigger_by"] or "system"

    # 已确认待执行规划（applied_rev IS NULL，按确认顺序）
    plans = [
        p for p in dao.list_plans(db, kb_id, status="CONFIRMED")
        if not p.get("applied_rev")
    ]
    plan_source_ids: set[str] = set()
    for p in plans:
        plan_source_ids.update(dao.jload(p["source_ids_json"], []) or [])

    # 默认入库集合：无规划覆盖且未 DONE 的源（PENDING/PLANNING/FAILED）
    all_sources = dao.list_sources(db, kb_id)
    default_sources = [
        s for s in all_sources
        if s["status"] in ("PENDING", "PLANNING", "FAILED")
        and s["source_id"] not in plan_source_ids
    ]
    plan_sources = [s for s in all_sources if s["source_id"] in plan_source_ids]

    if not plans and not default_sources:
        dao.set_task_status(
            db, task["task_id"], "DONE", progress_msg="无待编译变更",
            set_finished=True,
        )
        db.commit()
        return

    rev = task.get("rev") or dao.next_rev_no(db, kb_id)
    if not task.get("rev"):
        dao.set_task_status(db, task["task_id"], "RUNNING", rev=rev)
        db.commit()
        task = dao.get_task(db, task["task_id"])
    dao.create_rev(db, kb_id=kb_id, rev=rev, created_by=trigger)
    db.commit()
    progress = dao.set_task_progress

    summary = {"pages_created": 0, "pages_updated": 0, "pages_deleted": 0,
               "sources_added": 0}
    touched_pages: set[str] = set()   # 需重嵌/重建索引的页面 page_id
    created_paths: set[str] = set()

    try:
        # ---- parse：源 → Markdown（SHA 指纹增量：DONE 的源不进集合） ----
        todo = plan_sources + default_sources
        for i, src in enumerate(todo):
            _check_cancel(db, task["task_id"])
            progress(db, task["task_id"],
                           f"解析 {i + 1}/{len(todo)}: {src['file_name']}")
            dao.set_source_status(db, src["source_id"], "INGESTING")
            db.commit()
            parsed = storage.get_parsed(kb_id, src["source_id"])
            if parsed is None:
                data = storage.get_raw(kb_id, src["source_id"], src["file_name"])
                parsed = parse_bytes(
                    data, file_name=src["file_name"],
                    mime_type=src.get("mime_type") or guess_mime(
                        src["file_name"], None),
                )
                storage.put_parsed(kb_id, src["source_id"], parsed)

        # ---- generate：按规划动作（或默认单页入库）生成 wiki 页 ----
        action_results: list[dict] = []
        for plan in plans:
            progress(db, task["task_id"],
                           f"执行规划 {plan['plan_id']}")
            results = await _apply_plan(
                db, kb, plan, rev, touched_pages=touched_pages,
                created_paths=created_paths, task_id=task["task_id"],
            )
            action_results.append({"plan_id": plan["plan_id"], "results": results})
            db.commit()

        for i, src in enumerate(default_sources):
            _check_cancel(db, task["task_id"])
            progress(db, task["task_id"],
                           f"生成 {i + 1}/{len(default_sources)}: {src['file_name']}")
            parsed = storage.get_parsed(kb_id, src["source_id"]) or ""
            title = _title_of(src["file_name"], parsed)
            rel_dir = route_dir_from_schema(
                kb.get("schema_text"), title=title, file_name=src["file_name"],
            )
            path = f"{rel_dir}{_slugify(title)}.md"
            body = await generator.generate_page_body(
                title=title, source_markdown=parsed,
                purpose_text=kb.get("purpose_text"),
            )
            page = dao.upsert_page(
                db, kb_id=kb_id, rel_path=path, title=title,
                frontmatter=generator.build_frontmatter(
                    title=title, source_ids=[src["source_id"]],
                ),
                body_md=body,
                source_hashes=[src["sha256"]],
            )
            touched_pages.add(page["page_id"])
            created_paths.add(path)
            summary["pages_created"] += 1
            summary["sources_added"] += 1
            db.commit()

        # ---- 聚合页维护（先于链接重建：index.md 的 wikilink 是图邻接来源） ----
        _maintain_aggregates(db, kb_id, rev, summary)
        db.commit()

        # ---- lint：四类 Review 中的确定性子集（断链/重复/建议） ----
        progress(db, task["task_id"], "lint 扫描")
        lint_report = _lint_current(db, kb_id, rev)
        db.commit()

        # ---- embed + chunk + 向量（S8 纪律 + 缓存） ----
        await _chunk_pages(db, task, kb, touched_pages)

        # ---- index：wikilink 邻接表全量重建（D-2026-08-16-01 #2） ----
        progress(db, task["task_id"], "重建链接索引")
        pages = dao.current_pages(db, kb_id)
        path_by_name = generator.build_link_index(pages)
        links = [
            (p["rel_path"], path_by_name[target])
            for p in pages for target in extract_wikilinks(p["body_md"])
            if target in path_by_name
        ]
        dao.rebuild_graph_links(db, kb_id, links)
        db.commit()

        # ---- publish：manifest + 原子 current_rev 切换 + 回填 ----
        progress(db, task["task_id"], f"发布 {rev}")
        _publish_rev(
            db, kb, rev, summary=summary, lint_report=lint_report,
            sources_done=[s["source_id"] for s in todo],
            plan_results=action_results, trigger=trigger,
        )
        # ---- schema/purpose 自动演进（011：空置或 AI 维护中的字段） ----
        progress(db, task["task_id"], "维护 schema/purpose")
        await _evolve_kb_config(db, kb)
        progress(db, task["task_id"], f"完成: Rev {rev} 已发布")
        dao.set_task_status(
            db, task["task_id"], "DONE", progress_msg=f"Rev {rev} 已发布",
            set_finished=True,
        )
        db.commit()
    except TaskCanceled:
        _finalize_canceled(db, task, rev, todo)
        db.commit()


def _title_of(file_name: str, parsed_md: str) -> str:
    m = re.search(r"^#\s+(.+)$", parsed_md or "", re.M)
    if m:
        return m.group(1).strip()[:200]
    stem = re.sub(r"\.[^.]+$", "", file_name)
    return stem[:200]


# ---------------------------------------------------------------------------
# 规划执行（per-action 乐观校验 + 回写 expected_effect，04 §10.3）
# ---------------------------------------------------------------------------
async def _apply_plan(
    db: Database, kb: dict, plan: dict, rev: str, *,
    touched_pages: set[str], created_paths: set[str], task_id: str,
) -> list[dict]:
    kb_id = kb["kb_id"]
    proposal = dao.jload(plan["proposal_json"], {}) or {}
    changes = proposal.get("changes") or []
    confirmed = dao.jload(_plan_confirmed_selection(plan), []) or []

    base_rev = plan.get("base_rev")
    current = kb.get("current_rev")
    base_ok = (base_rev or None) == (current or None)

    results: list[dict] = []
    done_ids: set[str] = set()
    # 拓扑执行：depends_on 全部完成后再执行（D4 DAG）
    for _ in range(len(changes) + 1):
        progressed = False
        for ch in changes:
            aid = ch["action_id"]
            if aid in done_ids:
                continue
            deps = ch.get("depends_on") or []
            if any(d in {c["action_id"] for c in changes if c["action_id"] not in done_ids} for d in deps):
                continue  # 前置未完成
            if confirmed and aid not in confirmed:
                done_ids.add(aid)
                results.append({
                    "action_id": aid, "status": "SKIPPED",
                    "detail": "人工确认时未勾选该动作",
                })
                continue
            res = await _apply_one_change(
                db, kb, ch, base_ok=base_ok, touched_pages=touched_pages,
                created_paths=created_paths,
            )
            results.append({"action_id": aid, **res})
            done_ids.add(aid)
            progressed = True
            _check_cancel(db, task_id)
        if len(done_ids) == len(changes) or not progressed:
            break
    # 环/未执行动作 → SKIP 留痕
    for ch in changes:
        if ch["action_id"] not in done_ids:
            results.append({
                "action_id": ch["action_id"], "status": "SKIPPED",
                "detail": "依赖闭包未满足（生成期 DAG 错误或被取消）",
            })

    # 回填 per-action 结果（expected_effect 闭环度量，01 §6.6）
    for ch in changes:
        ch.pop("selected", None)
        match = next(
            (r for r in results if r["action_id"] == ch["action_id"]), None,
        )
        if match:
            ch["result"] = {
                "status": match["status"],
                "detail": match["detail"][:500],
                "rev": match.get("rev"),
            }
    proposal["changes"] = changes
    skipped = any(r["status"] == "SKIPPED" for r in results)
    plan_status = "APPLIED_WITH_SKIPS" if skipped else "APPLIED"
    dao.update_plan_status(
        db, plan["plan_id"], plan_status, applied_rev=rev, proposal=proposal,
    )
    dao.audit(
        db, user=PLANNER_IDENTITY if plan.get("created_by") == PLANNER_IDENTITY
        else plan.get("created_by") or "system",
        action="kb.plan.apply", resource_type="kb_ingest_plan",
        resource_id=plan["plan_id"],
        detail={"applied_rev": rev, "results": results},
    )
    return results


def _plan_confirmed_selection(plan: dict) -> str | None:
    # 勾选子集记录在 confirm_note 约定键（不新增列，02 DDL 为准）
    note = plan.get("confirm_note") or ""
    m = re.search(r"\[selected:(.*?)\]", note, re.S)
    return m.group(1) if m else None


async def _apply_one_change(
    db: Database, kb: dict, ch: dict, *, base_ok: bool,
    touched_pages: set[str], created_paths: set[str],
) -> dict:
    kb_id = kb["kb_id"]
    action = ch.get("action")

    if action == "CREATE_PAGE":
        path = ch.get("path") or ""
        if not path.startswith("wiki/"):
            return {"status": "SKIPPED", "detail": f"非法路径 {path}"}
        existing = dao.current_page_by_path(db, kb_id, path)
        if existing and not base_ok:
            return {
                "status": "SKIPPED",
                "detail": "页面已存在且 base_rev 已变化（乐观校验失败）",
            }
        src_ids = ch.get("sources") or []
        bodies: list[str] = []
        hashes: list[str] = []
        for sid in src_ids:
            src = dao.get_source(db, sid)
            if not src or src["kb_id"] != kb_id:
                return {"status": "SKIPPED", "detail": f"源不存在 {sid}"}
            parsed = storage.get_parsed(kb_id, sid)
            if parsed is None:
                try:
                    data = storage.get_raw(kb_id, sid, src["file_name"])
                    parsed = parse_bytes(
                        data, file_name=src["file_name"],
                        mime_type=src.get("mime_type"),
                    )
                    storage.put_parsed(kb_id, sid, parsed)
                except ParseError as exc:
                    dao.set_source_status(
                        db, sid, "FAILED", error_code=exc.code,
                        error_detail=exc.detail,
                    )
                    return {"status": "SKIPPED", "detail": f"源解析失败: {exc.code}"}
            bodies.append(parsed)
            hashes.append(src["sha256"])
        title = ch.get("title") or path.rsplit("/", 1)[-1].removesuffix(".md")
        body = await generator.generate_page_body(
            title=title, source_markdown="\n\n".join(bodies),
            purpose_text=kb.get("purpose_text"),
        )
        page = dao.upsert_page(
            db, kb_id=kb_id, rel_path=path, title=title,
            frontmatter=generator.build_frontmatter(
                title=title, source_ids=src_ids,
            ),
            body_md=body, source_hashes=hashes,
        )
        touched_pages.add(page["page_id"])
        created_paths.add(path)
        return {"status": "APPLIED",
                "detail": f"已生成页面 {path}", "rev": None}

    if action == "UPDATE_PAGE":
        target = ch.get("target") or ""
        page = dao.current_page_by_path(db, kb_id, target)
        if not page:
            return {"status": "SKIPPED", "detail": f"目标页不存在 {target}"}
        if not base_ok:
            return {"status": "SKIPPED", "detail": "base_rev 已变化（乐观校验）"}
        if ch.get("target_hash") and page["content_hash"] != ch["target_hash"]:
            return {
                "status": "SKIPPED",
                "detail": "目标页内容已变化（content_hash 乐观校验失败）",
            }
        note = ch.get("effect") or ch.get("reason") or "内容更新标注"
        new_body = generator.inject_update_note(page["body_md"], note=note)
        fm = dao.jload(page["frontmatter_json"], {}) or {}
        new_page = dao.upsert_page(
            db, kb_id=kb_id, rel_path=target, title=page["title"],
            frontmatter=fm, body_md=new_body,
            source_hashes=dao.jload(page["source_hashes_json"], []) or [],
        )
        touched_pages.add(new_page["page_id"])
        return {"status": "APPLIED", "detail": f"已更新页面 {target}"}

    if action == "RELINK":
        targets = ch.get("targets") or []
        if not base_ok:
            return {"status": "SKIPPED", "detail": "base_rev 已变化（乐观校验）"}
        applied_any = False
        detail_parts = []
        for t in targets:
            page = dao.current_page_by_path(db, kb_id, t)
            if not page:
                detail_parts.append(f"{t}: 页面不存在")
                continue
            if ch.get("target_hash") and page["content_hash"] != ch["target_hash"]:
                detail_parts.append(f"{t}: 内容已变化")
                continue
            new_body, skip = generator.apply_relink_first_occurrence(
                page["body_md"], ch.get("from") or "", ch.get("to") or "",
            )
            if new_body is None:
                detail_parts.append(f"{t}: {skip}")
                continue
            fm = dao.jload(page["frontmatter_json"], {}) or {}
            new_page = dao.upsert_page(
                db, kb_id=kb_id, rel_path=t, title=page["title"],
                frontmatter=fm, body_md=new_body,
                source_hashes=dao.jload(page["source_hashes_json"], []) or [],
            )
            touched_pages.add(new_page["page_id"])
            applied_any = True
            detail_parts.append(f"{t}: 首现替换完成")
        if not applied_any:
            return {"status": "SKIPPED", "detail": "; ".join(detail_parts) or "无可替换链接"}
        return {"status": "APPLIED", "detail": "; ".join(detail_parts)}

    if action == "UPDATE_INDEX_LOG":
        # 聚合页由代码在 publish 阶段维护（03 §3.3-2），此处仅登记
        return {"status": "APPLIED", "detail": "index/log 由代码在发布时维护"}

    return {"status": "SKIPPED", "detail": f"未知动作 {action}"}


# ---------------------------------------------------------------------------
# lint（B2 四类型中的确定性子集；contradiction 由 LLM 二期增强）
# ---------------------------------------------------------------------------
def _lint_current(db: Database, kb_id: str, rev: str) -> dict:
    pages = dao.current_pages(db, kb_id)
    names = set(generator.build_link_index(pages))
    issues = 0
    titles: dict[str, list[str]] = {}
    for p in pages:
        # 断链 → missing_page
        for target in extract_wikilinks(p["body_md"]):
            if target not in names:
                dao.insert_review(
                    db, kb_id=kb_id, rev=rev, page_id=p["page_id"],
                    type_="missing_page",
                    payload={
                        "evidence": f"断链 [[{target}]]（目标页不存在）",
                        "locations": [p["rel_path"]],
                        "options": ["Create Page", "Skip"],
                    },
                )
                issues += 1
        titles.setdefault(p["title"], []).append(p["rel_path"])
    # 同名页 → duplicate
    for title, paths in titles.items():
        if len(paths) > 1:
            dao.insert_review(
                db, kb_id=kb_id, rev=rev, page_id=None, type_="duplicate",
                payload={
                    "evidence": f"同名页面 {len(paths)} 个",
                    "locations": paths, "options": ["Merge", "Rename", "Skip"],
                },
            )
            issues += 1
    return {"lint_issues": issues}


# ---------------------------------------------------------------------------
# chunk 段（D-2026-08-16-02 去向量化：chunk 仍由管线产出供契约 chunks[] 回显，
# 但不再计算向量/写向量库/embed_cache——08 §3.4/§3.5）
# ---------------------------------------------------------------------------
async def _chunk_pages(
    db: Database, task: dict, kb: dict, page_ids: set[str],
) -> None:
    if not page_ids:
        return
    kb_id = kb["kb_id"]
    chunk_cfg = normalize_chunking_config(dao.jload(kb.get("chunking_config")))
    pages = [dao.get_page(db, pid) for pid in page_ids]
    pages = [p for p in pages if p]
    total = len(pages)
    done = 0
    for page in pages:
        _check_cancel(db, task["task_id"])
        if page["embedded"] and page["chunk_count"] > 0:
            done += 1  # 页面未变 → chunk 行跨 Rev 复用（B5）
            dao.set_task_progress(
                db, task["task_id"], f"chunk {done}/{total}（复用）",
            )
            continue
        chunks = chunk_markdown(
            page["body_md"], max_tokens=chunk_cfg["max_tokens"],
            overlap=chunk_cfg["overlap"],
            title_hierarchy=chunk_cfg["title_hierarchy"],
        )
        dao.replace_page_chunks(
            db, kb_id=kb_id, page_id=page["page_id"],
            page_content_hash=page["content_hash"], chunks=chunks,
        )
        dao.mark_page_embedded(
            db, page["page_id"], ok=True, chunk_count=len(chunks),
        )
        db.commit()
        done += 1
        dao.set_task_progress(
            db, task["task_id"], f"chunk {done}/{total}: {page['rel_path']}",
        )


# ---------------------------------------------------------------------------
# publish（03 §4.4：manifest + 原子 current_rev + 回填）
# ---------------------------------------------------------------------------
def _maintain_aggregates(db: Database, kb_id: str, rev: str, summary: dict) -> None:
    """聚合页由代码维护（LLM 永不整页重写，03 §3.3-2）。

    必须在链接重建（index 段）之前执行：index.md 的 [[wikilink]] 是图邻接的
    主要来源（D-2026-08-16-01 #2）。
    """
    pages = dao.current_pages(db, kb_id)
    dao.upsert_page(
        db, kb_id=kb_id, rel_path=generator.INDEX_PATH, title="知识库索引",
        frontmatter={"title": "知识库索引", "system": True},
        body_md=generator.build_index_md(pages),
        source_hashes=[],
    )
    old_log = None
    log_row = dao.current_page_by_path(db, kb_id, generator.LOG_PATH)
    if log_row:
        old_log = log_row["body_md"]
    dao.upsert_page(
        db, kb_id=kb_id, rel_path=generator.LOG_PATH, title="变更日志",
        frontmatter={"title": "变更日志", "system": True},
        body_md=generator.build_log_md(old_log, rev=rev, summary=summary),
        source_hashes=[],
    )


async def _evolve_kb_config(db: Database, kb: dict) -> None:
    """schema/purpose 自动演进（011，发布 Rev 后调用）。

    规则: 字段为空或 ai_managed=1 → AI/启发式生成并持续完善；
    用户填过（非空且 ai_managed=0）→ 人工权威，不改写。
    演进失败只记日志，绝不阻断编译任务。"""
    kb_id = kb["kb_id"]
    schema_managed = bool(kb.get("schema_ai_managed")) or not (
        kb.get("schema_text") or ""
    ).strip()
    purpose_managed = bool(kb.get("purpose_ai_managed")) or not (
        kb.get("purpose_text") or ""
    ).strip()
    if not (schema_managed or purpose_managed):
        return
    pages = dao.current_pages(db, kb_id)
    if not pages:
        return
    pages_summary = "\n".join(
        f"- {p['rel_path']} ｜ {p.get('title') or ''}" for p in pages[:200]
    )
    draft: dict | None = None
    try:
        draft = await generator.evolve_kb_config(
            kb_name=kb.get("display_name") or kb_id,
            kb_description=kb.get("description"),
            current_schema=kb.get("schema_text"),
            current_purpose=kb.get("purpose_text"),
            pages_summary=pages_summary,
        )
    except Exception as exc:  # noqa: BLE001 演进是尽力而为
        logger.warning("kb %s schema/purpose LLM 演进失败: %s", kb_id, exc)
    if not draft:
        draft = generator.heuristic_schema_purpose(
            pages, kb.get("display_name") or kb_id, kb.get("description"),
        )
    updates: dict = {}
    if schema_managed and (draft.get("schema_md") or "").strip():
        updates.update(
            schema_text=draft["schema_md"].strip(), schema_ai_managed=1,
        )
    if purpose_managed and (draft.get("purpose_md") or "").strip():
        updates.update(
            purpose_text=draft["purpose_md"].strip(), purpose_ai_managed=1,
        )
    if not updates:
        return
    dao.update_kb_config(db, kb_id, **updates)
    db.commit()
    logger.info("kb %s schema/purpose 已自动演进 (ai_managed)", kb_id)


def _publish_rev(
    db: Database, kb: dict, rev: str, *, summary: dict, lint_report: dict,
    sources_done: list[str], plan_results: list[dict], trigger: str,
) -> None:
    kb_id = kb["kb_id"]
    pages = dao.current_pages(db, kb_id)
    entries = []
    for p in pages:
        chunk_ids = [c["chunk_id"] for c in dao.chunks_of_page(db, p["page_id"])]
        entries.append({
            "page_id": p["page_id"], "content_hash": p["content_hash"],
            "chunk_ids": chunk_ids,
        })
    dao.write_manifest(db, kb_id=kb_id, rev=rev, entries=entries)
    dao.finish_rev(
        db, kb_id=kb_id, rev=rev, status="READY",
        summary=summary,
        report={**lint_report, "plan_results": plan_results},
    )
    dao.switch_current_rev(db, kb_id, rev)
    for sid in sources_done:
        dao.set_source_status(db, sid, "DONE", last_rev=rev)
    db.commit()
    # R-01 落盘双写（08 §4.1）：当前 Rev 全部页面写 <root>/<kb_id>/wiki/，
    # 幂等覆盖（内容一致跳过、manifest 外文件删除）；写失败抛异常 → 任务
    # FAIL 可重试（对账任务兜底，08 §4.3）
    disk_counts = storage.sync_wiki_dir(
        kb_id, [{"rel_path": p["rel_path"], "body_md": p["body_md"]}
                for p in pages],
    )
    logger.info(
        "kb %s publish %s: wiki 落盘 written=%s unchanged=%s removed=%s",
        kb_id, rev, disk_counts["written"], disk_counts["unchanged"],
        disk_counts["removed"],
    )


def _finalize_canceled(
    db: Database, task: dict, rev: str, sources: list[dict],
) -> None:
    """取消：任务 CANCELED、半成品 Rev FAILED 不发布、源回退 PENDING（03 §2.2）。"""
    dao.finish_rev(db, kb_id=task["kb_id"], rev=rev, status="FAILED",
                   report={"note": "canceled"})
    for s in sources:
        if s["status"] == "INGESTING":
            dao.set_source_status(db, s["source_id"], "PENDING")
    dao.set_task_status(
        db, task["task_id"], "CANCELED", progress_msg="已取消（检查点终止）",
        set_finished=True,
    )


# ---------------------------------------------------------------------------
# REBUILD / EMBED
# ---------------------------------------------------------------------------
async def _run_rebuild(db: Database, task: dict) -> None:
    kb = dao.get_kb(db, task["kb_id"])
    if not kb:
        raise RuntimeError(f"KB 不存在: {task['kb_id']}")
    rev = task.get("rev") or dao.next_rev_no(db, kb_id := kb["kb_id"])
    if not task.get("rev"):
        dao.set_task_status(db, task["task_id"], "RUNNING", rev=rev)
        db.commit()
        task = dao.get_task(db, task["task_id"])
    dao.create_rev(db, kb_id=kb["kb_id"], rev=rev, created_by=task["trigger_by"])
    db.commit()
    pages = dao.current_pages(db, kb["kb_id"])
    summary = {"pages_created": 0, "pages_updated": len(pages),
               "pages_deleted": 0, "sources_added": 0}
    try:
        # 聚合页刷新 + 全量重嵌（缓存命中自动跳过未变页）+ 重建链接
        dao.set_task_progress(db, task["task_id"], f"重建索引 {len(pages)} 页")
        _maintain_aggregates(db, kb["kb_id"], rev, summary)
        db.commit()
        pages = dao.current_pages(db, kb["kb_id"])
        await _chunk_pages(db, task, kb, {p["page_id"] for p in pages})
        _check_cancel(db, task["task_id"])
        path_by_name = generator.build_link_index(pages)
        links = [
            (p["rel_path"], path_by_name[t])
            for p in pages for t in extract_wikilinks(p["body_md"])
            if t in path_by_name
        ]
        dao.rebuild_graph_links(db, kb["kb_id"], links)
        db.commit()
        _publish_rev(
            db, kb, rev, summary=summary, lint_report={"lint_issues": 0},
            sources_done=[], plan_results=[], trigger=task["trigger_by"],
        )
        await _evolve_kb_config(db, kb)
        dao.set_task_status(
            db, task["task_id"], "DONE", progress_msg=f"索引已重建: {rev}",
            set_finished=True,
        )
        db.commit()
    except TaskCanceled:
        _finalize_canceled(db, task, rev, [])
        db.commit()


async def _run_embed_only(db: Database, task: dict) -> None:
    kb = dao.get_kb(db, task["kb_id"])
    if not kb:
        raise RuntimeError(f"KB 不存在: {task['kb_id']}")
    pages = [
        p for p in dao.current_pages(db, kb["kb_id"])
        if not p["embedded"] or p.get("error_code")
    ]
    try:
        await _chunk_pages(db, task, kb, {p["page_id"] for p in pages})
        dao.set_task_status(
            db, task["task_id"], "DONE",
            progress_msg=f"重嵌完成 {len(pages)} 页", set_finished=True,
        )
        db.commit()
    except TaskCanceled:
        dao.set_task_status(
            db, task["task_id"], "CANCELED", progress_msg="已取消",
            set_finished=True,
        )
        db.commit()
