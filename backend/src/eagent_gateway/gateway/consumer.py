# -*- coding: utf-8 -*-
"""consumer 密钥管理（higress-mcp-gateway 03 T09/T13）。

- 创建：生成随机 32B base64url 密钥 → 库内 AES-GCM 加密 → key-auth
  WasmPlugin defaultConfig.consumers 追加（明文展开是 Higress 现状，01 §5.4）。
- 轮换：新密钥替换 credential，旧密钥挪 prev_* 并保留宽限期
  （EAGENT_GATEWAY_KEY_GRACE_SECONDS，默认 300s）；期内 key-auth 条目的
  credentials 同时含新旧两把；到期由 sweep_expired_grace 下线旧 key。
- 绑定路由（route_ids）变更后重写对应 ai-route CM（allowedConsumers 联动）。
"""
from __future__ import annotations

from datetime import timedelta

from .. import config, errors
from ..persistence.db import Database, now
from . import crypto, dao, resources, route as route_mod
from .k8s_client import GatewayK8sClient

KEY_AUTH_PLUGIN = "key-auth.internal"


def _consumer_out(db: Database, c: dict) -> dict:
    rids = dao.jload(c.get("route_ids")) or []
    names = []
    for rid in rids:
        r = dao.get_route(db, rid)
        if r:
            names.append(r["name"])
    try:
        masked = crypto.mask_credential(bytes(c["credential"]))
    except Exception:  # noqa: BLE001 主密钥缺失/密文异常时仍可列表（不暴露）
        masked = "****"
    return {
        "id": c["id"],
        "profile_id": c["profile_id"],
        "name": c["name"],
        "credential": masked,
        "route_ids": rids,
        "routes": names,
        "in_grace": bool(c.get("prev_credential")),
        "prev_expires_at": str(c["prev_expires_at"])
        if c.get("prev_expires_at") else None,
        "status": c["status"],
        "created_by": c["created_by"],
        "created_at": str(c["created_at"]),
        "updated_at": str(c["updated_at"]),
    }


def _write_key_auth(client: GatewayK8sClient, mutate) -> None:
    """读改写 key-auth WasmPlugin（缺失则按 02 §4 基准形态创建）。"""
    wasm = client.get_wasmplugin(KEY_AUTH_PLUGIN)
    if wasm is None:
        wasm = {
            "apiVersion": f"extensions.higress.io/{client._wasm_ver()}",
            "kind": "WasmPlugin",
            "metadata": {"name": KEY_AUTH_PLUGIN, "namespace": client.ns},
            "spec": {
                "defaultConfig": {
                    "consumers": [],
                    "global_auth": False,
                    "keys": ["x-higress-dummy-key"],
                },
                "url": "oci://higress-registry.cn-hangzhou.cr.aliyuncs.com"
                       "/plugins/key-auth:2.0.0",
            },
        }
    mutate(wasm)
    client.replace_wasmplugin(wasm)
    after = client.get_wasmplugin(KEY_AUTH_PLUGIN)
    if after is None:
        raise errors.DependencyError("key-auth 写入后复核失败")
    return


def list_consumers(
    db: Database, profile: dict, client: GatewayK8sClient | None = None,
) -> dict:
    rows = [_consumer_out(db, c) for c in dao.list_consumers(db, profile["id"])]
    live: list[str] | None = None
    if client is not None:
        wasm = client.get_wasmplugin(KEY_AUTH_PLUGIN)
        if wasm is not None:
            live = resources.consumer_names(wasm)
    return {"consumers": rows, "liveConsumers": live}


def create_consumer(
    db: Database, profile: dict, *, name: str, credential: str | None = None,
    route_names: list[str] | None = None, operator: str,
    client: GatewayK8sClient | None = None,
) -> dict:
    """创建 consumer（credential 缺省自动生成；显式传入供导入用）。。

    返回体一次性回显完整明文密钥（仅此一次；此后列表只显尾 4 位）。"""
    if client is None:
        from .k8s_client import client_for_profile
        client = client_for_profile(profile)
    crypto.assert_master_key_configured()  # fail-secure：无主密钥不可写
    if dao.get_consumer_by_name(db, profile["id"], name):
        raise errors.ConflictError(f"consumer 已存在: {name}")
    routes: list[dict] = []
    for rn in route_names or []:
        r = dao.get_route_by_name(db, profile["id"], rn)
        if not r:
            raise errors.NotFoundError(f"路由不存在: {rn}")
        routes.append(r)

    plaintext = credential or crypto.generate_credential()
    entry = resources.build_consumer_entry(name, plaintext)
    try:
        _write_key_auth(
            client, lambda w: resources.upsert_consumer(w, entry),
        )
    except errors.EAgentError:
        dao.record_audit(
            db, profile_id=profile["id"], resource_type="wasmplugin",
            action="create", payload_before=None,
            payload_after={"consumer": name}, result="failed",
            operator=operator,
        )
        raise
    row = dao.create_consumer(
        db, profile_id=profile["id"], name=name,
        credential=crypto.encrypt_credential(plaintext),
        route_ids=[r["id"] for r in routes], created_by=operator,
    )
    for r in routes:  # 绑定后联动重写路由 CM（allowedConsumers）
        route_mod.refresh_route_cm(db, profile, r, client=client)
    dao.record_audit(
        db, profile_id=profile["id"], resource_type="wasmplugin",
        action="create", payload_before=None,
        payload_after={"consumer": _consumer_out(db, row)},
        result="success", operator=operator,
    )
    out = _consumer_out(db, row)
    out["credential"] = plaintext  # 仅创建响应回显一次完整密钥
    return out


def delete_consumer(
    db: Database, profile: dict, name: str, *, operator: str,
    client: GatewayK8sClient | None = None,
) -> dict:
    if client is None:
        from .k8s_client import client_for_profile
        client = client_for_profile(profile)
    c = dao.get_consumer_by_name(db, profile["id"], name)
    if not c:
        raise errors.NotFoundError(f"consumer 不存在: {name}")
    before = _consumer_out(db, c)
    _write_key_auth(client, lambda w: resources.remove_consumer(w, name))
    # 从绑定路由的 allowedConsumers 中摘除（重写相关 CM）
    touched = []
    for rid in dao.jload(c.get("route_ids")) or []:
        r = dao.get_route(db, rid)
        if r:
            route_mod.refresh_route_cm(db, profile, r, client=client)
            touched.append(r["name"])
    dao.delete_consumer(db, c["id"])
    dao.record_audit(
        db, profile_id=profile["id"], resource_type="wasmplugin",
        action="delete", payload_before=before,
        payload_after={"removed": name, "refreshedRoutes": touched},
        result="success", operator=operator,
    )
    return {"deleted": name, "refreshedRoutes": touched}


def rotate_consumer(
    db: Database, profile: dict, name: str, *, operator: str,
    grace_seconds: int | None = None,
    client: GatewayK8sClient | None = None,
) -> dict:
    """轮换密钥：写新 key（credentials 同时含新旧，宽限期内双 key 有效）→
    到期由 sweep 下线旧 key（T13）。返回体一次性回显新明文密钥。"""
    if client is None:
        from .k8s_client import client_for_profile
        client = client_for_profile(profile)
    crypto.assert_master_key_configured()
    c = dao.get_consumer_by_name(db, profile["id"], name)
    if not c:
        raise errors.NotFoundError(f"consumer 不存在: {name}")
    grace = (config.settings.gateway_key_grace_seconds
             if grace_seconds is None else grace_seconds)
    old_plain = crypto.decrypt_credential(bytes(c["credential"]))
    new_plain = crypto.generate_credential()
    expires = now() + timedelta(seconds=grace)
    entry = resources.build_consumer_entry(name, new_plain)
    # 宽限期内 credentials 含新旧两把（02 §4 credentials 为数组）
    entry["credentials"].append(f"Bearer {old_plain}")
    try:
        _write_key_auth(
            client, lambda w: resources.upsert_consumer(w, entry),
        )
    except errors.EAgentError:
        dao.record_audit(
            db, profile_id=profile["id"], resource_type="wasmplugin",
            action="update", payload_before=_consumer_out(db, c),
            payload_after={"consumer": name, "rotate": True},
            result="failed", operator=operator,
        )
        raise
    updated = dao.update_consumer(
        db, c["id"], credential=crypto.encrypt_credential(new_plain),
        prev_credential=c["credential"], prev_expires_at=expires,
    )
    dao.record_audit(
        db, profile_id=profile["id"], resource_type="wasmplugin",
        action="update", payload_before=_consumer_out(db, c),
        payload_after={
            "consumer": name, "rotate": True,
            "prevExpiresAt": str(expires),
        },
        result="success", operator=operator,
    )
    out = _consumer_out(db, updated)
    out["credential"] = new_plain  # 仅轮换响应回显一次新密钥
    out["graceSeconds"] = grace
    return out


def sweep_expired_grace(
    db: Database, client_factory=None,
) -> list[dict]:
    """宽限期到期下线旧 key（后台循环 + 惰性调用双入口，03 T13）。"""
    swept: list[dict] = []
    by_profile: dict[int, list[dict]] = {}
    for c in dao.expired_grace_consumers(db):
        by_profile.setdefault(c["profile_id"], []).append(c)
    for profile_id, consumers in by_profile.items():
        profile = dao.get_profile(db, profile_id)
        if not profile:
            continue
        try:
            if client_factory is not None:
                client = client_factory(profile)
            else:
                from .k8s_client import client_for_profile
                client = client_for_profile(profile)
            for c in consumers:
                plain = crypto.decrypt_credential(bytes(c["credential"]))
                entry = resources.build_consumer_entry(c["name"], plain)
                _write_key_auth(
                    client, lambda w, e=entry: resources.upsert_consumer(w, e),
                )
                dao.update_consumer(db, c["id"], clear_prev=True)
                dao.record_audit(
                    db, profile_id=profile_id, resource_type="wasmplugin",
                    action="update", payload_before=None,
                    payload_after={
                        "consumer": c["name"], "graceExpired": True,
                    },
                    result="success", operator="system",
                )
                swept.append(
                    {"profile_id": profile_id, "consumer": c["name"]},
                )
        except errors.EAgentError as exc:
            dao.record_audit(
                db, profile_id=profile_id, resource_type="wasmplugin",
                action="update", payload_before=None,
                payload_after={"graceSweep": True, "error": exc.code},
                result="failed", operator="system",
            )
    return swept
