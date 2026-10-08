# -*- coding: utf-8 -*-
"""幂等迁移（db/migrations/*.sql）。

- 版本登记在 eag_schema_migration（文件名前缀 NNN 即版本号）。
- 语句级幂等: 容忍 MySQL 1050(表已存在)/1060(列已存在)/1061(索引已存在)
  （及登记补写 1062、删索引 1091），另对「语句含 ngram 的 1064」做
  OceanBase 兼容容忍（见 _tolerated），其余错误立即失败（fail-closed，
  不静默跳过）。
- 应用启动（bootstrap_schema 之后）与 scripts/apply_migrations.py 共用。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .db import Database

# 与 apply_migrations.py 相同的容忍集合
# 1062: 迁移文件内的幂等 guard INSERT 已登记版本时，引擎补登记的主键冲突
# 1091: DROP INDEX 目标索引已不存在（009_kb.sql 幂等删除 FULLTEXT，08 R-10）
_TOLERATED = (1050, 1060, 1061, 1062, 1091)


def migrations_dir() -> Path:
    env = os.environ.get("EAGENT_MIGRATIONS_DIR", "")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    # 仓库布局（src-layout）: src/eagent_gateway/persistence/ 的上三级
    # 是仓库根 -> db/migrations; 容器内包可能装进 site-packages，
    # 由 /app/db/migrations 兜底（Dockerfile: COPY db /app/db）
    repo = here.parents[3]
    candidates = [repo / "db" / "migrations", Path("/app/db/migrations")]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return repo / "db" / "migrations"


def _statements(sql_text: str) -> list[str]:
    stmts = []
    for raw in sql_text.split(";"):
        lines = [
            ln for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("--")
        ]
        stmt = "\n".join(lines).strip()
        if stmt:
            stmts.append(stmt)
    return stmts


def _tolerated(exc: Exception, stmt: str) -> bool:
    code = getattr(getattr(exc, "orig", None), "args", [None])[0]
    if isinstance(code, int) and code in _TOLERATED:
        return True
    # OceanBase 兼容（窄条件）：仅 1064 且语句含 ngram 时容忍。
    # OceanBase MySQL 租户（实测 5.7.25-oceanbase-v4.2.1.10）不支持
    # `WITH PARSER ngram` 全文语法，解析阶段即 1064；该索引在最终形态已被
    # 009_kb.sql 删除（检索去向量化，08 R-10），跳过零损失。其余 1064 仍
    # 立即失败，fail-closed 口径不变（2026-09-01 内网部署实测引入）。
    return code == 1064 and "ngram" in stmt.lower()


def apply_pending_migrations(db: Database) -> list[int]:
    """应用全部未执行的迁移，返回本次应用的版本号列表。"""
    applied: list[int] = []
    files = sorted(migrations_dir().glob("*.sql"))
    done = {
        r["version_no"] for r in db.query_all(
            "SELECT version_no FROM eag_schema_migration",
        )
    }
    for path in files:
        m = re.match(r"^(\d+)_", path.name)
        if not m:
            raise RuntimeError(f"迁移文件名缺少版本前缀: {path.name}")
        version = int(m.group(1))
        if version in done:
            continue
        for stmt in _statements(path.read_text(encoding="utf-8")):
            try:
                db.execute(stmt)
            except Exception as exc:  # noqa: BLE001
                if _tolerated(exc, stmt):
                    continue
                raise RuntimeError(
                    f"迁移失败 {path.name}: {stmt[:120]}",
                ) from exc
        db.execute(
            # 幂等登记：已登记版本时主键冲突被 IGNORE
            "INSERT IGNORE INTO eag_schema_migration (version_no, name, applied_at)"
            " VALUES (:v, :n, CURRENT_TIMESTAMP(6))",
            {"v": version, "n": path.stem},
        )
        db.commit()
        applied.append(version)
    return applied
