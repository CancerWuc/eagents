# -*- coding: utf-8 -*-
"""启动种子与后台循环（higress-mcp-gateway 03 T06/T13）。

- assert_startup_config：EAGENT_GATEWAY_ENABLED=1 而主密钥未配置/非法 →
  拒绝启动（fail-secure，T12）；未启用不校验（可选功能不拖垮共用部署）。
- seed_gateway_profile：EAGENT_GATEWAY_PROFILE_NAME / _HIGRESS_NS /
  _KUBE_CONTEXT 三件套齐备时幂等自动建 profile（部署后一键可用）。
- grace_sweep_loop：轮换宽限期到期的旧密钥定期下线（0 = 禁用，惰性收敛）。
"""
from __future__ import annotations

import asyncio
import logging

from .. import config
from ..persistence.db import Database
from . import consumer as consumer_mod
from . import dao

logger = logging.getLogger("eagent.gateway.seed")


def assert_startup_config() -> None:
    if not config.settings.gateway_enabled:
        return
    from . import crypto

    crypto.assert_master_key_configured()  # 非法即抛错 → 拒绝启动
    logger.info("gateway control-plane enabled (master key ok)")


def seed_gateway_profile(db: Database) -> str | None:
    s = config.settings
    if not s.gateway_profile_name or not s.gateway_profile_higress_ns:
        return None
    name = s.gateway_profile_name
    if dao.get_profile_by_name(db, name):
        return None
    p = dao.create_profile(
        db, name=name, higress_ns=s.gateway_profile_higress_ns,
        kube_context=s.gateway_profile_kube_context,
    )
    dao.record_audit(
        db, profile_id=p["id"], resource_type="profile", action="create",
        payload_before=None, payload_after={"name": name, "seeded": True},
        result="success", operator="system",
    )
    logger.info(
        "seeded gateway profile: %s (ns=%s)", name,
        s.gateway_profile_higress_ns,
    )
    return name


async def grace_sweep_loop() -> None:
    interval = config.settings.gateway_grace_sweep_interval_s
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            db = Database()
            try:
                swept = consumer_mod.sweep_expired_grace(db)
                if swept:
                    logger.info("grace sweep retired %d old key(s)", len(swept))
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("gateway grace sweep failed")
