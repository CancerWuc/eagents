# -*- coding: utf-8 -*-
"""KB 解析 Provider（03 §3.1 parse 段）。

- Markdown/纯文本：原文即 Markdown（默认自研实现）。
- PDF：可插拔——pypdf 可导入时逐页抽取文本（大 PDF 按页推进度语义）；
  未安装时抛 ParseError（fail-closed，不猜格式）。
- Office/其他：一期不覆盖（01 §10 开放项 4），明确报错。
不复制 RAGFlow/llm-wiki 代码，只实现其语义。
"""
from __future__ import annotations

import re


class ParseError(ValueError):
    """解析失败（fail-closed，落 source.error_code）。"""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


_MD_HEAD = re.compile(r"^#{1,6}\s+(.+)$", re.M)


def guess_mime(file_name: str, declared: str | None) -> str:
    name = (file_name or "").lower()
    if name.endswith(".md") or name.endswith(".markdown"):
        return "text/markdown"
    if name.endswith(".txt"):
        return "text/plain"
    if name.endswith(".pdf"):
        return "application/pdf"
    if name.endswith((".docx", ".doc")):
        return "application/msword"
    if declared:
        return declared
    return "application/octet-stream"


def parse_bytes(data: bytes, *, file_name: str, mime_type: str | None) -> str:
    """raw 字节 → Markdown 文本（Provider 可插拔入口）。"""
    mime = mime_type or guess_mime(file_name, None)
    if mime in ("text/markdown", "text/plain", "application/json",
                "text/csv") or file_name.lower().endswith(
            (".md", ".markdown", ".txt", ".json", ".csv")):
        return data.decode("utf-8", errors="replace")
    if mime == "application/pdf" or file_name.lower().endswith(".pdf"):
        return _parse_pdf(data)
    raise ParseError(
        "UNSUPPORTED_FORMAT",
        f"一期不支持该格式: {file_name}（{mime}）；解析器以 Provider 扩展",
    )


def _parse_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ParseError(
            "PDF_PROVIDER_MISSING",
            "PDF 解析需要安装 pypdf（可插拔 Provider 未安装）",
        ) from exc
    try:
        reader = PdfReader(__import__("io").BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"## 第 {i + 1} 页\n\n{text}")
        return "\n\n".join(pages) if pages else ""
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ParseError("PDF_PARSE_FAILED", f"PDF 解析失败: {exc}") from exc


def heading_signals(markdown: str, limit: int = 30) -> list[str]:
    """标题信号（planner 输入 2：解析出的目录结构/标题信号）。"""
    return [h.strip() for h in _MD_HEAD.findall(markdown or "")][:limit]


def summarize_markdown(markdown: str, *, max_chars: int = 1200) -> str:
    """源解析摘要：标题清单 + 正文首段（供规划上下文，非检索）。"""
    heads = heading_signals(markdown, limit=10)
    body = re.sub(r"\s+", " ", markdown or "").strip()
    head_txt = "\n".join(f"- {h}" for h in heads) or "（无标题结构）"
    return f"标题结构:\n{head_txt}\n\n正文摘要: {body[:max_chars]}"
