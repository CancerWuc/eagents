# -*- coding: utf-8 -*-
"""检索数据源适配层（standalone-deployment 01 §5.1，S-01/S-03）。

retrieval.py 只依赖 ``SearchBackend`` 接口，同一套检索逻辑吃两种数据源：
- 实现A ``MySQLBackend``（平台内嵌）：现有 dao 调用与两处裸 SQL 的薄封装，
  行为不变（R 系列回归保障）；
- 实现B ``SnapshotBackend``（独立容器）：读 KB 快照文件（manifest/chunks/
  graph/sources/wiki，格式见 kb/snapshot.py 导出侧）。

同步接口：retrieval 全链路本无 await（dao 同步、文件 IO 量小），不引入
async 扩散。JSON 字段约定在 backend 层解析完毕：
- kb_row 的 ``search_config`` 为 dict（未配置为 None）；
- 页面行的 ``frontmatter_json`` 为 dict（无 frontmatter 为 None）。

纪律：检索链路新增数据访问必须过 backend（本接口清单随调用点同步扩）。
"""
from __future__ import annotations

import json
import posixpath
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .constants import normalize_search_config

if TYPE_CHECKING:
    from ..persistence.db import Database


# ---------------------------------------------------------------------------
# 快照格式（kb/snapshot.py 导出 / SnapshotBackend 读取，两侧唯一口径）
# ---------------------------------------------------------------------------
SNAPSHOT_FORMAT_VERSION = 1  # 引擎启动校验，老引擎跑新格式 fail-closed
MANIFEST_FILE = "manifest.json"
GRAPH_FILE = "graph.json"
CHUNKS_FILE = "chunks.json"
SOURCES_FILE = "sources.json"
INDEX_FILE = "index.json"  # 多 KB 快照根：{slug: {dir, rev, kb_id}}


class SearchBackend(Protocol):
    """检索链路的全部数据访问面（01 §5.1 十方法，按实际调用点盘点）。"""

    def kb_row(self, kb_id: str) -> dict | None:
        """KB 行（slug/display_name/current_rev/search_config 已解析为 dict）。"""
        ...

    def rev_ready(self, kb_id: str, rev: str) -> bool:
        """rev 对应行存在且 READY（快照侧恒 True）。"""
        ...

    def manifest_of(self, kb_id: str, rev: str) -> dict[str, str]:
        """rev → {page_id: content_hash}。"""
        ...

    def manifest_pages(self, kb_id: str, rev: str) -> list[dict]:
        """manifest join 页面行（page_id/rel_path/title/body_md/
        frontmatter_json 已解析/content_hash）。"""
        ...

    def page_by_path(self, kb_id: str, rel_path: str) -> dict | None:
        """当前版本页面行（read_page 用；frontmatter_json 已解析）。"""
        ...

    def page_id_path_rows(self, kb_id: str) -> list[dict]:
        """[{page_id, rel_path}]（seed_paths/_path_to_pid 用）。"""
        ...

    def chunks_of_page(self, page_id: str) -> list[dict]:
        """页面 chunk 行，按 chunk_index 升序（chunk_id/chunk_index/text_md）。"""
        ...

    def neighbors_of(self, kb_id: str, paths: set[str]) -> dict[str, set[str]]:
        """对称一跳邻接：path → 外部邻居集合（仅表内边，查询期过滤）。"""
        ...

    def source_path_map(self, kb_id: str) -> dict[str, str]:
        """source_id → rel_path（sources 溯源）。"""
        ...

    def read_wiki_body(self, kb_id: str, rel_path: str) -> str | None:
        """keyword 信号文件源正文（平台=落盘 wiki/，容器=快照内 wiki/）。"""
        ...


def search_config_of(kb: dict) -> dict:
    """kb_row → 归一化 search_config（backend 已解析，这里只做默认值合并）。"""
    return normalize_search_config((kb or {}).get("search_config"))


# ---------------------------------------------------------------------------
# 实现A：MySQLBackend（平台，行为与改造前 dao 直调等价）
# ---------------------------------------------------------------------------
class MySQLBackend:
    """现有 dao 调用与两处裸 SQL 的薄封装（retrieval.py 改造前的等价实现）。

    - ``MySQLBackend(db)``：借用调用方连接（生命周期归调用方）；
    - ``MySQLBackend()``：自建 Database（close 时关闭；kb_tool 每调用一个）。
    """

    def __init__(self, db: Database | None = None) -> None:
        from . import dao

        self._dao = dao
        self._owns_db = db is None
        self._db: Database | None = db

    # -- 生命周期 -----------------------------------------------------------
    def close(self) -> None:
        if self._owns_db and self._db is not None:
            self._db.close()
            self._db = None

    @property
    def db(self) -> Database:
        """惰性建连（首次数据访问时构造，容器内不 import 本类即可无 MySQL）。"""
        if self._db is None:
            from ..persistence.db import Database

            self._db = Database()
        return self._db

    # -- KB 行与 Rev --------------------------------------------------------
    def kb_row(self, kb_id: str) -> dict | None:
        row = self._dao.get_kb(self.db, kb_id)
        if row is None:
            return None
        row = dict(row)
        row["search_config"] = self._dao.jload(row.get("search_config"))
        return row

    def rev_ready(self, kb_id: str, rev: str) -> bool:
        row = self._dao.get_rev(self.db, kb_id, rev)
        return bool(row and row["status"] == "READY")

    # -- manifest 与页面 ----------------------------------------------------
    def manifest_of(self, kb_id: str, rev: str) -> dict[str, str]:
        return self._dao.manifest_of(self.db, kb_id, rev)

    def manifest_pages(self, kb_id: str, rev: str) -> list[dict]:
        rows = self._dao.manifest_pages(self.db, kb_id, rev)
        return [_page_row(r) for r in rows]

    def page_by_path(self, kb_id: str, rel_path: str) -> dict | None:
        row = self._dao.current_page_by_path(self.db, kb_id, rel_path)
        return _page_row(row) if row else None

    def page_id_path_rows(self, kb_id: str) -> list[dict]:
        return self.db.query_all(
            "SELECT page_id, rel_path FROM eag_kb_page WHERE kb_id=:k",
            {"k": kb_id},
        )

    # -- chunk / graph / 溯源 ------------------------------------------------
    def chunks_of_page(self, page_id: str) -> list[dict]:
        return self._dao.chunks_of_page(self.db, page_id)

    def neighbors_of(self, kb_id: str, paths: set[str]) -> dict[str, set[str]]:
        return self._dao.neighbors_of(self.db, kb_id, paths)

    def source_path_map(self, kb_id: str) -> dict[str, str]:
        rows = self.db.query_all(
            "SELECT source_id, rel_path FROM eag_kb_source WHERE kb_id=:k",
            {"k": kb_id},
        )
        return {r["source_id"]: r["rel_path"] for r in rows}

    # -- 正文文件 ------------------------------------------------------------
    def read_wiki_body(self, kb_id: str, rel_path: str) -> str | None:
        from . import storage

        return storage.read_wiki_page(kb_id, rel_path)


def _page_row(row: dict) -> dict:
    """页面行 JSON 列解析（frontmatter_json str → dict|None）。"""
    from . import dao

    out = dict(row)
    out["frontmatter_json"] = dao.jload(out.get("frontmatter_json"))
    return out


# ---------------------------------------------------------------------------
# 实现B：SnapshotBackend（独立容器，S-03；读快照文件，只读）
# ---------------------------------------------------------------------------
class SnapshotBackend:
    """读 KB 快照目录（01 §4.1 布局）：无 MySQL/平台依赖的只读数据源。

    - ``kb_row``/``manifest_*``/``page_by_path``/``page_id_path_rows`` ←
      manifest.json；``chunks_of_page`` ← chunks.json；``neighbors_of`` ←
      graph.json（缺失返回空 → 检索退化为纯 grep 单路）；``source_path_map``
      ← sources.json；``read_wiki_body`` ← wiki/<rel_path>；
    - ``rev_ready`` 恒 True（快照导出自 READY Rev，即快照态 Rev）；
    - manifest 不含 body_md：页面行的正文按「导出即对账」从快照 wiki 文件
      读回（导出时以 DB body_md 重写，二者一致，§4.2）。
    """

    def __init__(self, snapshot_dir: str | Path) -> None:
        self.root = Path(snapshot_dir)
        self._manifest: dict | None = None
        self._graph: dict[str, set[str]] | None = None
        self._chunks: dict[str, list[dict]] | None = None
        self._sources: dict[str, str] | None = None
        self._pages_by_id: dict[str, dict] | None = None

    def close(self) -> None:  # 文件数据源无连接生命周期
        pass

    # -- 快照文件加载 --------------------------------------------------------
    def manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = _load_json(self.root / MANIFEST_FILE)
        return self._manifest

    def _pages(self) -> dict[str, dict]:
        if self._pages_by_id is None:
            pages = {}
            for p in self.manifest().get("pages", []):
                row = dict(p)
                row["rel_path"] = p["path"]
                row["content_hash"] = p["hash"]
                row["frontmatter_json"] = {"sources": p.get("sources") or []}
                row["body_md"] = self.read_wiki_body(
                    self.kb_id_from_manifest(), p["path"],
                )
                pages[p["page_id"]] = row
            self._pages_by_id = pages
        return self._pages_by_id

    def kb_id_from_manifest(self) -> str:
        return str(self.manifest().get("kb_id", ""))

    # -- SearchBackend 十方法 ------------------------------------------------
    def kb_row(self, kb_id: str) -> dict | None:
        m = self.manifest()
        if not m or (kb_id and m.get("kb_id") != kb_id):
            return None
        return {
            "kb_id": m.get("kb_id"),
            "slug": m.get("kb_slug"),
            "display_name": m.get("display_name"),
            "current_rev": m.get("rev"),
            "status": "READY",
            "search_config": m.get("search_config"),
        }

    def rev_ready(self, kb_id: str, rev: str) -> bool:
        return True  # 快照即 READY Rev

    def manifest_of(self, kb_id: str, rev: str) -> dict[str, str]:
        return {p["page_id"]: p["hash"]
                for p in self.manifest().get("pages", [])}

    def manifest_pages(self, kb_id: str, rev: str) -> list[dict]:
        return list(self._pages().values())

    def page_by_path(self, kb_id: str, rel_path: str) -> dict | None:
        for row in self._pages().values():
            if row["rel_path"] == rel_path:
                return row
        return None

    def page_id_path_rows(self, kb_id: str) -> list[dict]:
        return [{"page_id": pid, "rel_path": row["rel_path"]}
                for pid, row in self._pages().items()]

    def chunks_of_page(self, page_id: str) -> list[dict]:
        if self._chunks is None:
            self._chunks = _load_json(self.root / CHUNKS_FILE, default={})
        return [dict(c) for c in self._chunks.get(page_id, [])]

    def neighbors_of(self, kb_id: str, paths: set[str]) -> dict[str, set[str]]:
        if not paths:
            return {}
        if self._graph is None:
            graph = _load_json(self.root / GRAPH_FILE, default={})
            self._graph = {s: set(v) for s, v in graph.items()}
        adj: dict[str, set[str]] = {}
        for s, nbrs in self._graph.items():
            if s not in paths:
                continue
            ext = {n for n in nbrs if n not in paths}
            if ext:
                adj[s] = ext
        return adj

    def source_path_map(self, kb_id: str) -> dict[str, str]:
        if self._sources is None:
            self._sources = _load_json(self.root / SOURCES_FILE, default={})
        return dict(self._sources)

    def read_wiki_body(self, kb_id: str, rel_path: str) -> str | None:
        norm = posixpath.normpath(rel_path or "")
        if not norm.startswith("wiki/") or ".." in norm or norm.startswith("/"):
            return None
        path = self.root / norm
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
        return None


def _load_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_snapshot_index(root: str | Path) -> dict[str, dict]:
    """多 KB 快照根 → {slug: {dir, kb_id, rev}}（01 §4.1；容器装配用）。

    写侧见 kb/snapshot.py write_snapshot_index（平台导出端）。
    """
    data = _load_json(Path(root) / INDEX_FILE, default=None)
    if not isinstance(data, dict):
        raise ValueError(f"快照根缺少 {INDEX_FILE}（多 KB 快照布局不完整）")
    return data


def snapshot_dir_hash(snapshot_dir: str | Path) -> str:
    """从快照目录重算确定性内容哈希（容器侧运行时复核 / 平台打包前校验）。

    与导出侧 kb/snapshot.snapshot_hash 同一聚合函数（对内容不对字节）。
    """
    from .snapshot_hash import snapshot_hash  # 纯函数，无平台依赖

    root = Path(snapshot_dir)
    manifest = _load_json(root / MANIFEST_FILE)
    if not isinstance(manifest, dict):
        raise ValueError(f"快照缺少 {MANIFEST_FILE}: {root}")
    graph_raw = _load_json(root / GRAPH_FILE, default={}) or {}
    graph = {s: set(v) for s, v in graph_raw.items()}
    chunks = _load_json(root / CHUNKS_FILE, default={}) or {}
    return snapshot_hash(manifest, graph, chunks)


def snapshot_root_hash(root: str | Path) -> str:
    """多 KB 快照根哈希 = sha256(canonical_json({slug: 每库哈希}))。

    deploy_digest 的快照分量（01 §7.1）；与导出侧 write 顺序无关。
    """
    from .constants import canonical_json, sha256_hex

    index = load_snapshot_index(root)
    per_kb = {
        slug: snapshot_dir_hash(Path(root) / e["dir"])
        for slug, e in index.items()
    }
    return sha256_hex(canonical_json(per_kb))


def check_snapshot_format(snapshot_dir: str | Path) -> int:
    """快照 manifest 的 snapshot_format_version 启动校验（fail-closed）。

    引擎只认当前 SNAPSHOT_FORMAT_VERSION；不等（老/新格式）→ ValueError，
    老引擎跑新格式绝不静默降级（01 §7.1 第二道错配防线）。
    """
    manifest = _load_json(Path(snapshot_dir) / MANIFEST_FILE)
    if not isinstance(manifest, dict):
        raise ValueError(f"快照缺少 {MANIFEST_FILE}: {snapshot_dir}")
    version = manifest.get("snapshot_format_version")
    if version != SNAPSHOT_FORMAT_VERSION:
        raise ValueError(
            f"快照格式版本不支持: {version}（引擎支持 "
            f"{SNAPSHOT_FORMAT_VERSION}）: {snapshot_dir}"
        )
    return int(version)


def snapshot_backends_for_root(
    root: str | Path, kb_slugs: list[str],
) -> list[tuple[str, SnapshotBackend]]:
    """按 slug 集合装配 SnapshotBackend 列表（绑定集过滤，§8 快照授权）。

    slug 缺失于快照根 → ValueError（fail-closed：绑定 KB 必须都在快照内）。
    """
    index = load_snapshot_index(root)
    out: list[tuple[str, SnapshotBackend]] = []
    for slug in kb_slugs:
        entry = index.get(slug)
        if not entry:
            raise ValueError(f"快照根缺少 KB 绑定: {slug}")
        out.append((slug, SnapshotBackend(Path(root) / entry["dir"])))
    return out
