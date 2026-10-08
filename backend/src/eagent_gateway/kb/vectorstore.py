# -*- coding: utf-8 -*-
"""KB 向量库 Provider（D-2026-08-16-01：默认 LanceDB，可插拔）。

- LanceDBStore：每 KB 一个库目录（与 llm-wiki 每项目一库同构）；
  lancedb 未安装时由 LocalVectorStore 兜底（见 get_vector_store 的 auto）。
- LocalVectorStore：纯 Python 余弦扫描 + 每 KB 一份 JSON 持久化，
  一期单实例部署的等价实现（05 §7 风险表：LanceDB 单机形态的一期规避）。

行键 = eag_kb_chunk.vector_id（B3）；行内容含 kb_id/page_id/chunk_id/path。
向量字节不落 MySQL（02 §2.3）。
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Protocol

from ..config import settings

logger = logging.getLogger("eagent.kb.vector")


class VectorRecord(dict):
    """{id, vector, kb_id, page_id, chunk_id, path}。"""


class VectorStore(Protocol):
    name: str

    async def upsert(self, kb_id: str, records: list[dict]) -> None: ...
    async def delete(self, kb_id: str, ids: list[str]) -> None: ...
    async def delete_page(self, kb_id: str, page_id: str) -> None: ...
    async def search(
        self, kb_id: str, query_vector: list[float], limit: int,
    ) -> list[dict]: ...
    async def drop_kb(self, kb_id: str) -> None: ...
    # 同步孪生（GC 等同步上下文使用；Local 实现即本体，LanceDB 直调同步 API）
    def delete_sync(self, kb_id: str, ids: list[str]) -> None: ...
    def delete_page_sync(self, kb_id: str, page_id: str) -> None: ...


class LocalVectorStore:
    """本地 JSON 存储 + 余弦 top-k（一期兜底 Provider，接口与 LanceDB 对齐）。"""

    name = "local"

    def __init__(self, root: str | None = None):
        self.root = Path(root or settings.kb_vector_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict[str, dict]] = {}

    def _path(self, kb_id: str) -> Path:
        return self.root / kb_id / "vectors.json"

    def _load(self, kb_id: str) -> dict[str, dict]:
        if kb_id in self._cache:
            return self._cache[kb_id]
        path = self._path(kb_id)
        data: dict[str, dict] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
        self._cache[kb_id] = data
        return data

    def _save(self, kb_id: str) -> None:
        path = self._path(kb_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._cache.get(kb_id, {})
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    async def upsert(self, kb_id: str, records: list[dict]) -> None:
        data = self._load(kb_id)
        for r in records:
            data[r["id"]] = r
        self._save(kb_id)

    async def delete(self, kb_id: str, ids: list[str]) -> None:
        self.delete_sync(kb_id, ids)

    def delete_sync(self, kb_id: str, ids: list[str]) -> None:
        data = self._load(kb_id)
        for i in ids:
            data.pop(i, None)
        self._save(kb_id)

    async def delete_page(self, kb_id: str, page_id: str) -> None:
        self.delete_page_sync(kb_id, page_id)

    def delete_page_sync(self, kb_id: str, page_id: str) -> None:
        data = self._load(kb_id)
        for rid in [k for k, v in data.items() if v.get("page_id") == page_id]:
            data.pop(rid, None)
        self._save(kb_id)

    async def search(
        self, kb_id: str, query_vector: list[float], limit: int,
    ) -> list[dict]:
        data = self._load(kb_id)
        scored = []
        for rid, rec in data.items():
            score = _cosine(query_vector, rec.get("vector") or [])
            if score is None:
                continue
            scored.append((score, rid, rec))
        scored.sort(key=lambda t: t[0], reverse=True)
        out = []
        for score, rid, rec in scored[:limit]:
            item = {k: v for k, v in rec.items() if k != "vector"}
            item["id"] = rid
            item["score"] = score
            out.append(item)
        return out

    async def drop_kb(self, kb_id: str) -> None:
        import shutil

        shutil.rmtree(self.root / kb_id, ignore_errors=True)
        self._cache.pop(kb_id, None)


class LanceDBStore:
    """LanceDB Provider（每 KB 一库；lancedb 可导入时启用）。

    异步 API 用线程执行器包裹同步调用，避免阻塞事件循环。
    """

    name = "lancedb"

    def __init__(self, root: str | None = None):
        import lancedb  # noqa: F401 - fail fast if missing

        self._lancedb = lancedb
        self.root = Path(root or settings.kb_vector_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _table(self, kb_id: str):
        db = self._lancedb.connect(str(self.root / kb_id))
        try:
            return db.open_table("chunks")
        except Exception:  # noqa: BLE001 - 表不存在时建表
            return None

    async def upsert(self, kb_id: str, records: list[dict]) -> None:
        import asyncio

        await asyncio.to_thread(self._upsert_sync, kb_id, records)

    def _upsert_sync(self, kb_id: str, records: list[dict]) -> None:
        db = self._lancedb.connect(str(self.root / kb_id))
        table = self._table(kb_id)
        if table is None:
            db.create_table("chunks", records)
            return
        table.add(records)

    async def delete(self, kb_id: str, ids: list[str]) -> None:
        self.delete_sync(kb_id, ids)

    def delete_sync(self, kb_id: str, ids: list[str]) -> None:
        table = self._table(kb_id)
        if table is None or not ids:
            return
        quoted = ", ".join(f"'{i}'" for i in ids)
        table.delete(f"id IN ({quoted})")

    async def delete_page(self, kb_id: str, page_id: str) -> None:
        self.delete_page_sync(kb_id, page_id)

    def delete_page_sync(self, kb_id: str, page_id: str) -> None:
        table = self._table(kb_id)
        if table is None:
            return
        table.delete(f"page_id = '{page_id}'")

    async def search(
        self, kb_id: str, query_vector: list[float], limit: int,
    ) -> list[dict]:
        table = self._table(kb_id)
        if table is None:
            return []
        rows = (
            table.search(query_vector).limit(limit).to_list()
        )
        out = []
        for r in rows:
            r = dict(r)
            vec = r.pop("vector", None)
            score = r.pop("_distance", None)
            r["score"] = 1 - float(score) if score is not None else 0.0
            out.append(r)
        return out

    async def drop_kb(self, kb_id: str) -> None:
        import shutil

        shutil.rmtree(self.root / kb_id, ignore_errors=True)


def _cosine(a: list[float], b: list[float]) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """按配置选择 Provider：auto = lancedb 可导入则用，否则本地实现。"""
    global _store
    if _store is not None:
        return _store
    backend = settings.kb_vector_backend
    if backend in ("auto", "lancedb"):
        try:
            _store = LanceDBStore()
            logger.info("KB vector store: lancedb (%s)", settings.kb_vector_dir)
            return _store
        except ImportError:
            if backend == "lancedb":
                raise RuntimeError(
                    "EAGENT_KB_VECTOR_BACKEND=lancedb 但 lancedb 未安装",
                )
            logger.warning(
                "lancedb 未安装，KB 向量库回退 local Provider（单实例适用）",
            )
    _store = LocalVectorStore()
    logger.info("KB vector store: local (%s)", settings.kb_vector_dir)
    return _store


def reset_vector_store() -> None:
    """测试用：重置单例（切换 backend 后生效）。"""
    global _store
    _store = None
