# -*- coding: utf-8 -*-
"""KB 对象存储（raw/ 源 + parsed/ 解析缓存 + wiki/ 落盘页）。

本地 PVC 主存储（07 §1，D-2026-08-16-02）：每 KB 一个目录；
OSS 定期同步见 kb/oss_sync.py（S3 兼容，只换 env）。

目录布局（root = EAGENT_KB_STORAGE_DIR）:
  <root>/<kb_id>/raw/<source_id>__<file_name>   不可变原始字节
  <root>/<kb_id>/parsed/<source_id>.md          解析产物（planner 只读消费）
  <root>/<kb_id>/wiki/<rel_path>                当前 Rev 页面（检索/同步载体）
"""
from __future__ import annotations

import os
import posixpath
from pathlib import Path

from ..config import settings


def _root() -> Path:
    root = Path(settings.kb_storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(name: str) -> str:
    # 防路径穿越：只保留文件名部分并剔除分隔符
    base = os.path.basename(name or "file")
    return "".join(ch if ch not in "/\\\0" else "_" for ch in base)[:200] or "file"


def put_raw(kb_id: str, source_id: str, file_name: str, data: bytes) -> str:
    """落原始字节，返回相对路径 raw/sources/<file_name>（表 rel_path 口径）。"""
    kb_dir = _root() / kb_id
    (kb_dir / "raw").mkdir(parents=True, exist_ok=True)
    fname = _safe_name(file_name)
    (kb_dir / "raw" / f"{source_id}__{fname}").write_bytes(data)
    return f"raw/sources/{fname}"


def raw_path(kb_id: str, source_id: str, file_name: str) -> Path:
    fname = _safe_name(file_name)
    return _root() / kb_id / "raw" / f"{source_id}__{fname}"


def get_raw(kb_id: str, source_id: str, file_name: str) -> bytes:
    return raw_path(kb_id, source_id, file_name).read_bytes()


def put_parsed(kb_id: str, source_id: str, markdown: str) -> None:
    kb_dir = _root() / kb_id
    (kb_dir / "parsed").mkdir(parents=True, exist_ok=True)
    (kb_dir / "parsed" / f"{source_id}.md").write_text(markdown, encoding="utf-8")


def get_parsed(kb_id: str, source_id: str) -> str | None:
    path = _root() / kb_id / "parsed" / f"{source_id}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


# ---------------------------------------------------------------------------
# wiki 落盘（08 §4.1，D-2026-08-16-02 / R-01）：检索 grep/glob 载体 + OSS 同步源
# ---------------------------------------------------------------------------
def wiki_path(kb_id: str, rel_path: str) -> Path:
    """rel_path 形如 wiki/policies/x.md（S6 命名空间内），映射到
    <root>/<kb_id>/wiki/policies/x.md。"""
    norm = posixpath.normpath(rel_path or "")
    if not norm.startswith("wiki/") or ".." in norm or norm.startswith("/"):
        raise ValueError(f"非法 wiki 路径: {rel_path}")
    return _root() / kb_id / norm


def read_wiki_page(kb_id: str, rel_path: str) -> str | None:
    path = wiki_path(kb_id, rel_path)
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    return None


def write_wiki_page(kb_id: str, rel_path: str, body_md: str) -> None:
    path = wiki_path(kb_id, rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body_md or "", encoding="utf-8")
    tmp.replace(path)


def sync_wiki_dir(kb_id: str, entries: list[dict]) -> dict:
    """publish 落盘双写（幂等覆盖）：entries = [{rel_path, body_md}]。

    使 <root>/<kb_id>/wiki/ 与当前 Rev manifest 一致：
    - 内容不同（或缺失）→ 覆盖写；
    - 目录中不在 manifest 的文件 → 删除（历史版本不落盘，08 §4.1）。
    返回计数 {written, unchanged, removed}；写失败抛异常 → 任务 FAIL 可重试。
    """
    wiki_dir = _root() / kb_id / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    expected = {}
    for e in entries:
        expected[wiki_path(kb_id, e["rel_path"])] = e["body_md"] or ""
    counts = {"written": 0, "unchanged": 0, "removed": 0}
    for path in list(wiki_dir.rglob("*")):
        if path.is_dir():
            continue
        if path not in expected:
            path.unlink(missing_ok=True)
            counts["removed"] += 1
    for path, body in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == body:
            counts["unchanged"] += 1
            continue
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        counts["written"] += 1
    return counts


def iter_local_files(kb_id: str) -> list[tuple[str, Path]]:
    """枚举该 KB 落盘文件（对账/同步用）：[(相对 key, Path)]，
    key 形如 raw/<sid>__<file> / parsed/<sid>.md / wiki/<rel_path>。"""
    kb_dir = _root() / kb_id
    if not kb_dir.is_dir():
        return []
    out = []
    for sub in ("raw", "parsed", "wiki"):
        d = kb_dir / sub
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix != ".tmp":
                out.append((f"{sub}/{p.relative_to(d).as_posix()}", p))
    return out


def delete_source_files(kb_id: str, source_id: str, file_name: str) -> None:
    raw_path(kb_id, source_id, file_name).unlink(missing_ok=True)
    (_root() / kb_id / "parsed" / f"{source_id}.md").unlink(missing_ok=True)


def delete_kb_dir(kb_id: str) -> None:
    import shutil

    shutil.rmtree(_root() / kb_id, ignore_errors=True)
