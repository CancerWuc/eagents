# -*- coding: utf-8 -*-
"""KB 对象存储定期同步（08 §4.2，D-2026-08-16-02 / R-05）。

- 协议统一 S3 兼容（minio SDK，endpoint 可配）：本地 MinIO / k8s MinIO /
  阿里云 OSS 只换 env，零代码改动；bucket 首次同步自动创建（幂等）。
- 增量：按文件 sha256 对比断点清单，未变化跳过；**先 raw 后 parsed/wiki**
  （raw 不可再生，优先级最高，07 §3）。
- 断点续传：每文件上传成功即落 `.oss_sync.json` 清单（崩溃后不重传已完成
  文件）；大文件由 minio put_object 分块上传；失败清单进审计，下轮重试。
- 审计：kb.oss.sync（detail 记 uploaded/skipped/failed）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Protocol

from ..config import settings
from ..persistence.db import Database
from . import dao, storage
from .constants import sha256_hex

logger = logging.getLogger("eagent.kb.oss")


class OssClient(Protocol):
    """S3 兼容客户端最小面（测试可注入 fake）。"""

    def ensure_bucket(self) -> None: ...
    def exists(self, key: str) -> bool: ...
    def upload_file(self, key: str, path: Path) -> None: ...
    def download_file(self, key: str, path: Path) -> None: ...


class MinioOssClient:
    """minio SDK 实现（依赖：pip install minio，pyproject [kb-oss] 可选组）。"""

    def __init__(self, *, endpoint: str, bucket: str,
                 access_key: str, secret_key: str):
        try:
            from minio import Minio
        except ImportError as exc:  # pragma: no cover - 联调环境才有
            raise RuntimeError(
                "EAGENT_KB_OSS_* 已配置但 minio SDK 未安装"
                "（pip install minio 或安装 .[kb-oss]）",
            ) from exc
        self.bucket = bucket
        secure = endpoint.startswith("https://")
        self._client = Minio(
            endpoint.replace("https://", "").replace("http://", ""),
            access_key=access_key, secret_key=secret_key, secure=secure,
        )

    def ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self.bucket):
            self._client.make_bucket(self.bucket)
            logger.info("oss bucket created: %s", self.bucket)

    def exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self.bucket, key)
            return True
        except Exception:  # noqa: BLE001 - 不存在/无权限统一按缺失处理
            return False

    def upload_file(self, key: str, path: Path) -> None:
        # put_object 按文件路径上传（内部自动分块，断点续传由清单保证文件粒度）
        self._client.fput_object(
            self.bucket, key, str(path),
            metadata={"sha256": sha256_hex(path.read_bytes())},
        )

    def download_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client.fget_object(self.bucket, key, str(path))


def oss_configured() -> bool:
    return bool(settings.kb_oss_endpoint)


def build_client() -> MinioOssClient | None:
    if not oss_configured():
        return None
    return MinioOssClient(
        endpoint=settings.kb_oss_endpoint, bucket=settings.kb_oss_bucket,
        access_key=settings.kb_oss_access_key,
        secret_key=settings.kb_oss_secret_key,
    )


def _object_key(kb_id: str, relkey: str) -> str:
    prefix = settings.kb_oss_prefix.strip("/")
    parts = [p for p in (prefix, kb_id, relkey) if p]
    return "/".join(parts)


def _state_path(kb_id: str) -> Path:
    return Path(settings.kb_storage_dir) / kb_id / ".oss_sync.json"


def _load_state(kb_id: str) -> dict:
    path = _state_path(kb_id)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def _save_state(kb_id: str, state: dict) -> None:
    path = _state_path(kb_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def sync_kb(db: Database, kb_id: str, client: OssClient | None = None) -> dict:
    """单 KB 增量同步（raw 优先；返回计数并审计）。"""
    if client is None:
        client = build_client()
        if client is None:
            return {"disabled": True}
    client.ensure_bucket()
    state = _load_state(kb_id)
    counts = {"uploaded": 0, "skipped": 0, "failed": []}
    # iter_local_files 顺序 = raw → parsed → wiki（raw 优先，07 §3）
    for relkey, path in storage.iter_local_files(kb_id):
        digest = sha256_hex(path.read_bytes())
        if state.get(relkey) == digest:
            counts["skipped"] += 1
            continue
        try:
            client.upload_file(_object_key(kb_id, relkey), path)
            # 断点续传：成功即落清单（崩溃后不重传已完成文件）
            state[relkey] = digest
            _save_state(kb_id, state)
            counts["uploaded"] += 1
        except Exception as exc:  # noqa: BLE001 - 单文件失败不阻塞，下轮重试
            counts["failed"].append({"key": relkey, "error": str(exc)[:200]})
    if counts["uploaded"] or counts["failed"]:
        dao.audit(
            db, user="system", action="kb.oss.sync", resource_type="kb",
            resource_id=kb_id, detail=counts,
        )
        db.commit()
    logger.info("kb oss sync %s: uploaded=%s skipped=%s failed=%s",
                kb_id, counts["uploaded"], counts["skipped"],
                len(counts["failed"]))
    return counts


def sync_all(client: OssClient | None = None) -> list[dict]:
    if not oss_configured():
        return []
    db = Database()
    try:
        out = []
        for kb in dao.list_kbs(db):
            try:
                out.append({"kb_id": kb["kb_id"],
                            **sync_kb(db, kb["kb_id"], client=client)})
            except Exception:  # noqa: BLE001
                logger.exception("kb oss sync failed: %s", kb["kb_id"])
        return out
    finally:
        db.close()


async def kb_oss_sync_loop() -> None:
    """定时增量同步（0 = 禁用；本地开发 60s、测试/生产 3600，08 §4.2）。"""
    if settings.kb_oss_sync_interval_s <= 0:
        return
    while True:
        await asyncio.sleep(settings.kb_oss_sync_interval_s)
        try:
            sync_all()
        except Exception:  # noqa: BLE001
            logger.exception("kb oss sync loop failed")
