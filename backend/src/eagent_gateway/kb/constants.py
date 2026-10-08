# -*- coding: utf-8 -*-
"""KB 领域常量与配置默认值（权威口径：from_llm_wiki/02 §3/§4）。

状态枚举、search_config 默认值（D2/D3）、chunking 默认值、
内容指纹算法、中文停用词表（03 §5.2）都在这里集中定义，
全模块禁止散落魔法字符串。
"""
from __future__ import annotations

import hashlib
import json
import re

# ---------------------------------------------------------------------------
# 状态枚举（02 §3 逐表一致）
# ---------------------------------------------------------------------------
KB_STATUSES = ("CREATED", "PARSING", "READY", "STALE", "FAILED", "ARCHIVED")
SOURCE_STATUSES = (
    "PENDING", "PLANNING", "INGESTING", "DONE", "FAILED", "DELETED",
)
REV_STATUSES = ("BUILDING", "READY", "FAILED")
PAGE_REV = None  # 页面无独立状态机（embedded/error 列承载）
PLAN_STATUSES = (
    "DRAFT", "CONFIRMED", "REJECTED", "SUPERSEDED", "APPLIED",
    "APPLIED_WITH_SKIPS",
)
TASK_KINDS = ("INGEST", "REBUILD", "EMBED")
TASK_STATUSES = ("PENDING", "RUNNING", "DONE", "FAILED", "CANCELED")
REVIEW_TYPES = (
    "contradiction", "duplicate", "missing_page", "suggestion",
)
REVIEW_STATUSES = ("OPEN", "RESOLVED", "DISMISSED")
CLASSIFICATIONS = ("PUBLIC", "INTERNAL", "CONFIDENTIAL")
BINDING_TARGET_TYPES = ("MASTER", "AGENT")

PLANNER_IDENTITY = "service:kb-planner"

# ---------------------------------------------------------------------------
# 默认配置（02 §4.2 search_config / §4.1 chunking）
# ---------------------------------------------------------------------------
DEFAULT_SEARCH_CONFIG: dict = {
    # top_k = 最终结果窗口 = 下游上下文预算（llm-wiki DEFAULT_RESULTS=20/MAX=50）
    "top_k_default": 20,
    "top_k_max": 50,
    # D3：聚合字节预算 + 单条 snippet 截断
    "aggregate_budget_bytes": 32768,
    "snippet_max_bytes": 2048,
    # D3：相关度地板。rrf_floor 按位次算，最低可达分 1/(60+19)≈0.0127，
    # 恒大于 0.005 —— 该阈值不可达，等同关闭，保留仅为兼容既有配置 JSON。
    # 真正生效的是 keyword_floor：keyword_signal 的原始相关度分（见
    # _score_page 打分公式），与位次无关，可阈值化。
    # 默认 6.0 依据两个真实库实测（IDF 加权后的量级）：
    #   IT 工单库：该答 7.2~74.2 / 不该答 0~7.1
    #   售后库  ：该答 10.5~31.2 / 不该答 1.2~4.7
    # 取 6.0 保证零误拒，代价是极少数擦边无关查询会漏进来（由 purpose.md
    # 的「无法确证则明确告知」兜底）。宁可漏过也不误拒：误拒会让明明有
    # 资料的问题被拒答，比多给一点弱相关上下文更糟。
    # **每个库上线前应按自己的语料重新标定**（检索测试页会显示实时分值）。
    "no_hit": {"min_results": 2, "rrf_floor": 0.005, "keyword_floor": 6.0},
    # S6/llm-wiki MAX_TEXT_BYTES=120_000
    "read_page_budget_bytes": 120000,
    # D-2026-08-16-02 追加模式：graph 邻居 ≤max 比例且仅填充剩余窗口
    # （min 字段保留以兼容既有配置 JSON，两信号下不再使用）
    "graph_quota": {"min": 0.15, "max": 0.30},
}

DEFAULT_CHUNKING_CONFIG: dict = {
    "max_tokens": 512,
    "overlap": 48,
    "title_hierarchy": True,
}

# embedding_config 默认（**已废弃**，D-2026-08-16-02 去向量化，08 §3.4：
# 检索/编译不再调用 embedder；字段与表保留仅为兼容既有配置 JSON）
DEFAULT_EMBEDDING_CONFIG: dict = {
    "provider": "lancedb",
    "model": "builtin-hash-512",
    "dimension": 512,
    "endpoint": None,
}

# 检索常量（08 §3，D-2026-08-16-02 两信号规格；vector 常量已随去向量化删除）
RRF_K = 60
GRAPH_SEED_TOP_N = 20
MAX_SEARCH_FILES = 10_000          # 候选池保护（llm-wiki MAX_SEARCH_FILES）
SEARCH_WALL_CLOCK_BUDGET_S = 3.0   # 整条检索链路预算（fail-closed）

# 打分公式（llm-wiki search.rs:818-883，逐条照搬）
SCORE_FILENAME_EXACT = 200
SCORE_PHRASE_IN_TITLE = 50
SCORE_CONTENT_PHRASE = 20
SCORE_CONTENT_PHRASE_CAP = 10      # 次数上限（最高 200）
SCORE_TITLE_TOKEN = 5.0
SCORE_CONTENT_TOKEN = 1.0
# 中文单字权重（相对 bigram / ascii 词）。单字几乎不承载语义——「申」「操」
# 「作」单独出现毫无信息量，却因为在长文档里频繁出现而压过真正的关键词。
# 语义主要在 bigram（申请/操作）与 ascii 词（vpn/api）上。
CJK_UNIGRAM_WEIGHT = 0.25

# 编译任务（B4）
TASK_MAX_RETRY = 3

# 入库规划（04）
PLANNER_DEBOUNCE_S = 30.0          # D7：30s 防抖合并窗口
PLANNER_BUDGET_S = 120.0           # 预算：工具轮次 / 120s wall-clock
PLANNER_MAX_TOOL_ROUNDS = 8        # 轮次下限（小批次沿用原值）
# 轮次按批次规模伸缩（每源一轮 + 余量），此为硬顶防失控。
# 固定 8 轮时 >8 份文档的批次必然整批降级，见 planner.run_plan_for_sources。
PLANNER_MAX_TOOL_ROUNDS_HARD_CAP = 128
PLANNER_REJECTED_HISTORY_N = 5     # S3：最近 5 条驳回原因注入 prompt

# Rev 保留窗口（03 §4.3）
REV_RETAIN_WINDOW = 5

# ---------------------------------------------------------------------------
# 中文停用词（03 §5.2：吸收 llm-wiki 中文经验，按业务扩充）
# ---------------------------------------------------------------------------
CJK_STOPWORDS = frozenset(
    "的了吗呢是在和与及或对把被就都还也很最更这那有无不没请"
    "什么怎么为什么是否可以如何你我他她它我们你们他们吗吧啊呀"
    "一二三四五六七八九十的个之其为此于以则个等并或者以及",
)

_WIKILINK_RE = re.compile(r"\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")


def extract_wikilinks(body_md: str) -> list[str]:
    """提取正文中 `[[target]]` / `[[target|alias]]` 的 target 列表（去重保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(body_md or ""):
        t = m.group(1).strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_fingerprint(embedding_config: dict) -> str:
    """embedding 配置指纹（B5/S8）：provider/model/dimension/endpoint 任一变化即变。"""
    return sha256_hex(canonical_json(embedding_config or {}))


def page_content_hash(frontmatter: dict | None, body_md: str) -> str:
    """页面内容指纹 = sha256(frontmatter 规范化 JSON + body)（02 §3 表 3）。"""
    return sha256_hex(canonical_json(frontmatter or {}) + "\n" + (body_md or ""))


# 需要按键深合并的嵌套配置：整块覆盖会让既有 KB 永久缺失后加的子键
_NESTED_SEARCH_KEYS = ("no_hit", "graph_quota")


def normalize_search_config(raw: dict | None) -> dict:
    """合并用户配置与默认值（应用层解析，JSON 列不建索引 B1）。

    嵌套字典按键深合并：老 KB 存的 no_hit 只有 min_results/rrf_floor，
    浅合并会整块替换掉默认值，导致新增的 keyword_floor 永远取不到。
    """
    cfg = dict(DEFAULT_SEARCH_CONFIG)
    if raw:
        for k, v in raw.items():
            if k not in cfg or v is None:
                continue
            if k in _NESTED_SEARCH_KEYS and isinstance(v, dict):
                merged = dict(cfg[k])
                merged.update({ik: iv for ik, iv in v.items() if iv is not None})
                cfg[k] = merged
            else:
                cfg[k] = v
    return cfg


def normalize_chunking_config(raw: dict | None) -> dict:
    cfg = dict(DEFAULT_CHUNKING_CONFIG)
    if raw:
        for k, v in raw.items():
            if k in cfg and v is not None:
                cfg[k] = v
    return cfg


def normalize_embedding_config(raw: dict | None) -> dict:
    cfg = dict(DEFAULT_EMBEDDING_CONFIG)
    if raw:
        for k in ("provider", "model", "dimension", "endpoint"):
            if raw.get(k) is not None:
                cfg[k] = raw[k]
    return cfg
