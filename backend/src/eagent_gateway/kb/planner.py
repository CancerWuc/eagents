# -*- coding: utf-8 -*-
"""KB Ingest Planner —— 非对话式入库规划服务（04 全文）。

- 事件驱动（上传成功 / 手动 :plan），无聊天窗口；只产出 DRAFT。
- 只读分析面：内部 DAO（结构摘要 S12）+ Runtime 只读检索（相关性初筛）；
  唯一写出口 = eag_kb_ingest_plan 的 DRAFT 创建 / 旧稿 SUPERSEDED。
- D7 并发规则：30s 防抖合并窗口 + 同 KB DRAFT 互斥 + 上传侧 SHA256 去重。
- 预算（04 §6.3）：8 工具轮次 / 120s wall-clock；超时降级为保守 DRAFT
  （整份文档默认单页入库，confidence=low），不阻塞上传。
- S3：最近 N 条驳回原因注入规划 prompt（非参数更新）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from ..config import settings
from ..contracts import KB_PLAN_PROPOSAL_SCHEMA, validate
from ..persistence.db import Database
from . import dao, generator, retrieval, storage
from .backends import MySQLBackend
from .compile import _slugify, route_dir_from_schema
from .constants import (
    PLANNER_BUDGET_S,
    PLANNER_DEBOUNCE_S,
    PLANNER_IDENTITY,
    PLANNER_MAX_TOOL_ROUNDS,
    PLANNER_MAX_TOOL_ROUNDS_HARD_CAP,
    PLANNER_REJECTED_HISTORY_N,
)
from .parser import ParseError, parse_bytes

logger = logging.getLogger("eagent.kb.planner")

# D7：per-KB 防抖合并窗口（进程内）
_windows: dict[str, dict] = {}
_windows_lock = asyncio.Lock()


class Budget:
    """规划预算：工具轮次 + wall-clock（04 §6.3）。"""

    def __init__(self, rounds: int = PLANNER_MAX_TOOL_ROUNDS,
                 seconds: float = PLANNER_BUDGET_S):
        self.rounds_left = rounds
        self.deadline = time.monotonic() + seconds
        self.exhausted = False

    def tool_round(self) -> bool:
        """一次只读检索调用记一轮；超预算返回 False（触发降级）。"""
        self.rounds_left -= 1
        if self.rounds_left < 0 or time.monotonic() > self.deadline:
            self.exhausted = True
            return False
        return True


# ---------------------------------------------------------------------------
# 触发面（04 §2.2/§2.3）
# ---------------------------------------------------------------------------
def notify_upload(kb_id: str, source_id: str) -> None:
    """上传成功事件：并入 per-KB 防抖窗口；无人持有窗口则开一个。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_register_and_schedule(kb_id, source_id))


async def _register_and_schedule(kb_id: str, source_id: str) -> None:
    async with _windows_lock:
        win = _windows.get(kb_id)
        now = time.monotonic()
        if win and now < win["deadline"]:
            win["source_ids"].append(source_id)
            return
        _windows[kb_id] = {
            "source_ids": [source_id],
            "deadline": now + settings.kb_planner_debounce_s,
        }
    await asyncio.sleep(max(
        settings.kb_planner_debounce_s, 0.0,
    ))
    async with _windows_lock:
        win = _windows.pop(kb_id, None)
    if not win:
        return
    try:
        await run_plan_for_sources(kb_id, win["source_ids"])
    except Exception:  # noqa: BLE001 - 规划失败不阻塞上传（04 §6.3）
        logger.exception("planner run failed kb=%s", kb_id)


async def run_plan_for_sources(kb_id: str, source_ids: list[str]) -> dict | None:
    """对一个批次的源执行规划 → DRAFT 落库（管理面置 PLANNING 后调用）。"""
    db = Database()
    try:
        kb = dao.get_kb(db, kb_id)
        if not kb:
            logger.warning("planner: kb 不存在 %s", kb_id)
            return None
        # 管理面置位：PENDING → PLANNING（04 §4.4）
        for sid in source_ids:
            src = dao.get_source(db, sid)
            if src and src["status"] == "PENDING":
                dao.set_source_status(db, sid, "PLANNING")
        db.commit()

        # 预算按批次规模伸缩：_related_pages 是**每个源一轮**的本地只读检索，
        # 固定 8 轮时任何超过 8 份文档的批次都会在第 9 份必然耗尽，整批降级到
        # 保守方案（文件名当标题、不做结构分析）。实测 16/19 份的批次 100%
        # 踩中。这里给每个源留一轮再加固定余量，同时设硬顶防失控。
        budget = Budget(
            rounds=min(
                max(PLANNER_MAX_TOOL_ROUNDS, len(source_ids) + 2),
                PLANNER_MAX_TOOL_ROUNDS_HARD_CAP,
            ),
            seconds=settings.kb_planner_budget_s or PLANNER_BUDGET_S,
        )
        try:
            proposal, preview, rationale, risk = await _analyze(
                db, kb, source_ids, budget,
            )
            degraded = False
        except _BudgetExhausted as exc:
            proposal, preview = _conservative_proposal(db, kb, source_ids)
            rationale = (
                f"规划预算耗尽（{exc}），降级为保守方案：每份文档默认单页入库，"
                "请人工确认或驳回后重新规划。"
            )
            risk = "降级方案未做结构分析，可能存在放置冲突"
            degraded = True
        proposal["confidence"] = "low" if degraded else proposal.get("confidence", "medium")

        errors_ = validate(proposal, KB_PLAN_PROPOSAL_SCHEMA)
        if errors_:
            logger.warning("planner proposal 契约校验失败: %s", errors_)
            proposal, preview = _conservative_proposal(db, kb, source_ids)
            rationale = "生成的规划未通过契约校验，已降级为保守单页方案。"
            proposal["confidence"] = "low"

        # 同 KB 互斥：旧 DRAFT/REJECTED → SUPERSEDED + 新 DRAFT 插入（同一事务）
        db.execute(
            "SELECT kb_id FROM eag_knowledge_base WHERE kb_id=:k FOR UPDATE",
            {"k": kb_id},
        )
        superseded = dao.supersede_drafts(db, kb_id)
        plan_id = dao.insert_plan(
            db, kb_id=kb_id, source_ids=source_ids,
            base_rev=kb.get("current_rev"), proposal=proposal,
            preview=preview, rationale_md=rationale, risk_md=risk,
            created_by=PLANNER_IDENTITY,
        )
        dao.audit(
            db, user=PLANNER_IDENTITY, action="kb.plan.create",
            resource_type="kb_ingest_plan", resource_id=plan_id,
            detail={"trigger": "upload", "source_ids": source_ids,
                    "superseded": superseded, "degraded": degraded},
        )
        db.commit()
        # 规划完成后源回到 PENDING 等编译（或被驳回重新规划）
        for sid in source_ids:
            src = dao.get_source(db, sid)
            if src and src["status"] == "PLANNING":
                dao.set_source_status(db, sid, "PENDING")
        db.commit()
        return dao.get_plan(db, plan_id)
    finally:
        db.close()


class _BudgetExhausted(Exception):
    pass


def _title_from_source(
    db: Database, kb: dict, sid: str, file_name: str,
) -> str:
    """源文档的首个一级标题；取不到才退回文件名去扩展名。

    与 _heuristic_proposal 的标题口径保持一致，供降级路径复用。
    """
    parsed = storage.get_parsed(kb["kb_id"], sid)
    if parsed is None:
        src = dao.get_source(db, sid)
        try:
            parsed = parse_bytes(
                storage.get_raw(kb["kb_id"], sid, file_name),
                file_name=file_name,
                mime_type=(src or {}).get("mime_type"),
            )
        except (ParseError, OSError):
            parsed = ""
    m = re.search(r"^#\s+(.+)$", parsed or "", re.M)
    if m:
        return m.group(1).strip()[:200]
    return re.sub(r"\.[^.]+$", "", file_name)


# ---------------------------------------------------------------------------
# 分析（04 §6）：LLM 可用走结构化产出；否则确定性启发式
# ---------------------------------------------------------------------------
async def _analyze(
    db: Database, kb: dict, source_ids: list[str], budget: Budget,
) -> tuple[dict, dict, str, str]:
    # 输入 2：源解析摘要（planner 只读消费编译面解析产物，04 §5.1）
    sources_md: dict[str, str] = {}
    for sid in source_ids:
        src = dao.get_source(db, sid)
        if not src:
            continue
        parsed = storage.get_parsed(kb["kb_id"], sid)
        if parsed is None:
            try:
                data = storage.get_raw(kb["kb_id"], sid, src["file_name"])
                parsed = parse_bytes(
                    data, file_name=src["file_name"],
                    mime_type=src.get("mime_type"),
                )
                storage.put_parsed(kb["kb_id"], sid, parsed)
            except ParseError as exc:
                parsed = f"（解析失败 {exc.code}: {exc.detail}）"
        sources_md[sid] = parsed

    # 输入 3：结构摘要走内部 DAO（S12，不走检索工具）
    pages = dao.current_pages(db, kb["kb_id"])
    structure = "\n".join(
        f"- {p['rel_path']}（{p['title']}，{len(p['body_md'] or '')} 字符）"
        for p in pages[:200]
    ) or "（空知识库，尚无页面）"

    # 输入 4：相关既有页面（只读检索初筛，计入预算轮次）
    related: dict[str, list[str]] = {}
    for sid, md in sources_md.items():
        if not budget.tool_round():
            raise _BudgetExhausted("工具轮次耗尽")
        related[sid] = await _related_pages(db, kb, md)

    # 输入 5：OPEN Review；输入 6：最近驳回原因（S3）
    open_reviews = dao.list_reviews(db, kb["kb_id"], status="OPEN")[:10]
    rejected = dao.recent_rejected_plans(
        db, kb["kb_id"], PLANNER_REJECTED_HISTORY_N,
    )

    proposal, preview = _heuristic_proposal(
        db, kb, source_ids, sources_md, related, pages, rejected,
    )
    rationale = _build_rationale(
        kb, source_ids, sources_md, related, len(pages),
    )
    risk = "RELINK/合并动作影响既有页面；执行阶段会做逐动作乐观校验。"
    return proposal, preview, rationale, risk


async def _related_pages(
    db: Database, kb: dict, markdown: str,
) -> list[str]:
    """只读检索初筛（keyword 信号，L1 进程内直调 Runtime 检索面）。"""
    title = (re.search(r"^#\s+(.+)$", markdown or "", re.M) or [None, ""])[1]
    query = (title or (markdown or "")[:120]).strip()
    if not query or not kb.get("current_rev"):
        return []
    try:
        kw, _, _scores = retrieval.keyword_signal(
            MySQLBackend(db), kb["kb_id"], kb["current_rev"], query,
            dao.manifest_of(db, kb["kb_id"], kb["current_rev"]),
        )
    except Exception:  # noqa: BLE001 - 初筛失败不阻塞规划
        return []
    pages = {p["page_id"]: p["rel_path"] for p in
             dao.current_pages(db, kb["kb_id"])}
    return [pages[pid] for pid in kw[:5] if pid in pages]


def _heuristic_proposal(
    db: Database, kb: dict, source_ids: list[str],
    sources_md: dict[str, str], related: dict[str, list[str]],
    pages: list[dict], rejected: list[dict],
) -> tuple[dict, dict]:
    """确定性规划启发式（LLM 未配置时的等价实现；语义对齐 04 §6.2）。"""
    changes: list[dict] = []
    new_pages = []
    relinks = []
    updated_pages = []
    confidence = "high"
    strategies_used: set[str] = set()

    pages_by_path = {p["rel_path"]: p for p in pages}
    for sid in source_ids:
        src = dao.get_source(db, sid)
        md = sources_md.get(sid) or ""
        title_m = re.search(r"^#\s+(.+)$", md, re.M)
        title = (title_m.group(1).strip() if title_m
                 else re.sub(r"\.[^.]+$", "", src["file_name"] if src else sid))
        rel = related.get(sid) or []
        # 判定动作：有高相关页 → 并入/更新；否则新建页
        merge_target = rel[0] if rel else None
        # 驳回学习（S3，启发式侧）：曾驳回「拆分/新建多页」→ 一律单页并入
        no_split = any(
            "拆" in (r.get("confirm_note") or "") for r in rejected
        )
        if merge_target and not _same_title_exists(pages, title):
            strategies_used.add("MERGE_INTO_EXISTING")
            confidence = "medium"
            changes.append({
                "action_id": f"update:{merge_target}",
                "action": "UPDATE_PAGE",
                "target": merge_target,
                "target_hash": pages_by_path[merge_target]["content_hash"]
                if merge_target in pages_by_path else None,
                "depends_on": [],
                "reason": f"与既有页面 {merge_target} 主题相关，并入更新",
                "effect": "追加本源内容要点并标注来源",
                "expected_effect": "目标页包含本源内容，来源清单更新",
                "sources": [sid],
            })
            updated_pages.append({
                "path": merge_target, "change_summary": "并入新源内容要点",
                "diff_hint": "尾部追加 + frontmatter.sources",
            })
        else:
            rel_dir = route_dir_from_schema(
                kb.get("schema_text"), title=title,
                file_name=(src["file_name"] if src else sid),
            )
            path = f"{rel_dir}{_slugify(title)}.md"
            strategies_used.add("NEW_PAGE_AND_UPDATE_INDEX")
            changes.append({
                "action_id": f"create:{path}",
                "action": "CREATE_PAGE",
                "path": path,
                "title": title,
                "sources": [sid],
                "depends_on": [],
                "effect": "新增页面，frontmatter.sources 指向本源",
                "expected_effect": f"新页存在于 {path}，sources 含 {sid}",
            })
            new_pages.append({"path": path, "title": title, "source_ids": [sid]})
            # 同名旧版本检测（如 returns vs returns_v3）→ RELINK + 旧页标注
            old = _similar_version_page(pages, title)
            if old and not no_split:
                old_name = generator._wiki_name(old["rel_path"])
                new_name = generator._wiki_name(path)
                backlinks = dao.backlinks_of(db, kb["kb_id"], old["rel_path"])
                targets = [b for b in backlinks if b in pages_by_path]
                if targets:
                    changes.append({
                        "action_id": f"update:{old['rel_path']}",
                        "action": "UPDATE_PAGE",
                        "target": old["rel_path"],
                        "target_hash": old["content_hash"],
                        "depends_on": [],
                        "reason": "存在新版本页面，旧页标注版本说明",
                        "effect": "加版本说明 note 并指向新页",
                        "expected_effect": "旧页含版本说明",
                    })
                    updated_pages.append({
                        "path": old["rel_path"], "change_summary": "加版本说明",
                        "diff_hint": "尾部 note",
                    })
                    changes.append({
                        "action_id": f"relink:{targets[0]}",
                        "action": "RELINK",
                        "targets": targets[:3],
                        "from": f"[[{old_name}]]",
                        "to": f"[[{new_name}]]",
                        "depends_on": [f"create:{path}"],
                        "reason": "反向链接应指向最新版本",
                        "effect": f"{len(targets[:3])} 处 wikilink 改指新页",
                        "expected_effect": "引用页链接指向新页",
                    })
                    relinks.append({
                        "targets": targets[:3], "from": f"[[{old_name}]]",
                        "to": f"[[{new_name}]]", "count": len(targets[:3]),
                    })
                    confidence = "medium"

    changes.append({
        "action_id": "update_index_log:index.md",
        "action": "UPDATE_INDEX_LOG",
        "targets": ["index.md", "log.md"],
        "depends_on": [],
        "effect": "index/log 由代码维护式更新（不整页重写）",
        "expected_effect": "index 含新页条目，log 追加本次变更",
    })

    strategy = (
        "MERGE_INTO_EXISTING" if "MERGE_INTO_EXISTING" in strategies_used
        and "NEW_PAGE_AND_UPDATE_INDEX" not in strategies_used
        else "NEW_PAGE_AND_UPDATE_INDEX"
    )
    proposal = {
        "kb_id": kb["kb_id"],
        "base_rev": kb.get("current_rev"),
        "trigger": "UPLOAD",
        "sources": source_ids,
        "placement": {
            "proposed_dir": route_dir_from_schema(
                kb.get("schema_text"), title="",
                file_name="",
            ),
            "strategy": strategy,
            "rationale": "按 schema 目录路由 + 相关页检索结果判定放置",
        },
        "changes": changes,
        "lint_after": ["扫描新旧页面矛盾（contradiction）与重复（duplicate）"],
        "confidence": confidence,
        "alternatives": [{
            "strategy": "MERGE_INTO_EXISTING",
            "rationale": "若希望保持单页，可把内容并入相关既有页",
            "tradeoff": "页面变长，来源差异混在同一页",
        }],
        "risk": "结构动作影响既有页面；执行阶段逐动作乐观校验。",
        "rollback": "应用前保留 base_rev；失败可回滚至 base_rev",
    }
    preview = {
        "new_pages": new_pages,
        "updated_pages": updated_pages,
        "deleted_pages": [],
        "relinks": relinks,
    }
    return proposal, preview


def _same_title_exists(pages: list[dict], title: str) -> bool:
    return any(p["title"] == title for p in pages)


def _similar_version_page(pages: list[dict], title: str) -> dict | None:
    """同名版本检测：标题是某页标题 + 版本号（如 v2/v3/2024）。"""
    m = re.search(r"\s+v\d+$|\s+\d{4}$", title)
    if not m:
        return None
    base_title = title[: m.start()].strip()
    for p in pages:
        if p["title"] == base_title:
            return p
    return None


def _conservative_proposal(db: Database, kb: dict, source_ids: list[str]):
    """超时/失败降级：整份文档默认单页入库（04 §6.3）。

    降级的是「结构分析」，不是「标题识别」——读首个一级标题是确定性的纯文本
    操作，不消耗任何预算。此前这里直接拿文件名当标题，导致降级批次的页面标题
    与路径全部退化成 `04-expense-standards` 这类文件名，正文里按人类标题写的
    `[[wikilink]]` 随之全部悬空（图信号归零）。
    """
    changes = []
    new_pages = []
    for sid in source_ids:
        src = dao.get_source(db, sid)
        name = (src["file_name"] if src else sid)
        stem = _title_from_source(db, kb, sid, name)
        path = f"wiki/sources/{_slugify(stem)}.md"
        changes.append({
            "action_id": f"create:{path}",
            "action": "CREATE_PAGE",
            "path": path,
            "title": stem,
            "sources": [sid],
            "depends_on": [],
            "effect": "降级方案：单页入库",
            "expected_effect": f"新页存在于 {path}",
        })
        new_pages.append({"path": path, "title": stem, "source_ids": [sid]})
    changes.append({
        "action_id": "update_index_log:index.md",
        "action": "UPDATE_INDEX_LOG",
        "targets": ["index.md", "log.md"],
        "depends_on": [],
        "effect": "index/log 由代码维护式更新",
        "expected_effect": "index 含新页条目",
    })
    proposal = {
        "kb_id": kb["kb_id"],
        "base_rev": kb.get("current_rev"),
        "trigger": "UPLOAD",
        "sources": source_ids,
        "placement": {
            "proposed_dir": "wiki/sources/",
            "strategy": "NEW_PAGE_AND_UPDATE_INDEX",
            "rationale": "降级方案：未做结构分析，默认单页入库",
        },
        "changes": changes,
        "lint_after": [],
        "confidence": "low",
        "alternatives": [],
        "risk": "未分析既有结构，可能与现有页重复",
        "rollback": "应用前保留 base_rev；失败可回滚至 base_rev",
    }
    preview = {"new_pages": new_pages, "updated_pages": [],
               "deleted_pages": [], "relinks": []}
    return proposal, preview


def _build_rationale(
    kb: dict, source_ids: list[str], sources_md: dict[str, str],
    related: dict[str, list[str]], page_count: int,
) -> str:
    lines = [
        f"知识库当前 {page_count} 页，rev={kb.get('current_rev') or '（空）'}。",
    ]
    for sid in source_ids:
        rel = related.get(sid) or []
        lines.append(
            f"- 源 {sid}: 相关页 {', '.join(rel[:3]) if rel else '无'}",
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 依赖闭包（D4，04 §9.2）：confirm 服务端校验
# ---------------------------------------------------------------------------
def validate_dependency_closure(
    proposal: dict, selected: list[str],
) -> tuple[list[str], list[str]]:
    """返回 (勾选闭包, auto_selected)。

    闭包 = selected ∪ 其 depends_on 传递闭包；引用不存在的 action_id
    视为生成期错误（由调用方 422）。
    """
    changes = proposal.get("changes") or []
    by_id = {c["action_id"]: c for c in changes}
    unknown = [a for a in selected if a not in by_id]
    if unknown:
        raise KeyError(f"action_id 不存在: {unknown}")
    chosen = list(dict.fromkeys(selected))
    auto: list[str] = []
    # 传递闭包（DAG 由 planner 保证；此处防御环）
    queue = list(chosen)
    seen: set[str] = set()
    while queue:
        aid = queue.pop()
        if aid in seen:
            continue
        seen.add(aid)
        for dep in by_id[aid].get("depends_on") or []:
            if dep not in by_id:
                raise KeyError(f"depends_on 引用不存在的 action_id: {dep}")
            if dep not in chosen:
                chosen.append(dep)
                auto.append(dep)
            queue.append(dep)
    return chosen, auto
