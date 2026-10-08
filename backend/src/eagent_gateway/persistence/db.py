# -*- coding: utf-8 -*-
"""持久层: SQLAlchemy 引擎 + 会话 + 常用查询助手（Core 风格，轻 ORM）。"""
import uuid
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from ..config import settings

_engine = None
_session_factory = None


def engine():
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(
            settings.db_dsn,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=8,
            max_overflow=8,
        )
        _session_factory = sessionmaker(bind=_engine)
    return _engine


def new_id(prefix: str = "") -> str:
    """带前缀的 32 位 ID（所有主键列均为 VARCHAR(32)）。"""
    raw = uuid.uuid4().hex
    if not prefix:
        return raw
    return (prefix + raw)[:32]


def now() -> datetime:
    return datetime.now()


class Database:
    """每请求一个实例; commit 由调用方显式控制。"""

    def __init__(self) -> None:
        engine()
        self.session = _session_factory()

    def execute(self, sql: str, params: dict | None = None):
        return self.session.execute(text(sql), params or {})

    def query_one(self, sql: str, params: dict | None = None) -> dict | None:
        row = self.execute(sql, params).mappings().first()
        return dict(row) if row is not None else None

    def query_all(self, sql: str, params: dict | None = None) -> list[dict]:
        rows = self.execute(sql, params).mappings().all()
        return [dict(r) for r in rows]

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def close(self) -> None:
        self.session.close()
