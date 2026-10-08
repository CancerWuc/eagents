# -*- coding: utf-8 -*-
"""HIAGENT 来源服务的注册/探测/会话映射（hiagent-integration/01 §3.2/§3.3）。

设计要点：
- **注册进统一 Agent Catalog**（`eag_a2a_service_registration` +
  `source_type='HIAGENT'`），不另立目录——design-v2 §4.1「Agent Catalog 隐藏
  HiAgent 与第三方 A2A 来源差异」的原始意图；专属配置落 side 表
  `eag_hiagent_agent`（app_key AES-GCM 加密，永不回显）。
- HIAGENT 来源**构造即 COMPATIBLE**（适配器是平台自有，无远端协议漂移）；
  Card 由平台生成（`card.py`），注册时落一条合成 Card Revision，让既有
  快照/digest 工具面原样可用。
- 健康探测 = 连通性测试（create_conversation + update_conversation + 发「你好」，对齐
  qagent_manager/hiagent_test.py），复用 HEALTH 四态与观测表。
- slug **跨表唯一**：与总控（eag_master_agent）互查，避免两实体抢同一
  `/a2a/{slug}` 挂载点。
- 会话映射（contextId ↔ AppConversationID）落 MySQL（eagent 无 Redis，
  不新引依赖）；TTL 2 天惰性过期（对齐 qagent 渠道口径）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from google.protobuf.json_format import MessageToDict
from .query_extends import EMPTY_SCHEMA, validate_schema, validate_values

from ..catalog import service as catalog
from ..persistence.db import Database, new_id, now

logger = logging.getLogger("eagent.hiagent.service")

SOURCE_TYPE = "HIAGENT"

# 会话映射 TTL（秒）；过期视为不存在，下次调用重建
CONVERSATION_TTL_S = 2 * 24 * 3600
# 连通性测试读超时（秒）——远小于 300s 常规读超时，注册/探测不挂人
_PROBE_TIMEOUT_S = 30.0


class HiagentRegistrationError(catalog.RegistrationError):
    """HIAGENT 注册失败（路由层复用 catalog 错误映射）。"""


class HiagentRegistrationValidationError(HiagentRegistrationError):
    """HIAGENT 注册参数/本地契约校验失败（HTTP 400）。"""


def assert_slug_free(db: Database, slug: str) -> None:
    """slug 跨表唯一（注册表 + 总控表双向占用即拒）。"""
    if db.query_one(
        "SELECT service_id FROM eag_a2a_service_registration WHERE slug=:s",
        {"s": slug},
    ):
        raise catalog.SlugConflictError(f"slug 已存在（服务注册表）: {slug}")
    if db.query_one(
        "SELECT master_id FROM eag_master_agent WHERE slug=:s", {"s": slug},
    ):
        raise catalog.SlugConflictError(f"slug 已存在（Agent Team）: {slug}")


def _decrypt_app_key(row: dict) -> str:
    blob = row.get("app_key_enc")
    if not blob:
        return ""
    if isinstance(blob, str):
        # VARBINARY 经部分驱动会按 latin-1 解成 str，先还原成原始字节
        blob = blob.encode("latin-1")
    from ..gateway.crypto import decrypt_credential

    try:
        return decrypt_credential(bytes(blob))
    except (ValueError, TypeError):
        return ""


def get_hiagent_config(db: Database, service_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_hiagent_agent WHERE service_id=:sid",
        {"sid": service_id},
    )


def build_client(db: Database, service_id: str, *, timeout_s: float | None = None):
    """按注册行构建协议客户端（实例级 base_url/app_key 优先）。"""
    from .client import HiagentClient

    row = get_hiagent_config(db, service_id)
    from .card import parse_agent_config
    if "inputsSchema" not in parse_agent_config((row or {}).get("agent_config_json")):
        from .client import HiagentAPIError
        raise HiagentAPIError("HiAgent 注册配置无效，请按 A2A 1.0 契约重新注册")
    app_key = _decrypt_app_key(row) if row else ""
    base_url = (row or {}).get("base_url") if row else None
    return HiagentClient(
        base_url=base_url, api_key=app_key or None, timeout_s=timeout_s,
    )


def _resolve_base_url(instance_base_url: str | None) -> str:
    from .client import resolve_base_url

    return resolve_base_url(instance_base_url)


def _registration_base_url(instance_base_url: str | None) -> str:
    """写入 agent_card_url 的具体地址（复用为 hiagent base_url 字段）。

    实例级显式指定用原值；否则用解析链结果（全局 env/默认），保证探测、
    调用与展示读到同一个值。"""
    return _resolve_base_url(instance_base_url)


async def _connectivity_test(
    db: Database, service_id: str,
) -> tuple[str, int, str]:
    """连通性测试：create_conversation + 发「你好」（对齐 hiagent_test.py）。

    返回 (health, latency_ms, detail)，health ∈ catalog.HEALTH。"""
    from ..errors import EAgentError

    client = build_client(db, service_id, timeout_s=_PROBE_TIMEOUT_S)
    from .card import parse_agent_config
    cfg = parse_agent_config((get_hiagent_config(db, service_id) or {}).get("agent_config_json"))
    test = cfg.get("connectionTest", {"query": "你好", "inputs": {}})
    started = time.perf_counter()
    try:
        conv_id = await client.create_conversation(
            inputs=test["inputs"], cap_user_name="eagent-probe",
            real_name="eagent 连通性测试",
        )
        await client.update_conversation(
            conv_id, inputs=test["inputs"], cap_user_name="eagent-probe",
            real_name="eagent 连通性测试", conversation_name="eAgent 连通性测试",
        )
        payload = await client.chat_blocking(
            conv_id, test["query"], cap_user_name="eagent-probe",
        )
        answer = str(payload.get("answer") or "")
        latency_ms = round((time.perf_counter() - started) * 1000)
        health = (
            "DEGRADED" if latency_ms >= catalog.HEALTH_DEGRADED_LATENCY_MS
            else "HEALTHY"
        )
        detail = f"连通性测试通过（conversation={conv_id[:8]}…，answer {len(answer)} 字）"
        return health, latency_ms, detail
    except EAgentError as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)
        return "UNREACHABLE", latency_ms, str(exc)


async def _probe_configuration(base_url: str, app_key: str | None, test: dict, *, failure_note: str) -> None:
    from ..errors import EAgentError
    from .client import HiagentClient

    probe_client = HiagentClient(
        base_url=base_url or None, api_key=app_key or None,
        timeout_s=_PROBE_TIMEOUT_S,
    )
    try:
        conv_id = await probe_client.create_conversation(
            inputs=test["inputs"], cap_user_name="eagent-probe",
            real_name="eagent 连通性测试",
        )
        await probe_client.update_conversation(
            conv_id, inputs=test["inputs"], cap_user_name="eagent-probe",
            real_name="eagent 连通性测试", conversation_name="eAgent 连通性测试",
        )
        await probe_client.chat_blocking(
            conv_id, test["query"], cap_user_name="eagent-probe",
        )
        stream_conv = await probe_client.create_conversation(
            inputs=test["inputs"], cap_user_name="eagent-probe",
            real_name="eAgent probe",
        )
        await probe_client.update_conversation(
            stream_conv, inputs=test["inputs"], cap_user_name="eagent-probe",
            real_name="eAgent probe", conversation_name="eAgent 连通性测试",
        )
        from .normalizer import normalize_stream
        await normalize_stream(probe_client.chat_stream(
            stream_conv, test["query"], cap_user_name="eagent-probe",
        ))
    except EAgentError as exc:
        raise HiagentRegistrationError(
            f"连通性测试失败，{failure_note}: {exc}",
        ) from exc

async def register_hiagent_agent(
    db: Database, *, slug: str, display_name: str,
    description: str | None = None, base_url: str | None = None,
    app_key: str | None = None, skills: list[dict] | None = None,
    inputs_schema: dict | None = None,
    connection_test: dict | None = None,
    created_by: str = "admin",
) -> dict:
    """注册 HIA agent：目录行 + 合成 Card Revision + side 表 + 审计。

    注册前验证 Schema、blocking 与 streaming；测试输入和参数契约存入 agent_config_json。"""
    assert_slug_free(db, slug)
    # 实例级 AppKey 加密依赖网关主密钥——先于连通性测试校验（fail-secure，
    # 不做无谓的 hiagent 调用；错误在路由层映射为 400 配置指引）
    if app_key:
        from ..gateway.crypto import assert_master_key_configured

        assert_master_key_configured()
    effective_base_url = _registration_base_url(base_url)
    agent_config: dict = {"skills": skills or []}
    schema = inputs_schema if inputs_schema is not None else EMPTY_SCHEMA
    test = (
        connection_test
        if connection_test is not None
        else {"query": "你好", "inputs": {}}
    )
    try:
        validate_schema(schema)
        if set(test) != {"query", "inputs"}:
            raise ValueError("connectionTest must contain query and inputs")
        validate_values(test["inputs"], schema)
        if not isinstance(test["query"], str) or not test["query"].strip():
            raise ValueError("connectionTest.query is required")
    except (ValueError, KeyError) as exc:
        raise HiagentRegistrationValidationError(str(exc)) from exc
    agent_config["inputsSchema"] = schema
    agent_config["connectionTest"] = test
    if description:
        agent_config["description"] = description

    # 合成 Card（不拉取远端）并自校验契约——平台生成的 Card 必须能过
    # 与 A2A_REMOTE 注册同一套 agent-card.v1 校验（fail-closed 不变量）
    from .card import build_hiagent_card, parse_agent_config

    card = build_hiagent_card(
        {"slug": slug, "display_name": display_name,
         "description": description},
        parse_agent_config(json.dumps(agent_config, ensure_ascii=False)),
    )
    card_json = MessageToDict(card)
    violations = catalog.validate(card_json, catalog.AGENT_CARD_SCHEMA)
    if violations:
        raise HiagentRegistrationError(
            f"生成的 Agent Card 未通过契约校验: {violations[0]}",
        )
    normalized = catalog.normalize_card(card_json)
    compat_reason = (
        "HIAGENT 来源：适配器为平台自有，Card 由平台生成（无远端协议漂移）"
    )

    await _probe_configuration(effective_base_url, app_key, test, failure_note="未注册")

    ts = now()
    service_id = new_id("svc_")
    revision_id = new_id("rev_")
    db.execute(
        """
        INSERT INTO eag_a2a_card_revision
            (revision_id, service_id, protocol_version, card_json,
             normalized_json, content_digest, compatibility, fetched_at)
        VALUES (:rid, :sid, :pv, :cj, :nj, :dg, 'COMPATIBLE', :fa)
        """,
        {"rid": revision_id, "sid": service_id,
         "pv": "1.0",
         "cj": json.dumps(card_json, ensure_ascii=False),
         "nj": json.dumps(normalized, ensure_ascii=False),
         "dg": catalog.card_digest(normalized), "fa": ts},
    )
    app_key_enc = None
    if app_key:
        from ..gateway.crypto import encrypt_credential

        app_key_enc = encrypt_credential(app_key)
    db.execute(
        """
        INSERT INTO eag_a2a_service_registration
            (service_id, slug, display_name, agent_card_url, auth_secret_ref,
             tags, enabled, compatibility, compatibility_reason,
             current_revision_id, source_type, created_at, updated_at)
        VALUES (:sid, :slug, :dn, :url, NULL, NULL, 1, 'COMPATIBLE',
                :reason, :rid, 'HIAGENT', :ts, :ts)
        """,
        {"sid": service_id, "slug": slug,
         "dn": display_name or card_json.get("name") or slug,
         "url": effective_base_url, "reason": compat_reason,
         "rid": revision_id, "ts": ts},
    )
    db.execute(
        """
        INSERT INTO eag_hiagent_agent
            (service_id, base_url, app_key_enc, agent_config_json,
             created_by, created_at, updated_at)
        VALUES (:sid, :url, :key, :cfg, :u, :ts, :ts)
        """,
        {"sid": service_id, "url": base_url, "key": app_key_enc,
         "cfg": json.dumps(agent_config, ensure_ascii=False),
         "u": created_by, "ts": ts},
    )
    db.execute(
        "INSERT INTO eag_audit_event (audit_id, cap_user_name, action,"
        " resource_type, resource_id, detail, created_at)"
        " VALUES (:a, :u, 'service.register', 'service', :sid, :d, :ts)",
        {"a": new_id("aud_"), "u": created_by, "sid": service_id,
         "d": json.dumps({
             "slug": slug, "source_type": SOURCE_TYPE, "compatibility": "COMPATIBLE",
         }, ensure_ascii=False), "ts": ts},
    )
    db.commit()
    logger.info(
        "registered HIAGENT agent slug=%s service_id=%s", slug, service_id,
    )
    return catalog.get_service(db, service_id)


def synthetic_release(db: Database, service: dict) -> dict:
    """HIAGENT 绑定的合成 release（跳过远端 Release/Installation 链）。

    endpoint/digest 来自注册时快照（合成 Card Revision）；Team 装配据此
    生成 HiAgentTool，A2ATool 的 release 字段口径保持形状兼容。"""
    rev = catalog.current_card(db, service["service_id"])
    digest = rev["content_digest"] if rev else ""
    version = f"hia-{digest[:8]}" if digest else "hia-unversioned"
    return {
        "release_id": f"synthetic:{service['service_id']}",
        "service_id": service["service_id"],
        "agent_version": version,
        "state": "PUBLISHED",
    }


async def probe_service(db: Database, service_id: str) -> dict | None:
    """HIAGENT 健康探测（连通性测试 + 观测落库）。非 HIAGENT 行返回 None。"""
    svc = catalog.get_service(db, service_id)
    if not svc or svc.get("source_type") != SOURCE_TYPE:
        return None
    health, latency_ms, detail = await _connectivity_test(db, service_id)
    return catalog.record_health(db, service_id, health, latency_ms, detail)


def refresh_hiagent_card(db: Database, service_id: str, *, commit: bool = True) -> dict | None:
    """按 side 表当前配置重新生成合成 Card Revision（:refresh 端点用）。

    Card 由平台生成、无远端可拉：刷新 = 用最新 agent_config（skills 等）
    重建快照，digest 变化才落新 Revision。非 HIAGENT 行返回 None。"""
    svc = catalog.get_service(db, service_id)
    if not svc or svc.get("source_type") != SOURCE_TYPE:
        return None
    from .card import build_hiagent_card, parse_agent_config

    side = get_hiagent_config(db, service_id) or {}
    agent_config = parse_agent_config(side.get("agent_config_json"))
    card = build_hiagent_card(svc, agent_config)
    card_json = MessageToDict(card)
    violations = catalog.validate(card_json, catalog.AGENT_CARD_SCHEMA)
    if violations:
        raise HiagentRegistrationValidationError("生成的 Agent Card 未通过契约校验")
    normalized = catalog.normalize_card(card_json)
    digest = catalog.card_digest(normalized)
    last = db.query_one(
        "SELECT content_digest FROM eag_a2a_card_revision WHERE revision_id=:rid",
        {"rid": svc["current_revision_id"]},
    ) if svc["current_revision_id"] else None
    ts = now()
    if not last or last["content_digest"] != digest:
        revision_id = new_id("rev_")
        db.execute(
            """
            INSERT INTO eag_a2a_card_revision
                (revision_id, service_id, protocol_version, card_json,
                 normalized_json, content_digest, compatibility, fetched_at)
            VALUES (:rid, :sid, :pv, :cj, :nj, :dg, 'COMPATIBLE', :fa)
            """,
            {"rid": revision_id, "sid": service_id,
             "pv": "1.0",
             "cj": json.dumps(card_json, ensure_ascii=False),
             "nj": json.dumps(normalized, ensure_ascii=False),
             "dg": digest, "fa": ts},
        )
        db.execute(
            "UPDATE eag_a2a_service_registration SET current_revision_id=:rid,"
            " updated_at=:ts WHERE service_id=:sid",
            {"rid": revision_id, "ts": ts, "sid": service_id},
        )
    if commit:
        db.commit()
    return catalog.get_service(db, service_id)


async def update_hiagent_agent(db: Database, service_id: str, changes: dict, *, updated_by: str) -> dict:
    """Validate and probe candidate settings before atomically replacing the configuration."""
    from .card import parse_agent_config
    from ..gateway.crypto import encrypt_credential, decrypt_credential
    from ..errors import NotFoundError, DependencyError

    svc = catalog.get_service(db, service_id)
    side = get_hiagent_config(db, service_id)
    if not svc or not side:
        raise NotFoundError("HiAgent 配置不存在")
    config = parse_agent_config(side.get("agent_config_json"))
    for field in ("description", "skills", "inputsSchema", "connectionTest"):
        if field in changes:
            config[field] = changes[field]
    schema = config.get("inputsSchema", EMPTY_SCHEMA)
    test = config.get("connectionTest", {"query": "你好", "inputs": {}})
    try:
        validate_schema(schema)
        if not isinstance(test, dict) or set(test) != {"query", "inputs"}:
            raise ValueError("connectionTest must contain query and inputs")
        validate_values(test["inputs"], schema)
        if not isinstance(test["query"], str) or not test["query"].strip():
            raise ValueError("connectionTest.query is required")
        if not isinstance(config.get("skills", []), list) or any(not isinstance(s, dict) for s in config.get("skills", [])):
            raise ValueError("skills must be a list of objects")
        name = changes.get("displayName", svc["display_name"])
        if not isinstance(name, str) or not name.strip():
            raise ValueError("服务名称不能为空")
    except (ValueError, KeyError, TypeError) as exc:
        raise HiagentRegistrationValidationError(str(exc)) from exc

    base_url = changes.get("baseUrl", side.get("base_url")) or None
    effective_url = _registration_base_url(base_url)
    key_blob = side.get("app_key_enc")
    replacement_key = (changes.get("appKey") or "").strip()
    if changes.get("clearCredential"):
        key_blob, key = None, None
    elif replacement_key:
        key_blob, key = encrypt_credential(replacement_key), replacement_key
    elif key_blob:
        try:
            key = decrypt_credential(bytes(key_blob))
        except (ValueError, TypeError) as exc:
            raise DependencyError("AppKey 解密失败，请重新填写 AppKey") from exc
    else:
        key = None
    await _probe_configuration(effective_url, key, test, failure_note="未保存，原配置保持不变")

    ts = now()
    db.execute(
        "UPDATE eag_hiagent_agent SET base_url=:url, app_key_enc=:key, agent_config_json=:cfg, updated_at=:ts WHERE service_id=:sid",
        {"url": base_url, "key": key_blob, "cfg": json.dumps(config, ensure_ascii=False), "ts": ts, "sid": service_id},
    )
    db.execute(
        "UPDATE eag_a2a_service_registration SET display_name=:name, agent_card_url=:url, updated_at=:ts WHERE service_id=:sid",
        {"name": name.strip(), "url": effective_url, "ts": ts, "sid": service_id},
    )
    refresh_hiagent_card(db, service_id, commit=False)
    if base_url != side.get("base_url") or key_blob != side.get("app_key_enc"):
        # Conversations belong to the old endpoint/application and cannot be reused there.
        db.execute("DELETE FROM eag_hiagent_conversation WHERE service_id=:sid", {"sid": service_id})
    db.execute(
        "INSERT INTO eag_audit_event (audit_id, cap_user_name, action, resource_type, resource_id, detail, created_at)"
        " VALUES (:a, :u, 'service.update', 'service', :sid, :d, :ts)",
        {"a": new_id("aud_"), "u": updated_by, "sid": service_id,
         "d": json.dumps({"source_type": SOURCE_TYPE, "fields": sorted(changes)}, ensure_ascii=False), "ts": ts},
    )
    db.commit()
    return catalog.get_service(db, service_id)


def delete_hiagent_agent(
    db: Database, service_id: str, *, force: bool = False, deleted_by: str = "admin",
) -> dict:
    """删除 HIAGENT 注册，复用公共授权/绑定清理与删除安全防线。"""
    svc = catalog.get_service(db, service_id)
    if not svc:
        raise LookupError(f"服务不存在: {service_id}")
    if svc.get("source_type") != SOURCE_TYPE:
        raise ValueError("仅 HIAGENT 来源支持此删除入口")
    return catalog.delete_service(
        db, service_id, confirm=svc["slug"], force=force, deleted_by=deleted_by,
    )


# ---------------------------------------------------------------------------
# 会话映射: A2A contextId ↔ hiagent AppConversationID（MySQL，TTL 2 天）
# ---------------------------------------------------------------------------
def identity_digest(cap_user_name, real_name) -> str:
    return hashlib.sha256(json.dumps([cap_user_name, real_name], ensure_ascii=False).encode()).hexdigest()


def configuration_version(side: dict) -> str:
    """Fingerprint effective configuration; no credential is persisted in the reference."""
    from ..config import settings
    value = {
        "base": side.get("base_url") or settings.hiagent_url,
        "key": bytes(side["app_key_enc"]).hex() if side.get("app_key_enc") else settings.hiagent_api_key,
        "config": side.get("agent_config_json"), "updated": str(side.get("updated_at", "")),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def get_conversation(db: Database, service_id: str, context_id: str, subject_id: str, identity_digest: str) -> str | None:
    row = db.query_one(
        "SELECT conversation_id, updated_at, identity_digest FROM eag_hiagent_conversation"
        " WHERE context_id=:ctx AND service_id=:sid AND subject_id=:subject",
        {"ctx": context_id, "sid": service_id, "subject": subject_id},
    )
    if not row or row["identity_digest"] != identity_digest:
        return None
    from datetime import datetime as _dt

    updated = row["updated_at"]
    if isinstance(updated, str):
        try:
            updated = _dt.fromisoformat(updated)
        except ValueError:
            return None
    if not isinstance(updated, _dt) or \
            time.time() - updated.timestamp() > CONVERSATION_TTL_S:
        return None
    # 命中即续期（惰性 TTL）
    db.execute(
        "UPDATE eag_hiagent_conversation SET updated_at=:ts"
        " WHERE context_id=:ctx AND service_id=:sid AND subject_id=:subject",
        {"ts": now(), "ctx": context_id, "sid": service_id, "subject": subject_id},
    )
    db.commit()
    return row["conversation_id"]


def save_conversation(
    db: Database, service_id: str, context_id: str, conversation_id: str, subject_id: str, identity_digest: str,
) -> None:
    db.execute(
        "INSERT INTO eag_hiagent_conversation"
        " (context_id, service_id, conversation_id, subject_id, identity_digest, updated_at)"
        " VALUES (:ctx, :sid, :conv, :subject, :digest, :ts)"
        " ON DUPLICATE KEY UPDATE conversation_id=IF(identity_digest<>VALUES(identity_digest) OR updated_at<DATE_SUB(NOW(), INTERVAL 2 DAY),VALUES(conversation_id),conversation_id), identity_digest=VALUES(identity_digest),"
        " updated_at=VALUES(updated_at)",
        {"ctx": context_id, "sid": service_id, "conv": conversation_id,
         "ts": now(), "subject": subject_id, "digest": identity_digest},
    )
    db.commit()


async def get_or_create_conversation(
    db: Database, service_id: str, context_id: str, *,
    inputs: dict | None = None, cap_user_name: str | None,
    real_name: str | None = None,
) -> str:
    """多轮复用同一 AppConversationID；映射缺失/过期则重建并落库。"""
    if not cap_user_name:
        raise ValueError("Authenticated subject required")
    digest = identity_digest(cap_user_name, real_name)
    existing = get_conversation(db, service_id, context_id, cap_user_name, digest)
    if existing:
        return existing
    client = build_client(db, service_id)
    conv_id = await client.create_conversation(
        inputs=inputs, cap_user_name=cap_user_name, real_name=real_name,
    )
    save_conversation(db, service_id, context_id, conv_id, cap_user_name, digest)
    winner = get_conversation(db, service_id, context_id, cap_user_name, digest)
    if not winner:
        from .client import HiagentAPIError
        raise HiagentAPIError("会话身份已变化，请重新发起请求")
    return winner


def snapshot_digest(db: Database, service_id: str) -> str:
    """注册时合成 Card 快照摘要（合成 release 版本号来源）。"""
    rev = catalog.current_card(db, service_id)
    if not rev:
        return hashlib.sha256(service_id.encode()).hexdigest()
    return rev["content_digest"]
