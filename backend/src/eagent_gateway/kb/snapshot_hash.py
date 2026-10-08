# -*- coding: utf-8 -*-
"""确定性快照哈希（standalone-deployment 01 §4.2，S-02）。

纯函数、无平台依赖：导出侧（kb/snapshot.py）与容器侧
（kb/backends.py snapshot_dir_hash）共用同一实现——
「对内容不对 tar 字节」（tar 含 mtime 不可复现），同 Rev 跨机重导必得同值。
"""
from __future__ import annotations

import hashlib

from .constants import canonical_json


def snapshot_hash(
    manifest: dict, graph: dict[str, set[str] | list[str]],
    chunks: dict[str, list[dict]],
) -> str:
    """manifest 页 hash（按 path 排序拼接）+ graph + chunks 递归聚合。

    与写入顺序无关、与 exported_at/目录 mtime 无关；graph/chunks 经
    canonical_json（sort_keys + 排序列表）规范化。
    """
    h = hashlib.sha256()
    h.update(b"pages\x00")
    for p in sorted(manifest["pages"], key=lambda x: x["path"]):
        h.update(f"{p['path']}\x1f{p['hash']}\x1e".encode("utf-8"))
    h.update(b"graph\x00")
    h.update(canonical_json({
        s: sorted(v) for s, v in sorted(graph.items())
    }).encode("utf-8"))
    h.update(b"chunks\x00")
    h.update(canonical_json({
        pid: sorted(
            ({"chunk_id": c["chunk_id"], "chunk_index": c["chunk_index"],
              "text_md": c["text_md"]} for c in cl),
            key=lambda c: c["chunk_index"],
        )
        for pid, cl in sorted(chunks.items())
    }).encode("utf-8"))
    return h.hexdigest()
