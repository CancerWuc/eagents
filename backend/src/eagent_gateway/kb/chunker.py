# -*- coding: utf-8 -*-
"""KB 分块器（03 §3.1 chunk 段）。

token-aware 贪心合并（参考 RAGFlow naive_merge 语义）+ 标题层级可选
（title_hierarchy=True 时块带所属标题前缀，参考 TitleChunker）；
不复制其代码。token 估算：CJK 按字计，ASCII 按词 ×1.3。
"""
from __future__ import annotations

import re

from .constants import sha256_hex

_PARA_SPLIT = re.compile(r"\n\s*\n")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.M)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = 0
    ascii_words = 0
    in_word = False
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
            in_word = False
        elif ch.isascii() and (ch.isalnum() or ch == "_"):
            if not in_word:
                ascii_words += 1
                in_word = True
        else:
            in_word = False
    return max(1, round(cjk + ascii_words * 1.3))


def _split_sections(md: str) -> list[tuple[str, str]]:
    """按标题切节：[(heading_prefix, text)]；标题前缀含层级路径。"""
    matches = list(_HEAD_RE.finditer(md))
    sections: list[tuple[str, str]] = []
    if not matches:
        return [("", md)]
    if matches[0].start() > 0:
        sections.append(("", md[: matches[0].start()]))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        level = len(m.group(1))
        title = m.group(2).strip()
        # 正文从标题行**之后**切起：head 已经承载标题，若这里再从 m.start()
        # 切就会把标题行包进正文，chunk_markdown 的 f"{head}\n{text}" 再前置
        # 一次，每个块的标题都印两遍、白吃检索预算。
        sections.append((f"{'#' * min(level, 6)} {title}", md[m.end():end]))
    return sections


def chunk_markdown(
    md: str, *, max_tokens: int = 512, overlap: int = 48,
    title_hierarchy: bool = True,
) -> list[dict]:
    """返回 [{chunk_index, text_md, token_count, content_hash}]。"""
    md = (md or "").strip()
    if not md:
        return []
    pieces: list[str] = []
    for head, text in _split_sections(md):
        text = text.strip()
        if not text:
            continue
        if estimate_tokens(text) <= max_tokens:
            pieces.append(f"{head}\n{text}" if head and title_hierarchy else text)
            continue
        # 长节按段落贪心合并；超限段落按句子硬切
        for para in _PARA_SPLIT.split(text):
            para = para.strip()
            if not para:
                continue
            if estimate_tokens(para) <= max_tokens:
                pieces.append(f"{head}\n{para}" if head and title_hierarchy else para)
            else:
                pieces.extend(_hard_split(para, max_tokens, head if title_hierarchy else ""))
    # 贪心合并到 max_tokens，保留 overlap 个 token 的尾部重叠
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        combined = f"{buf}\n\n{piece}" if buf else piece
        if estimate_tokens(combined) <= max_tokens or not buf:
            buf = combined
        else:
            chunks.append(buf)
            tail = _tail_overlap(buf, overlap)
            buf = f"{tail}\n\n{piece}" if tail else piece
    if buf:
        chunks.append(buf)
    out = []
    for idx, text in enumerate(chunks):
        out.append({
            "chunk_index": idx,
            "text_md": text,
            "token_count": estimate_tokens(text),
            "content_hash": sha256_hex(text),
        })
    return out


def _hard_split(text: str, max_tokens: int, head: str) -> list[str]:
    parts: list[str] = []
    cur = ""
    for sent in re.split(r"(?<=[。！？!?；;\n])", text):
        if not sent.strip():
            continue
        cand = f"{cur}{sent}" if cur else (f"{head}\n{sent}" if head else sent)
        if estimate_tokens(cand) <= max_tokens or not cur:
            cur = cand
        else:
            parts.append(cur)
            cur = (f"{head}\n{sent}" if head else sent)
    if cur:
        parts.append(cur)
    return parts


def _tail_overlap(text: str, overlap_tokens: int) -> str:
    if overlap_tokens <= 0:
        return ""
    if estimate_tokens(text) <= overlap_tokens:
        return text
    lo, hi = 0, len(text)
    # 二分取尾部约 overlap_tokens 的文本
    while lo < hi:
        mid = (lo + hi) // 2
        if estimate_tokens(text[mid:]) > overlap_tokens:
            lo = mid + 1
        else:
            hi = mid
    return text[lo:].lstrip()
