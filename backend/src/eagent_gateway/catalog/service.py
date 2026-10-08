# -*- coding: utf-8 -*-
"""能力②: A2A 服务注册 —— Card 拉取/校验/快照、兼容性、启停、健康观测。"""
import base64
import hashlib
import json
import logging
import time
from urllib.parse import urlparse

from ..contracts import AGENT_CARD_SCHEMA, validate
from ..persistence.db import Database, new_id, now

logger = logging.getLogger(__name__)

CARD_PATHS = ("/.well-known/agent-card.json",)
MAX_CARD_BYTES = 512 * 1024
CARD_FETCH_TIMEOUT = 15.0
HEALTH_DEGRADED_LATENCY_MS = 2000

COMPATIBILITY = ("VERIFYING", "COMPATIBLE", "INCOMPATIBLE")
HEALTH = ("UNKNOWN", "HEALTHY", "DEGRADED", "UNREACHABLE")


class RegistrationError(ValueError):
    pass


class SlugConflictError(RegistrationError):
    """slug 与既有注册冲突（路由层映射为 CONFLICT/409）。"""


class CardContractError(RegistrationError):
    """远端 Card 不符合 agent-card.v1 契约（fail-closed，不允许注册）。"""


class CardFetchError(RegistrationError):
    """Card 拉取失败（路由层映射为 DEPENDENCY_ERROR/502）。"""


class ServiceInUseError(RegistrationError):
    """服务正在被总控绑定或处于活跃路由中，不可删除（映射为 409 CONFLICT）。"""


# ---------------------------------------------------------------------------
# SSRF 校验（§10.2: 协议/主机/私网段）
# ---------------------------------------------------------------------------
async def _validate_card_url(agent_card_url: str) -> str:
    """注册前预检（友好报错）；实际连接由 SSRFGuardAsyncTransport
    在同一处完成解析+校验+固定 IP（无 TOCTOU，见 security/pinned_http.py）。

    ⚠️ 必须用 `check_hostname_async`：本函数只在协程（fetch_agent_card）里调用，
    同步版的 `socket.getaddrinfo` 会阻塞事件循环——健康探测每轮顺序探测全部
    已启用服务，主机名不可达时单次解析约 2 秒，16 个服务足以让整个进程
    （含管理台所有请求）冻住几十秒。校验语义与同步版完全一致。"""
    from ..config import settings
    from ..security.pinned_http import AddressBlockedError, check_hostname_async

    parsed = urlparse(agent_card_url)
    if parsed.scheme != "http" and parsed.scheme != "https":
        raise RegistrationError("agentCardUrl 仅支持 http/https")
    host = parsed.hostname or ""
    if not host:
        raise RegistrationError("agentCardUrl 缺少主机名")
    try:
        await check_hostname_async(
            host, allow_private=settings.allow_private_addresses,
        )
    except AddressBlockedError as exc:
        raise RegistrationError(str(exc)) from exc
    return agent_card_url


# ---------------------------------------------------------------------------
# Card 拉取 + 规范化 + 兼容性
# ---------------------------------------------------------------------------
async def fetch_agent_card(agent_card_url: str) -> tuple[dict, str]:
    """拉取 Agent Card，返回 (card_json 原文, 实际命中的 base_url)。

    出站连接经 SSRFGuardAsyncTransport：解析、校验、固定 IP 连接
    在同一处完成，阻断 DNS-rebinding/TOCTOU。"""
    from ..security.pinned_http import AddressBlockedError, guarded_async_client

    await _validate_card_url(agent_card_url)
    base = agent_card_url.split("/.well-known/")[0].rstrip("/")
    last_error: Exception | None = None
    async with guarded_async_client(timeout=CARD_FETCH_TIMEOUT) as client:
        for path in CARD_PATHS:
            try:
                resp = await client.get(f"{base}{path}")
                if resp.status_code == 200 and len(resp.content) <= MAX_CARD_BYTES:
                    card = resp.json()
                    # P0-1 契约优先: Card 必须通过 agent-card.v1 校验
                    # （fail-closed: 结构不合规直接拒绝，不进入兼容性判定）
                    violations = validate(card, AGENT_CARD_SCHEMA)
                    if violations:
                        raise CardContractError(
                            f"Agent Card 不符合契约 {AGENT_CARD_SCHEMA}:"
                            f" {violations[0]}",
                        )
                    return card, base
                last_error = RuntimeError(
                    f"{path} -> HTTP {resp.status_code}",
                )
            except AddressBlockedError as exc:
                raise RegistrationError(str(exc)) from exc
            except CardContractError:
                raise  # 契约违规不允许被当作"换个路径再试试"的普通失败
            except Exception as exc:  # noqa: BLE001
                last_error = exc
    raise CardFetchError(f"Agent Card 拉取失败: {last_error}")


def normalize_card(card: dict) -> dict:
    from ..a2a_server.protocol import parse_card, card_dict
    return card_dict(parse_card(card))


def check_compatibility(card: dict) -> tuple[str, str]:
    try:
        normalize_card(card)
    except (ValueError, TypeError) as exc:
        return "INCOMPATIBLE", str(exc)
    return "COMPATIBLE", "A2A 1.0 JSONRPC"


def card_digest(normalized: dict) -> str:
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode(),
    ).hexdigest()


# ---------------------------------------------------------------------------
# 服务级出站凭证（intranet-a2a-stream-adapter）
# ---------------------------------------------------------------------------
def _validate_credential_headers(headers: dict[str, str]) -> None:
    if not isinstance(headers, dict) or not headers:
        raise RegistrationError("出站凭证必须是非空 JSON 对象（header 名到值）")
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str) \
                or not name or not value:
            raise RegistrationError("出站凭证必须是 header 名到值的字符串 JSON 对象")


def encrypt_outbound_headers(headers: dict[str, str]) -> str:
    """出站凭证 headers（如 {"apikey": "..."}）AES-GCM 加密为 base64 文本。

    复用 external tools 的 gateway/crypto（主密钥 EAGENT_GATEWAY_MASTER_KEY，
    与网关开关无关，GATEWAY_ENABLED=0 也可用）。"""
    from ..gateway.crypto import encrypt_credential

    return base64.b64encode(
        encrypt_credential(json.dumps(headers, ensure_ascii=False)),
    ).decode("ascii")


def outbound_headers(db: Database, service_id: str) -> dict[str, str]:
    """读取并解密服务级出站凭证 headers（出站调用注入用）。

    - 空/未配置 → {}；
    - 非 base64 文本 → 旧格式占位（authSecretRef 历史明文，从未被读取），
      告警并视为未配置；
    - base64 但解密失败 → 主密钥不匹配/数据损坏，fail-closed 抛错
      （静默无凭证出站只会换来含混的远端 401/404）；
    - 解密后不是字符串到字符串的 JSON 对象 → 配置损坏，fail-closed。"""
    from ..errors import DependencyError
    from ..gateway.crypto import decrypt_credential

    row = db.query_one(
        "SELECT auth_secret_ref FROM eag_a2a_service_registration"
        " WHERE service_id=:sid",
        {"sid": service_id},
    )
    blob = row.get("auth_secret_ref") if row else None
    if not blob:
        return {}
    try:
        block = base64.b64decode(blob, validate=True)
    except (ValueError, TypeError):
        logger.warning(
            "服务 %s 的 authSecretRef 为旧格式明文，忽略（不注入出站凭证）",
            service_id,
        )
        return {}
    try:
        plain = decrypt_credential(block)
        headers = json.loads(plain)
    except Exception as exc:  # noqa: BLE001
        raise DependencyError(
            "出站凭证解密失败（主密钥不匹配或数据损坏）",
        ) from exc
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k and v
        for k, v in headers.items()
    ):
        raise DependencyError("出站凭证必须是 header 名到值的字符串 JSON 对象")
    return headers


def update_outbound_headers(
    db: Database, service_id: str, headers: dict[str, str] | None,
    *, updated_by: str = "admin",
) -> dict:
    """更新服务级出站凭证；headers=None 表示清除。"""
    from ..errors import NotFoundError

    svc = get_service(db, service_id)
    if not svc:
        raise NotFoundError(f"服务不存在: {service_id}")
    blob = encrypt_outbound_headers(headers) if headers else None
    db.execute(
        "UPDATE eag_a2a_service_registration SET auth_secret_ref=:sec,"
        " updated_at=:ts WHERE service_id=:sid",
        {"sec": blob, "ts": now(), "sid": service_id},
    )
    db.execute(
        "INSERT INTO eag_audit_event (audit_id, cap_user_name, action, resource_type, resource_id, detail, created_at)"
        " VALUES (:a, :u, 'service.credential.update', 'service', :sid, :d, :ts)",
        {"a": new_id("aud_"), "u": updated_by, "sid": service_id,
         "d": json.dumps({"headers": sorted(headers) if headers else []},
                         ensure_ascii=False),
         "ts": now()},
    )
    db.commit()
    return get_service(db, service_id)


# ---------------------------------------------------------------------------
# 注册/查询/启停/刷新
# ---------------------------------------------------------------------------
async def register_service(
    db: Database,
    *,
    slug: str,
    display_name: str,
    agent_card_url: str,
    auth_secret_ref: dict[str, str] | None = None,
    tags: str | None = None,
    created_by: str = "admin",
) -> dict:
    if db.query_one("SELECT service_id FROM eag_a2a_service_registration WHERE slug=:s", {"s": slug}):
        raise SlugConflictError(f"slug 已存在: {slug}")
    # slug 跨表唯一（hiagent-integration/01 §3.3）：总控与注册服务共用
    # /a2a/{slug} 挂载命名空间，不允许跨表抢占
    if db.query_one(
        "SELECT master_id FROM eag_master_agent WHERE slug=:s", {"s": slug},
    ):
        raise SlugConflictError(f"slug 已存在（Agent Team）: {slug}")
    if auth_secret_ref:
        _validate_credential_headers(auth_secret_ref)
        try:
            sec_blob = encrypt_outbound_headers(auth_secret_ref)
        except Exception as exc:  # noqa: BLE001
            raise RegistrationError(
                "出站凭证加密失败: 未配置 EAGENT_GATEWAY_MASTER_KEY"
                "（base64 的 32 字节）",
            ) from exc
    else:
        sec_blob = None
    card, base = await fetch_agent_card(agent_card_url)
    normalized = normalize_card(card)
    compat, reason = check_compatibility(card)
    ts = now()
    revision_id = new_id("rev_")
    service_id = new_id("svc_")
    db.execute(
        """
        INSERT INTO eag_a2a_card_revision
            (revision_id, service_id, protocol_version, card_json,
             normalized_json, content_digest, compatibility, fetched_at)
        VALUES (:rid, :sid, :pv, :cj, :nj, :dg, :cp, :fa)
        """,
        {
            "rid": revision_id, "sid": service_id,
            "pv": "1.0",
            "cj": json.dumps(card, ensure_ascii=False),
            "nj": json.dumps(normalized, ensure_ascii=False),
            "dg": card_digest(normalized), "cp": compat, "fa": ts,
        },
    )
    db.execute(
        """
        INSERT INTO eag_a2a_service_registration
            (service_id, slug, display_name, agent_card_url, auth_secret_ref,
             tags, enabled, compatibility, compatibility_reason,
             current_revision_id,
             created_at, updated_at)
        VALUES (:sid, :slug, :dn, :url, :sec, :tags, 1, :cp, :reason,
                :rid, :ts, :ts)
        """,
        {
            "sid": service_id, "slug": slug, "dn": display_name or normalized["name"],
            "url": base, "sec": sec_blob, "tags": tags,
            "cp": compat, "reason": reason, "rid": revision_id, "ts": ts,
        },
    )
    db.execute(
        "INSERT INTO eag_audit_event (audit_id, cap_user_name, action, resource_type, resource_id, detail, created_at)"
        " VALUES (:a, :u, 'service.register', 'service', :sid, :d, :ts)",
        {"a": new_id("aud_"), "u": created_by, "sid": service_id,
         "d": json.dumps({"slug": slug, "compatibility": compat, "reason": reason}, ensure_ascii=False),
         "ts": ts},
    )
    db.commit()
    return get_service(db, service_id)


def get_service(db: Database, service_id: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_a2a_service_registration WHERE service_id=:sid",
        {"sid": service_id},
    )


def list_services(db: Database, visible_ids: list[str] | None = None) -> list[dict]:
    base_sql = """
        SELECT s.*, COALESCE(h.health, 'UNKNOWN') AS health,
               h.latency_ms AS health_latency_ms,
               h.detail AS health_detail,
               h.observed_at AS health_observed_at
          FROM eag_a2a_service_registration s
          LEFT JOIN eag_a2a_health_observation h
            ON h.observation_id = (
                SELECT h2.observation_id
                  FROM eag_a2a_health_observation h2
                 WHERE h2.service_id = s.service_id
                 ORDER BY h2.observed_at DESC, h2.observation_id DESC
                 LIMIT 1
            )
    """
    if visible_ids is None:
        return db.query_all(base_sql + " ORDER BY s.created_at DESC")
    if not visible_ids:
        return []
    placeholders = ", ".join(f":v{i}" for i in range(len(visible_ids)))
    params = {f"v{i}": v for i, v in enumerate(visible_ids)}
    return db.query_all(
        base_sql
        + f" WHERE s.service_id IN ({placeholders}) ORDER BY s.created_at DESC",
        params,
    )


async def refresh_card(db: Database, service_id: str) -> dict:
    svc = get_service(db, service_id)
    if not svc:
        raise RegistrationError(f"服务不存在: {service_id}")
    card, base = await fetch_agent_card(svc["agent_card_url"])
    normalized = normalize_card(card)
    compat, reason = check_compatibility(card)
    digest = card_digest(normalized)
    ts = now()
    last = db.query_one(
        "SELECT * FROM eag_a2a_card_revision WHERE revision_id=:rid",
        {"rid": svc["current_revision_id"]},
    ) if svc["current_revision_id"] else None
    if not last or last["content_digest"] != digest:
        revision_id = new_id("rev_")
        db.execute(
            """
            INSERT INTO eag_a2a_card_revision
                (revision_id, service_id, protocol_version, card_json,
                 normalized_json, content_digest, compatibility, fetched_at)
            VALUES (:rid, :sid, :pv, :cj, :nj, :dg, :cp, :fa)
            """,
            {"rid": revision_id, "sid": service_id,
             "pv": "1.0",
             "cj": json.dumps(card, ensure_ascii=False),
             "nj": json.dumps(normalized, ensure_ascii=False),
             "dg": digest, "cp": compat, "fa": ts},
        )
        db.execute(
            "UPDATE eag_a2a_service_registration SET current_revision_id=:rid,"
            " compatibility=:cp, compatibility_reason=:reason, updated_at=:ts"
            " WHERE service_id=:sid",
            {"rid": revision_id, "cp": compat, "reason": reason,
             "ts": ts, "sid": service_id},
        )
    else:
        db.execute(
            "UPDATE eag_a2a_service_registration SET compatibility=:cp,"
            " compatibility_reason=:reason, updated_at=:ts WHERE service_id=:sid",
            {"cp": compat, "reason": reason, "ts": ts, "sid": service_id},
        )
    db.commit()
    return get_service(db, service_id)


def set_enabled(db: Database, service_id: str, enabled: bool) -> dict:
    db.execute(
        "UPDATE eag_a2a_service_registration SET enabled=:e, updated_at=:ts"
        " WHERE service_id=:sid",
        {"e": 1 if enabled else 0, "ts": now(), "sid": service_id},
    )
    db.commit()
    return get_service(db, service_id)


def current_card(db: Database, service_id: str) -> dict | None:
    svc = get_service(db, service_id)
    if not svc or not svc["current_revision_id"]:
        return None
    rev = db.query_one(
        "SELECT normalized_json, card_json, fetched_at, content_digest"
        " FROM eag_a2a_card_revision WHERE revision_id=:rid",
        {"rid": svc["current_revision_id"]},
    )
    if not rev:
        return None
    rev["normalized"] = json.loads(rev.pop("normalized_json"))
    rev["card"] = json.loads(rev.pop("card_json"))
    return rev


def record_health(
    db: Database, service_id: str, health: str,
    latency_ms: int | None = None, detail: str | None = None,
) -> dict:
    if health not in HEALTH:
        raise ValueError(f"invalid health: {health}")
    observation = {
        "observation_id": new_id("obs_"),
        "service_id": service_id,
        "health": health,
        "latency_ms": latency_ms,
        "detail": detail,
        "observed_at": now(),
    }
    db.execute(
        "INSERT INTO eag_a2a_health_observation"
        " (observation_id, service_id, health, latency_ms, detail, observed_at)"
        " VALUES (:oid, :sid, :h, :lat, :d, :ts)",
        {"oid": observation["observation_id"], "sid": service_id,
         "h": health, "lat": latency_ms, "d": detail,
         "ts": observation["observed_at"]},
    )
    db.commit()
    return observation


async def probe_health(db: Database, service_id: str) -> dict | None:
    """探测 Agent Card 可达性并追加一条健康观测。"""
    svc = get_service(db, service_id)
    if not svc:
        return None
    started = time.perf_counter()
    try:
        await fetch_agent_card(svc["agent_card_url"])
        latency_ms = round((time.perf_counter() - started) * 1000)
        health = (
            "DEGRADED"
            if latency_ms >= HEALTH_DEGRADED_LATENCY_MS
            else "HEALTHY"
        )
        detail = "Agent Card 可访问"
    except CardContractError as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)
        health = "DEGRADED"
        detail = str(exc)
    except Exception as exc:  # noqa: BLE001
        latency_ms = round((time.perf_counter() - started) * 1000)
        health = "UNREACHABLE"
        detail = str(exc)
    return record_health(db, service_id, health, latency_ms, detail)


async def update_service(
    db: Database,
    service_id: str,
    *,
    display_name: str | None = None,
    tags: str | None = None,
    agent_card_url: str | None = None,
    outbound_headers: dict[str, str] | None = None,
    clear_credential: bool = False,
    updated_by: str = "admin",
) -> dict:
    """更新已注册服务的基本信息；若 agent_card_url 变更则重新拉取并校验 Card。"""
    from ..errors import NotFoundError

    svc = get_service(db, service_id)
    if not svc:
        raise NotFoundError(f"服务不存在: {service_id}")

    updates = {}
    audit_changes = {}

    if outbound_headers:
        updates["auth_secret_ref"] = encrypt_outbound_headers(outbound_headers)
        audit_changes["credential"] = "replaced"
    elif clear_credential:
        updates["auth_secret_ref"] = None
        audit_changes["credential"] = "cleared"

    if display_name is not None and display_name != svc["display_name"]:
        updates["display_name"] = display_name
        audit_changes["display_name"] = {"old": svc["display_name"], "new": display_name}

    if tags is not None and tags != (svc.get("tags") or ""):
        updates["tags"] = tags
        audit_changes["tags"] = {"old": svc.get("tags"), "new": tags}

    if agent_card_url is not None:
        url_clean = agent_card_url.strip()
        base_clean = url_clean.split("/.well-known/")[0].rstrip("/")
        # 若清洗后的 base 与当前不同，触发重拉与契约校验
        if base_clean != (svc.get("agent_card_url") or "").rstrip("/"):
            card, base = await fetch_agent_card(url_clean)
            normalized = normalize_card(card)
            compat, reason = check_compatibility(card)
            ts = now()
            revision_id = new_id("rev_")
            db.execute(
                """
                INSERT INTO eag_a2a_card_revision
                    (revision_id, service_id, protocol_version, card_json,
                     normalized_json, content_digest, compatibility, fetched_at)
                VALUES (:rid, :sid, :pv, :cj, :nj, :dg, :cp, :fa)
                """,
                {
                    "rid": revision_id, "sid": service_id,
                    "pv": "1.0",
                    "cj": json.dumps(card, ensure_ascii=False),
                    "nj": json.dumps(normalized, ensure_ascii=False),
                    "dg": card_digest(normalized), "cp": compat, "fa": ts,
                },
            )
            updates["agent_card_url"] = base
            updates["current_revision_id"] = revision_id
            updates["compatibility"] = compat
            updates["compatibility_reason"] = reason
            audit_changes["agent_card_url"] = {"old": svc.get("agent_card_url"), "new": base}
            audit_changes["revision_id"] = revision_id

    if updates:
        set_clauses = [f"{k}=:{k}" for k in updates]
        set_clauses.append("updated_at=:ts")
        sql = f"UPDATE eag_a2a_service_registration SET {', '.join(set_clauses)} WHERE service_id=:sid"
        db.execute(sql, {**updates, "sid": service_id, "ts": now()})
        db.execute(
            "INSERT INTO eag_audit_event (audit_id, cap_user_name, action, resource_type, resource_id, detail, created_at)"
            " VALUES (:a, :u, 'service.update', 'service', :sid, :d, :ts)",
            {"a": new_id("aud_"), "u": updated_by, "sid": service_id,
             "d": json.dumps(audit_changes, ensure_ascii=False), "ts": now()},
        )
        db.commit()

    return get_service(db, service_id)


def delete_service(
    db: Database,
    service_id: str,
    *,
    confirm: str,
    force: bool = False,
    deleted_by: str = "admin",
) -> dict:
    """级联删除服务（带三道安全防线、授权清理与运行期 remount）。"""
    from ..errors import NotFoundError
    from ..a2a_server import mounts

    svc = get_service(db, service_id)
    if not svc:
        raise NotFoundError(f"服务不存在: {service_id}")

    # 防线一：强确认防呆
    if confirm != svc["slug"]:
        raise RegistrationError(f"确认标识不符：必须输入服务标识 '{svc['slug']}'")

    # 防线二：活跃流量或灰度阻断
    policy = db.query_one(
        "SELECT policy_id FROM eag_traffic_policy WHERE service_id=:sid AND status='ACTIVE'",
        {"sid": service_id},
    )
    if policy:
        raise ServiceInUseError("该服务存在生效中的流量切换策略，请先在流量管理中删除或停用策略")

    canary = db.query_one(
        "SELECT canary_id FROM eag_canary_run WHERE service_id=:sid AND status='RUNNING'",
        {"sid": service_id},
    )
    if canary:
        raise ServiceInUseError("该服务存在运行中的灰度测试，请先终止灰度测试")

    # 防线三：总控绑定拦截
    bound_rows = db.query_all(
        """
        SELECT DISTINCT m.*
        FROM eag_master_binding b
        JOIN eag_master_agent m ON b.master_id = m.master_id
        WHERE b.service_id = :sid
        """,
        {"sid": service_id},
    )
    bound_masters = [row["slug"] for row in bound_rows]

    if bound_masters and not force:
        raise ServiceInUseError(
            f"该服务已被总控智能体绑定: {', '.join(bound_masters)}。若要强制删除并解除总控绑定，请指定 force=true"
        )

    # 级联删除 11 张从属表
    cascade = {}
    cascade["group_visibilities"] = db.execute(
        "DELETE FROM eag_group_visibility WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    cascade["user_visibilities"] = db.execute(
        "DELETE FROM eag_user_visibility WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    cascade["traffic_policies"] = db.execute(
        "DELETE FROM eag_traffic_policy WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    cascade["canary_runs"] = db.execute(
        "DELETE FROM eag_canary_run WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    cascade["installations"] = db.execute(
        "DELETE FROM eag_installation WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    cascade["releases"] = db.execute(
        "DELETE FROM eag_agent_release WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    cascade["health_observations"] = db.execute(
        "DELETE FROM eag_a2a_health_observation WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    cascade["card_revisions"] = db.execute(
        "DELETE FROM eag_a2a_card_revision WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    cascade["agent_instances"] = db.execute(
        "DELETE FROM eag_agent_instance WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    cascade["master_bindings"] = db.execute(
        "DELETE FROM eag_master_binding WHERE service_id=:sid", {"sid": service_id},
    ).rowcount
    if svc.get("source_type") == "HIAGENT":
        cascade["hiagent_conversations"] = db.execute(
            "DELETE FROM eag_hiagent_conversation WHERE service_id=:sid", {"sid": service_id},
        ).rowcount
        cascade["hiagent_agents"] = db.execute(
            "DELETE FROM eag_hiagent_agent WHERE service_id=:sid", {"sid": service_id},
        ).rowcount
    cascade["services"] = db.execute(
        "DELETE FROM eag_a2a_service_registration WHERE service_id=:sid", {"sid": service_id},
    ).rowcount

    db.execute(
        "INSERT INTO eag_audit_event (audit_id, cap_user_name, action, resource_type, resource_id, detail, created_at)"
        " VALUES (:a, :u, 'service.delete', 'service', :sid, :d, :ts)",
        {"a": new_id("aud_"), "u": deleted_by, "sid": service_id,
         "d": json.dumps({"slug": svc["slug"], "cascade": cascade, "force": force}, ensure_ascii=False),
         "ts": now()},
    )
    db.commit()

    # 运行期动态刷新总控 Card Skills
    remounted_slugs = []
    for m in bound_rows:
        try:
            mounts.remount_master(db, m)
            remounted_slugs.append(m["slug"])
        except Exception as exc:
            logger.warning("remount master %s (%s) failed after service %s deleted: %s", m.get("master_id"), m.get("slug"), service_id, exc)
            remounted_slugs.append(m["slug"])

    return {
        "deleted": service_id,
        "slug": svc["slug"],
        "cascade": cascade,
        "remounted_masters": remounted_slugs,
    }
