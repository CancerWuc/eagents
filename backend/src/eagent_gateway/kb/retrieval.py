# -*- coding: utf-8 -*-
"""KB Runtime 检索引擎（D-2026-08-16-02 两信号规格，08 §3 权威）。

**去向量化**（08 方案覆盖 03 §5 旧三信号描述）：
- keyword: manifest 驱动 glob 定位落盘文件 + grep 读全文 + llm-wiki 打分
  （200/50/20×10/5.0/1.0，CJK bigram+单字+停用词 tokenize）；
  落盘缺失时回退 DB body_md（对账任务会补齐文件，07 §4.3）。
- graph:  邻接表对称一跳，种子 = keyword 命中 top-20；**追加模式**
  （keyword 命中优先，邻居 ≤30% 上限且仅在有剩余窗口时填充，不挤占 grep 结果）。
- RRF:   两路 K=60，score = Σ 1/(60+rank)；窗口 DEFAULT=20/MAX=50；
         聚合字节预算 32768 + no_reliable_hit（min_results/rrf_floor）。
- 无向量信号（VECTOR_SIM_FLOOR 已随 D-2026-08-16-02 删除）。
- S6 read_page 路径安全、D5 <kb_snippet> 包裹 + redact、fail-closed 不变。

**数据源适配（standalone-deployment 01 §5，S-01）**：本模块只依赖
``SearchBackend``（平台 MySQLBackend / 容器 SnapshotBackend），不直接触
dao/Database——检索核心 glob+grep 打分、graph 追加模式、RRF、窗口、聚合
预算、no_reliable_hit、<kb_snippet> 包裹对两种数据源完全同一套逻辑。
"""
from __future__ import annotations

import logging
import math
import posixpath
import time

from ..errors import ValidationError
from ..security.redact import redact_text
from .backends import SearchBackend, search_config_of
from .constants import (
    CJK_STOPWORDS,
    CJK_UNIGRAM_WEIGHT,
    DEFAULT_SEARCH_CONFIG,
    GRAPH_SEED_TOP_N,
    MAX_SEARCH_FILES,
    RRF_K,
    SCORE_CONTENT_PHRASE,
    SCORE_CONTENT_PHRASE_CAP,
    SCORE_CONTENT_TOKEN,
    SCORE_FILENAME_EXACT,
    SCORE_PHRASE_IN_TITLE,
    SCORE_TITLE_TOKEN,
    SEARCH_WALL_CLOCK_BUDGET_S,
)

logger = logging.getLogger("eagent.kb.search")


class KBUnavailableError(RuntimeError):
    """fail-closed：KB 无可用 Rev / 检索链路超时（不吞成普通文本）。"""


# ---------------------------------------------------------------------------
# S6 路径安全
# ---------------------------------------------------------------------------
def normalize_wiki_path(path: str) -> str:
    """归一化 + 限定 wiki/ 命名空间；拒绝 ../、绝对路径、非 wiki/ 前缀。"""
    raw = (path or "").strip()
    if not raw:
        raise ValidationError("缺少 path 参数")
    if "\\" in raw or "\x00" in raw:
        raise ValidationError("非法路径")
    norm = posixpath.normpath(raw)
    if norm.startswith("/") or norm.startswith("..") or "/../" in norm:
        raise ValidationError("路径穿越被拒绝")
    if not norm.startswith("wiki/"):
        raise ValidationError("只允许访问 wiki/ 命名空间")
    if len(norm) > 500:
        raise ValidationError("路径过长")
    return norm


def resolve_rev(backend: SearchBackend, kb: dict, rev: str | None) -> str:
    """rev=current → current_rev；对应 Rev 非 READY/为空 → KB_UNAVAILABLE。"""
    if rev and rev != "current":
        if not backend.rev_ready(kb["kb_id"], rev):
            raise KBUnavailableError(f"Rev 不可用: {rev}")
        return rev
    current = kb.get("current_rev")
    if not current:
        raise KBUnavailableError("KB 尚无可用版本（current_rev 为空）")
    if not backend.rev_ready(kb["kb_id"], current):
        raise KBUnavailableError("KB 当前版本不可用")
    return current


# ---------------------------------------------------------------------------
# tokenizer（ngram bigram + 单字，去停用词；llm-wiki search.rs:885-987 等价）
# ---------------------------------------------------------------------------
def query_tokens(query: str) -> tuple[list[str], list[str]]:
    """返回 (cjk 单字+bigram, ascii 词)。

    bigram 必须在**原文连续 CJK 段**内组合，不能先删停用词再拼接：
    「我想申请vpn如何操作」若先删掉停用词「我/请/如/何」，剩下的
    「想申操作」会拼出「申操」这种跨越删除位置的伪词，而真正的关键词
    「申请」反而因为「请」是停用词而永远组不出来。
    做法：bigram 取自原文连续段（保留停用词参与组合），单字仍按停用词
    过滤；两个字都是停用词的 bigram（如「如何」）丢弃。
    """
    runs: list[list[str]] = []
    cur_run: list[str] = []
    for ch in query:
        if 0x4E00 <= ord(ch) <= 0x9FFF:
            cur_run.append(ch)
        elif cur_run:
            runs.append(cur_run)
            cur_run = []
    if cur_run:
        runs.append(cur_run)

    cjk = [ch for run in runs for ch in run if ch not in CJK_STOPWORDS]
    bigrams = [
        run[k] + run[k + 1]
        for run in runs for k in range(len(run) - 1)
        # 纯停用词组合（「如何」「什么」）不承载语义，丢弃
        if not (run[k] in CJK_STOPWORDS and run[k + 1] in CJK_STOPWORDS)
    ]
    words = []
    cur = []
    for ch in query.lower():
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            cur.append(ch)
        else:
            if cur:
                words.append("".join(cur))
                cur = []
    if cur:
        words.append("".join(cur))
    return cjk + bigrams, words


# ---------------------------------------------------------------------------
# keyword 信号（08 §3.1：manifest 驱动 glob + grep 读全文 + 打分）
# ---------------------------------------------------------------------------
def keyword_signal(
    backend: SearchBackend, kb_id: str, rev: str, query: str,
    manifest: dict[str, str],
) -> tuple[list[str], int, dict[str, float]]:
    """glob 定位落盘文件 → 读全文 → llm-wiki 打分；
    返回 (rank list, 候选数, page_id → 原始相关度分)。

    第三项是 no_reliable_hit 判定的依据：RRF 融合分只反映位次，
    无关查询照样能排到第 0 位拿满分，无法阈值化（见 constants 注释）。

    - 候选范围 = 当前 Rev manifest 关联的页面（天然排除历史版本与孤儿文件）；
    - 正文读 backend.read_wiki_body（平台=<root>/<kb_id>/wiki/<rel_path>，
      容器=快照内 wiki/；grep 语义）；平台侧文件缺失回退 DB body_md
      （可检索性优先，缺失文件由对账任务修复并审计）；
    - MAX_SEARCH_FILES=10_000 候选上限（防目录爆炸，llm-wiki 语义）。
    """
    pages = backend.manifest_pages(kb_id, rev)[:MAX_SEARCH_FILES]
    # 正文只读一遍：IDF 需要全候选集的词频统计，逐页独立打分做不到
    docs = []
    for page in pages:
        body = backend.read_wiki_body(kb_id, page["rel_path"])
        if body is None:
            body = page.get("body_md") or ""
            logger.warning(
                "wiki 落盘缺失，keyword 回退 DB 正文: kb=%s page=%s"
                "（对账任务会重建文件）", kb_id, page["rel_path"],
            )
        docs.append((page, body))

    cjk_tokens, word_tokens = query_tokens((query or "").strip().lower())
    idf = _token_idf(docs, cjk_tokens + word_tokens)

    scored = []
    for page, body in docs:
        score = _score_page(
            page["rel_path"], page.get("title") or "", body, query, idf=idf,
        )
        if score > 0:
            scored.append((score, page["page_id"]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return (
        [pid for _, pid in scored],
        len(pages),
        {pid: sc for sc, pid in scored},
    )


def _token_idf(docs, tokens) -> dict[str, float]:
    """词的区分度权重（BM25 风格 IDF）。

    原公式对所有词元一视同仁（每次出现 +1），导致「作」「申」这类几乎每页
    都有的字，靠出现次数压过「vpn」这种只在一两页出现的关键词——问 VPN
    却排出报修流程。IDF 让普遍词趋近 0、独有词权重最高。
    """
    n = len(docs) or 1
    lowered = [
        ((p.get("title") or "").lower(), (b or "").lower()) for p, b in docs
    ]
    idf: dict[str, float] = {}
    for tok in set(t for t in tokens if t):
        df = sum(1 for tl, bl in lowered if tok in bl or tok in tl)
        idf[tok] = math.log((n - df + 0.5) / (df + 0.5) + 1.0)
    return idf


def _score_page(rel_path: str, title: str, body: str, query: str,
                *, idf: dict[str, float] | None = None) -> float:
    """相关度打分（源自 llm-wiki search.rs:818-883，08 §3.1，含两处偏离）。

    与 llm-wiki 基线的差异，均为修复实测暴露的排序错误：
    1. **大小写不敏感**：query_tokens 的 ascii 词元恒为小写，拿原文比对会
       让用户输入的 `vpn` 永远匹配不到正文里的 `VPN`（英文缩写 0 分）。
    2. **IDF 加权 + 单字降权**：原公式按出现次数线性累加，长文档里的通用
       字（作/的/为）会淹没真正的关键词；中文单字本身几乎不承载语义，
       语义主要在 bigram 与 ascii 词上，故单字按 CJK_UNIGRAM_WEIGHT 降权。

    idf 为 None 时退化为全 1.0（单页打分/单元测试路径）。
    """
    q = (query or "").strip().lower()
    if not q:
        return 0.0
    title_l = (title or "").lower()
    body_l = (body or "").lower()
    score = 0.0
    # filename 精确匹配 200
    fname = posixpath.basename(rel_path or "").lower()
    stem = fname[:-3] if fname.endswith(".md") else fname
    if q == stem or q == fname:
        score += SCORE_FILENAME_EXACT
    # phrase-in-title 50
    if q in title_l:
        score += SCORE_PHRASE_IN_TITLE
    # content phrase ×20，上限 10 次
    hits = body_l.count(q)
    score += SCORE_CONTENT_PHRASE * min(hits, SCORE_CONTENT_PHRASE_CAP)
    # token 打分：title ×5 / content ×1，均乘 IDF；CJK 单字额外降权
    cjk_tokens, word_tokens = query_tokens(q)
    for tok in cjk_tokens + word_tokens:
        if not tok:
            continue
        w = (idf or {}).get(tok, 1.0)
        if len(tok) == 1 and 0x4E00 <= ord(tok) <= 0x9FFF:
            w *= CJK_UNIGRAM_WEIGHT
        if tok in title_l:
            score += SCORE_TITLE_TOKEN * w
        score += body_l.count(tok) * SCORE_CONTENT_TOKEN * w
    return round(score, 2)


# ---------------------------------------------------------------------------
# graph 信号（08 §3.2：对称一跳 + 种子=keyword top-20；追加模式见 build_window）
# ---------------------------------------------------------------------------
def seed_paths(backend: SearchBackend, kb_id: str, page_ids: set[str]) -> set[str]:
    if not page_ids:
        return set()
    rows = backend.page_id_path_rows(kb_id)
    return {r["rel_path"] for r in rows if r["page_id"] in page_ids}


def graph_neighbors(
    backend: SearchBackend, kb_id: str, manifest: dict[str, str],
    kw_list: list[str],
) -> list[str]:
    """graph 候选（page_id rank list）：种子 = keyword 命中 top-20 的邻居，
    排除已在 keyword 命中的页（追加模式只补新页），按被种子命中数降序。"""
    seeds = kw_list[:GRAPH_SEED_TOP_N]
    if not seeds:
        return []
    adj = backend.neighbors_of(kb_id, seed_paths(backend, kb_id, set(seeds)))
    support: dict[str, int] = {}
    for nbrs in adj.values():
        for n in nbrs:
            support[n] = support.get(n, 0) + 1
    path_to_pid = _path_to_pid(backend, kb_id, manifest)
    exclude = set(kw_list)
    ranked = sorted(support.items(), key=lambda t: (-t[1], t[0]))
    out = []
    for path, _sup in ranked:
        pid = path_to_pid.get(path)
        if pid and pid not in exclude:
            out.append(pid)
    return out


def _path_to_pid(
    backend: SearchBackend, kb_id: str, manifest: dict[str, str],
) -> dict[str, str]:
    rows = backend.page_id_path_rows(kb_id)
    return {r["rel_path"]: r["page_id"] for r in rows
            if r["page_id"] in manifest}


# ---------------------------------------------------------------------------
# RRF 融合（K=60，两路）
# ---------------------------------------------------------------------------
def rrf_merge(
    ranked_lists: list[list[str]], k: int = RRF_K,
) -> list[tuple[str, float]]:
    """score = Σ 1/(k+rank)；缺席路不贡献。返回按得分降序。"""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, pid in enumerate(lst):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda t: (-t[1], t[0]))


def build_window(
    kw_list: list[str], gra_list: list[str], *, limit: int,
    quota_max: float,
) -> tuple[list[str], int]:
    """追加模式窗口（08 §3.2 拍板 §9-1）：

    keyword 命中全量进入窗口（受 limit 约束），graph 邻居按 ≤30% 上限
    且仅在有剩余窗口时填充——**不挤占 grep 结果**（业务测试基线为纯
    grep/glob）。返回 (窗口 page_id 序, graph 条目数)。
    """
    window = kw_list[:limit]
    g_cap = min(math.floor(limit * quota_max), limit - len(window))
    g_count = 0
    for pid in gra_list:
        if g_cap <= 0 or len(window) >= limit:
            break
        window.append(pid)
        g_cap -= 1
        g_count += 1
    return window, g_count


# ---------------------------------------------------------------------------
# 主入口（两信号执行序列）
# ---------------------------------------------------------------------------
async def search_kb(
    backend: SearchBackend, kb_id: str, query: str, *,
    rev: str | None = "current",
    top_k_override: int | None = None,
    signals_override: list[str] | None = None,
) -> dict:
    """Runtime 检索主流程（契约 v2：keyword_hits/graph_hits，无 vector）。

    top_k/signals 仅管理面预览可显式传（D2 不变）；signals 仅
    keyword/graph 有效（08 §3.5）。
    """
    t0 = time.monotonic()
    kb = backend.kb_row(kb_id)
    if not kb:
        raise ValidationError("KB 不存在")
    cfg = search_config_of(kb)
    cur_rev = resolve_rev(backend, kb, rev)
    manifest = backend.manifest_of(kb_id, cur_rev)
    if not manifest:
        return _empty_result(cur_rev, cfg)

    top_k = top_k_override or cfg["top_k_default"]
    top_k = max(1, min(int(top_k), cfg["top_k_max"]))
    signals = set(signals_override or ["keyword", "graph"])
    signals &= {"keyword", "graph"}

    kw_list: list[str] = []
    if "keyword" in signals:
        kw_list, _, kw_scores = keyword_signal(
            backend, kb_id, cur_rev, query, manifest,
        )
    _budget_check(t0)

    gra_list: list[str] = []
    if "graph" in signals:
        gra_list = graph_neighbors(backend, kb_id, manifest, kw_list)
    _budget_check(t0)

    fused = dict(rrf_merge([kw_list, gra_list]))
    window, graph_count = build_window(
        kw_list, gra_list, limit=top_k, quota_max=cfg["graph_quota"]["max"],
    )
    _budget_check(t0)

    # 聚合字节预算 + snippet 截断 + 包裹 + 脱敏（D3/D5 不变）
    budget = cfg["aggregate_budget_bytes"]
    snippet_max = cfg["snippet_max_bytes"]
    pages_by_id = {p["page_id"]: p for p in backend.manifest_pages(kb_id, cur_rev)}
    src_map = backend.source_path_map(kb_id)
    results = []
    used = 0
    kw_set = set(kw_list)
    for pid in window:
        page = pages_by_id.get(pid)
        if not page:
            continue
        chunks = backend.chunks_of_page(pid)
        fm = page.get("frontmatter_json") or {}
        sources = [
            src_map.get(sid, sid) for sid in (fm.get("sources") or [])
        ]
        snippet = _wrap(
            _best_snippet(query, page, chunks), page["rel_path"], snippet_max,
        )
        entry_bytes = len(snippet.encode("utf-8"))
        if used + entry_bytes > budget:
            break
        used += entry_bytes
        results.append({
            "page_id": pid,
            "path": page["rel_path"],
            "title": page["title"],
            "score": round(fused.get(pid, 0.0), 4),
            "snippet": snippet,
            "chunks": [
                {"chunk_id": c["chunk_id"],
                 "text": _wrap(c["text_md"], page["rel_path"], snippet_max)}
                for c in chunks[:3]
            ],
            "sources": sources,
            "graph_hops": _hops(backend, kb_id, page["rel_path"]),
        })

    no_hit_cfg = cfg["no_hit"]
    # 相关度地板：以首位结果的 keyword 原始分为准（位次分不可阈值化）。
    # 老 KB 的 no_hit 可能没有该键，取默认值兜底。
    kw_floor = float(no_hit_cfg.get(
        "keyword_floor", DEFAULT_SEARCH_CONFIG["no_hit"]["keyword_floor"],
    ))
    top_kw_score = kw_scores.get(results[0]["page_id"], 0.0) if results else 0.0
    no_reliable_hit = (
        not results
        or len(results) < int(no_hit_cfg["min_results"])
        or (results[0]["score"] < float(no_hit_cfg["rrf_floor"]))
        or (top_kw_score < kw_floor)
    )
    return {
        "rev": cur_rev,
        "window": {"limit": top_k, "used_bytes": used,
                   "budget_bytes": budget},
        "no_reliable_hit": bool(no_reliable_hit),
        "results": results,
        "keyword_hits": len(kw_list),
        "graph_hits": graph_count,
        # 调参/排障用：首位结果的 keyword 原始分与当前地板
        "top_keyword_score": round(top_kw_score, 1),
        "keyword_floor": kw_floor,
    }


def _budget_check(t0: float) -> None:
    if time.monotonic() - t0 > SEARCH_WALL_CLOCK_BUDGET_S:
        raise KBUnavailableError("检索链路超过 wall-clock 预算")


def _hops(backend: SearchBackend, kb_id: str, rel_path: str) -> list[str]:
    adj = backend.neighbors_of(kb_id, {rel_path})
    return sorted(adj.get(rel_path, set()))[:5]


def _best_snippet(query: str, page: dict, chunks: list[dict]) -> str:
    """单条 snippet：优先含 query 短语的 chunk，否则首个 chunk / 正文头。"""
    q = (query or "").strip()
    for c in chunks:
        if q and q in c["text_md"]:
            return c["text_md"]
    if chunks:
        return chunks[0]["text_md"]
    return (page.get("body_md") or "")[:512]


def _wrap(text: str, source: str, max_bytes: int) -> str:
    """D5 包裹 + 截断 + 脱敏（服务端兜底；工具面再叠一层）。"""
    safe = redact_text(text or "")
    raw = len(safe.encode("utf-8"))
    if raw > max_bytes:
        # 按字节截断（不撕裂多字节字符）
        enc = safe.encode("utf-8")[:max_bytes]
        safe = enc.decode("utf-8", errors="ignore")
    return f'<kb_snippet source="{source}">{safe}</kb_snippet>'


def _empty_result(rev: str, cfg: dict) -> dict:
    return {
        "rev": rev,
        "window": {"limit": cfg["top_k_default"], "used_bytes": 0,
                   "budget_bytes": cfg["aggregate_budget_bytes"]},
        "no_reliable_hit": True,
        "results": [],
        "keyword_hits": 0, "graph_hits": 0,
    }


# ---------------------------------------------------------------------------
# read_page（S6，不变）
# ---------------------------------------------------------------------------
def read_page(
    backend: SearchBackend, kb_id: str, raw_path: str, *,
    rev: str | None = "current", budget_bytes: int | None = None,
) -> dict:
    path = normalize_wiki_path(raw_path)
    kb = backend.kb_row(kb_id)
    if not kb:
        raise ValidationError("KB 不存在")
    cfg = search_config_of(kb)
    budget = budget_bytes or cfg["read_page_budget_bytes"]
    cur_rev = resolve_rev(backend, kb, rev)
    manifest = backend.manifest_of(kb_id, cur_rev)
    page = backend.page_by_path(kb_id, path)
    if not page or page["page_id"] not in manifest:
        raise ValidationError(f"页面不存在: {path}")
    body = page["body_md"] or ""
    data = body.encode("utf-8")
    truncated = len(data) > budget
    if truncated:
        body = data[:budget].decode("utf-8", errors="ignore")
    return {
        "path": path, "title": page["title"], "rev": cur_rev,
        "body_md": redact_text(body), "bytes": min(len(data), budget),
        "truncated": truncated,
    }
