# -*- coding: utf-8 -*-
"""DEV-07: 文本生成指标（BLEU / ROUGE-1/2/L / token-F1）。

- 纯标准库实现，无 nltk / rouge-score 依赖；公式与公开基准对齐
  （验收口径: 与 nltk sentence_bleu(weights=[.25]*4)、rouge-score 的
  rougeN/rougeL 抽样对齐误差 ≤ 1e-6，单测用手工计算参考值锁定）。
- 中文预处理: CJK 逐字切分 + 拉丁字母/数字按词切分 + 小写化。这是确定性
  token 流（非分词器），对 BLEU/ROUGE 这类集合/序列运算是合法口径；
  参考与假设两侧用同一套 tokenize，自洽。
- 空文本（参考或假设为空）一律返回 0.0，不抛错——回放超时的用例没有
  输出，应记 0 分而非中断整批评测。
"""
from __future__ import annotations

import math
import re
from collections import Counter

# CJK 基本区 + 扩展 A；拉丁词/数字连续段作为一个 token
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]|[a-z0-9]+")


def tokenize(text: str | None) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _ngrams(tokens: list[str], n: int) -> Counter:
    return Counter(
        tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)
    )


def _clipped_matches(hyp_ng: Counter, ref_ng: Counter) -> int:
    return sum(min(c, ref_ng.get(g, 0)) for g, c in hyp_ng.items())


def bleu(reference: str, hypothesis: str, max_n: int = 4) -> float:
    """nltk sentence_bleu(weights=[0.25]*4) 口径（无平滑，method0）。

    任一阶 n-gram 精确匹配为 0（或该阶假设侧无 n-gram）时整体为 0；
    短句惩罚 BP = exp(1 - len(ref)/len(hyp))（长度不短于参考时为 1）。
    """
    ref, hyp = tokenize(reference), tokenize(hypothesis)
    if not ref or not hyp:
        return 0.0
    log_sum = 0.0
    for n in range(1, max_n + 1):
        hng = _ngrams(hyp, n)
        total = sum(hng.values())
        if total == 0:
            return 0.0
        match = _clipped_matches(hng, _ngrams(ref, n))
        if match == 0:
            return 0.0
        log_sum += math.log(match / total)
    bp = 1.0 if len(hyp) >= len(ref) else math.exp(1 - len(ref) / len(hyp))
    return bp * math.exp(log_sum / max_n)


def rouge_n(reference: str, hypothesis: str, n: int = 1) -> float:
    """rouge-score 库 rougeN 口径: n-gram P/R 的调和平均（fmeasure）。"""
    ref, hyp = tokenize(reference), tokenize(hypothesis)
    rng, hng = _ngrams(ref, n), _ngrams(hyp, n)
    ref_total, hyp_total = sum(rng.values()), sum(hng.values())
    if not ref_total or not hyp_total:
        return 0.0
    match = _clipped_matches(hng, rng)
    p, r = match / hyp_total, match / ref_total
    return 2 * p * r / (p + r) if p + r else 0.0


def _lcs_len(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            cur[j] = prev[j - 1] + 1 if ai == b[j - 1] \
                else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def rouge_l(reference: str, hypothesis: str, beta: float = 1.2) -> float:
    """rouge-score 库 rougeL 口径: LCS 的 P/R 按 beta=1.2 加权（偏召回）。"""
    ref, hyp = tokenize(reference), tokenize(hypothesis)
    if not ref or not hyp:
        return 0.0
    lcs = _lcs_len(ref, hyp)
    p, r = lcs / len(hyp), lcs / len(ref)
    if p + r == 0:
        return 0.0
    return ((1 + beta * beta) * p * r) / (beta * beta * p + r)


def f1(reference: str, hypothesis: str) -> float:
    """token 级 F1（unigram 口径，与 rouge_1 的 fmeasure 一致，便于对照）。"""
    return rouge_n(reference, hypothesis, 1)


def text_scores(reference: str, hypothesis: str) -> dict:
    """一次算全五项，round(6) 入库/展示。"""
    return {
        "bleu": round(bleu(reference, hypothesis), 6),
        "rouge1": round(rouge_n(reference, hypothesis, 1), 6),
        "rouge2": round(rouge_n(reference, hypothesis, 2), 6),
        "rougeL": round(rouge_l(reference, hypothesis), 6),
        "f1": round(f1(reference, hypothesis), 6),
    }
