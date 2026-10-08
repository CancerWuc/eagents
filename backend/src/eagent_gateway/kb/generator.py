# -*- coding: utf-8 -*-
"""KB 生成模块（03 §3.3/§3.4）：两段式 CoT + 「LLM 永不持整页重写权」。

红线（06 §6 保留项 2）:
1. LLM 只允许「生成新页正文 + 正文内 wikilink」；
2. index.md / log.md 聚合页由代码维护（maintainWikiIndex 语义）；
3. 对已有页不整页重写：UPDATE_PAGE 只做代码注入式追加修订说明；
4. RELINK 只给 from→to 映射，代码做首现替换（enrichWithWikilinks 语义）。

LLM 未配置/失败 → 确定性降级（规则式页面生成），保证管线可用。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from . import llm as kb_llm
from .parser import heading_signals

logger = logging.getLogger("eagent.kb.generate")

INDEX_PATH = "wiki/index.md"
LOG_PATH = "wiki/log.md"

_STEP1_SYSTEM = (
    "你是知识库入库分析器。分析新源文档与既有 Wiki 结构的关系，"
    "输出 JSON: {\"entities\": [..], \"concepts\": [..], "
    "\"contradictions\": [..], \"structure_suggestions\": [..]}。"
    "只输出 JSON，不写盘、不生成正文。"
)

_STEP2_SYSTEM = (
    "你是知识库页面生成器。基于源文档生成一个 Markdown Wiki 页面：\n"
    "- 首行是一级标题\n- 正文中相关概念用 [[wikilink]] 引用\n"
    "- 不编造源文档之外的事实\n- 只输出 Markdown 正文（不含 frontmatter）"
)


async def analyze_source(
    *, source_summary: str, wiki_structure: str, schema_text: str,
) -> dict:
    """Step1 分析（两段式 CoT 第一段）：只产出分析不写盘（03 §3.3）。"""
    text = await kb_llm.chat(
        system=_STEP1_SYSTEM,
        user=(
            f"# 源文档摘要\n{source_summary}\n\n# 既有 Wiki 结构\n"
            f"{wiki_structure}\n\n# schema 规则\n{schema_text or '（空）'}"
        ),
    )
    if text:
        try:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                return json.loads(m.group(0))
        except ValueError:
            logger.warning("Step1 分析输出非 JSON，走确定性分析")
    return _deterministic_analysis(source_summary)


def _deterministic_analysis(source_summary: str) -> dict:
    heads = heading_signals(source_summary)
    return {
        "entities": heads[:5],
        "concepts": [],
        "contradictions": [],
        "structure_suggestions": [],
    }


async def generate_page_body(
    *, title: str, source_markdown: str, purpose_text: str,
) -> str:
    """Step2 生成新页正文；LLM 失败/未配置 → 确定性规则式正文。"""
    text = await kb_llm.chat(
        system=_STEP2_SYSTEM,
        user=(
            f"# 知识库目标\n{purpose_text or '通用客服知识库'}\n\n"
            f"# 页面标题\n{title}\n\n# 源文档内容\n{source_markdown}"
        ),
    )
    if text and text.strip():
        return text.strip()
    return _fallback_body(title, source_markdown)


def _fallback_body(title: str, source_markdown: str) -> str:
    """确定性降级正文：源文档规整为 Wiki 页（保真，不做创造性改写）。"""
    body = source_markdown.strip() or "（源文档为空）"
    if not heading_signals(body):
        body = f"## 概述\n\n{body}"
    return f"# {title}\n\n{body}"


def build_frontmatter(
    *, title: str, source_ids: list[str], tags: list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "sources": source_ids,
        "tags": tags or [],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 聚合页维护（代码维护，LLM 永不整页重写，03 §3.3）
# ---------------------------------------------------------------------------
def build_index_md(pages: list[dict]) -> str:
    lines = ["# 知识库索引", ""]
    for p in sorted(pages, key=lambda x: x["rel_path"]):
        title = p.get("title") or p["rel_path"]
        wikiname = _wiki_name(p["rel_path"])
        lines.append(f"- [[{wikiname}]] — {title}")
    lines += ["", f"_共 {len(pages)} 页；本页由系统维护，请勿手改。_"]
    return "\n".join(lines) + "\n"


def build_log_md(prev_log: str | None, *, rev: str, summary: dict) -> str:
    head = prev_log if prev_log and prev_log.strip() else "# 变更日志\n"
    entry = (
        f"\n- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} "
        f"`{rev}`: 新增 {summary.get('pages_created', 0)} / "
        f"更新 {summary.get('pages_updated', 0)} / "
        f"删除 {summary.get('pages_deleted', 0)} 页，"
        f"源 +{summary.get('sources_added', 0)}"
    )
    return head.rstrip("\n") + "\n" + entry.strip() + "\n"


def _wiki_name(rel_path: str) -> str:
    """wiki/policies/returns.md → returns（wikilink 目标名）。"""
    name = rel_path.rsplit("/", 1)[-1]
    return re.sub(r"\.md$", "", name)


def build_link_index(pages: list[dict]) -> dict[str, str]:
    """wikilink 目标名 → rel_path，同时接受两种写法：

    - 文件名式 ``[[returns]]``（build_index_md 生成的形式，系统内部约定）
    - 标题式 ``[[退货政策]]``（人写文档的直觉形式）

    文件名优先：两者冲突时以文件名为准，避免标题重名把链接指到别处。
    标题重复的（_lint_current 会另行报 duplicate）一律不建映射，宁可让它
    保持断链被 lint 抓到，也不要静默指向任意一页。
    """
    by_name = {_wiki_name(p["rel_path"]): p["rel_path"] for p in pages}

    seen: dict[str, int] = {}
    for p in pages:
        title = (p.get("title") or "").strip()
        if title:
            seen[title] = seen.get(title, 0) + 1

    index = dict(by_name)
    for p in pages:
        title = (p.get("title") or "").strip()
        if not title or seen.get(title, 0) > 1 or title in by_name:
            continue
        index[title] = p["rel_path"]
    return index


# ---------------------------------------------------------------------------
# RELINK（LLM 给映射、代码首现替换，03 §3.4）
# ---------------------------------------------------------------------------
def apply_relink_first_occurrence(body_md: str, from_link: str, to_link: str) -> tuple[str | None, str]:
    """首现替换。返回 (new_body, skip_reason)；无匹配 → (None, 原因)（SKIP 留痕）。"""
    if from_link not in body_md:
        return None, f"正文中未找到待替换链接 {from_link}"
    new_body = body_md.replace(from_link, to_link, 1)
    return new_body, ""


def inject_update_note(body_md: str, *, note: str) -> str:
    """UPDATE_PAGE 代码注入式修订说明（不整页重写，03 §3.3-3）。"""
    return (
        body_md.rstrip("\n")
        + "\n\n> **版本说明**: " + (note or "本页有新版本内容，见关联页面。") + "\n"
    )


# ---------------------------------------------------------------------------
# schema.md / purpose.md 演进（011：空置或 AI 维护中的字段由本模块完善）
# ---------------------------------------------------------------------------
_EVOLVE_SYSTEM = (
    "你是知识库配置维护器。根据知识库信息与实际 Wiki 页面结构，输出 JSON: "
    '{"schema_md": "...", "purpose_md": "..."}。\n'
    "schema_md 每行格式 `- <目录名>: <目录说明> <路由关键词...>`，目录名仅"
    "小写英文/数字/中划线且必须与页面路径中已有的目录一致，不得发明不存在的目录；"
    "purpose_md 用一两句话概括知识库目标与服务对象。只输出 JSON。"
)


async def evolve_kb_config(
    *, kb_name: str, kb_description: str | None,
    current_schema: str | None, current_purpose: str | None,
    pages_summary: str,
) -> dict | None:
    """LLM 生成/完善 schema 与 purpose 草稿；未配置 LLM 返回 None。

    与既有红线一致：LLM 只产出文本草稿，落库由调用方（compile）决定。"""
    text = await kb_llm.chat(
        system=_EVOLVE_SYSTEM,
        user=(
            f"# 知识库\n{kb_name}"
            + (f"：{kb_description}" if kb_description else "")
            + f"\n\n# 当前 schema.md\n{current_schema or '（空）'}"
            + f"\n\n# 当前 purpose.md\n{current_purpose or '（空）'}"
            + f"\n\n# 实际 Wiki 页面（路径 ｜ 标题）\n{pages_summary}"
        ),
    )
    if not text:
        return None
    try:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return {
                    "schema_md": str(data.get("schema_md") or ""),
                    "purpose_md": str(data.get("purpose_md") or ""),
                }
    except ValueError:
        logger.warning("schema/purpose 演进输出非 JSON，走启发式降级")
    return None


def heuristic_schema_purpose(
    pages: list[dict], kb_name: str, kb_description: str | None,
) -> dict:
    """无 LLM 降级：从实际目录结构与页面标题确定性生成（011 兜底）。"""
    dirs: dict[str, list[str]] = {}
    for p in pages:
        parts = p["rel_path"].split("/")
        if len(parts) >= 3 and parts[0] == "wiki":  # wiki/<dir>/<page>.md
            dirs.setdefault(parts[1], []).append(p.get("title") or parts[-1])
    schema_lines = [
        f"- {d}: {'、'.join(titles[:3])}"
        for d, titles in sorted(dirs.items())
    ]
    purpose = (
        f"{kb_name}：整理"
        + ("、".join(sorted(dirs)) if dirs else "上传资料")
        + "相关知识，供检索问答使用。"
    )
    return {
        "schema_md": "\n".join(schema_lines),
        "purpose_md": (kb_description or "").strip() or purpose,
    }
