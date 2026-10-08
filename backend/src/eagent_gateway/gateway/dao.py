# -*- coding: utf-8 -*-
"""网关配置面 DAO（higress-mcp-gateway 01 §5.1 五张表，03 T03/T14）。

- id 为 BIGINT 自增（迁移 006 与 01 §5.1 一致）；唯一键 (profile_id, name)
  做插入后的回读定位，不用 lastrowid（保持查询风格与仓库其余 DAO 一致）。
- 外键约束在库层（006 迁移）；删除顺序由调用方保证（先子后父）。
- 审计 record_audit 由所有写路径调用（含失败记录，result=failed）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from ..persistence.db import Database, now

# 受管 Higress 资源命名规则（02 基准 §6；唯一格式来源）
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(name: str) -> str:
    """服务/consumer 名 → slug：小写、非 [a-z0-9-] 折叠为 '-'、去首尾 '-'。

    例：acktest-1 → llm-llm-acktest-1.internal / llm-acktest-1 /
    ai-route-route-acktest-1 / consumer-acktest-1（02 基准 §6 引用链）。
    """
    slug = _SLUG_RE.sub("-", (name or "").strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        raise ValueError("name 无法生成合法 slug（至少含一个字母/数字）")
    return slug[:48]


def registry_name(slug: str) -> str:
    """McpBridge registry 名：llm-llm-<slug>.internal。"""
    return f"llm-llm-{slug}.internal"


def provider_id(slug: str) -> str:
    """ai-proxy provider id / ai-route upstreams[].provider：llm-<slug>。"""
    return f"llm-{slug}"


def route_cm_name(slug: str) -> str:
    """ai-route ConfigMap 名：ai-route-route-<slug>。"""
    return f"ai-route-route-{slug}"


def route_payload_name(slug: str) -> str:
    """ai-route payload 内的 name：route-<slug>（02 基准 §3）。"""
    return f"route-{slug}"


def match_rule_service(slug: str) -> str:
    """ai-proxy matchRules[].service：<registry 名>.dns（02 基准 §5）。"""
    return f"{registry_name(slug)}.dns"


def jload(raw) -> dict | list | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def jdump(v) -> str | None:
    return json.dumps(v, ensure_ascii=False) if v is not None else None


# ===========================================================================
# profile（网关环境）
# ===========================================================================
def create_profile(
    db: Database, *, name: str, higress_ns: str, kube_context: str = "",
    higress_version: str | None = None,
) -> dict:
    db.execute(
        """INSERT INTO eag_gateway_profile
             (name, higress_ns, kube_context, higress_version, enabled)
           VALUES (:name, :ns, :ctx, :ver, 1)""",
        {"name": name, "ns": higress_ns, "ctx": kube_context,
         "ver": higress_version},
    )
    db.commit()
    return get_profile_by_name(db, name)


def get_profile(db: Database, profile_id: int) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_gateway_profile WHERE id=:id", {"id": profile_id},
    )


def get_profile_by_name(db: Database, name: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_gateway_profile WHERE name=:n", {"n": name},
    )


def list_profiles(db: Database) -> list[dict]:
    return db.query_all("SELECT * FROM eag_gateway_profile ORDER BY id")


def update_profile(
    db: Database, profile_id: int, *, higress_ns: str | None = None,
    kube_context: str | None = None, higress_version: str | None = None,
    enabled: bool | None = None,
) -> dict | None:
    sets, params = [], {"id": profile_id}
    if higress_ns is not None:
        sets.append("higress_ns=:ns"); params["ns"] = higress_ns
    if kube_context is not None:
        sets.append("kube_context=:ctx"); params["ctx"] = kube_context
    if higress_version is not None:
        sets.append("higress_version=:ver"); params["ver"] = higress_version
    if enabled is not None:
        sets.append("enabled=:en"); params["en"] = 1 if enabled else 0
    if sets:
        db.execute(
            f"UPDATE eag_gateway_profile SET {', '.join(sets)} WHERE id=:id",
            params,
        )
        db.commit()
    return get_profile(db, profile_id)


def count_profile_children(db: Database, profile_id: int) -> int:
    row = db.query_one(
        """SELECT
             (SELECT COUNT(*) FROM eag_gateway_mcp_service
              WHERE profile_id=:id) AS mcp,
             (SELECT COUNT(*) FROM eag_gateway_route
              WHERE profile_id=:id) AS routes,
             (SELECT COUNT(*) FROM eag_gateway_consumer
              WHERE profile_id=:id) AS consumers""",
        {"id": profile_id},
    )
    return row["mcp"] + row["routes"] + row["consumers"]


def delete_profile(db: Database, profile_id: int) -> None:
    """物理删除 profile（调用方保证无子资源，防误删整环境配置）。

    审计行**不随删**：保留全部历史（含本次删除墓碑）——审计不可删
    （治理口径同 eag_invocation_event；无 FK，行内 profile_id 即溯源锚点）。
    """
    db.execute(
        "DELETE FROM eag_gateway_profile WHERE id=:id", {"id": profile_id},
    )
    db.commit()


# ===========================================================================
# MCP 服务（McpBridge registry 的库侧镜像）
# ===========================================================================
def create_mcp_service(
    db: Database, *, profile_id: int, name: str, domain: str,
    port: int = 443, protocol: str = "https", registry_type: str = "dns",
    provider_config: dict | None = None, created_by: str,
) -> dict:
    db.execute(
        """INSERT INTO eag_gateway_mcp_service
             (profile_id, name, domain, port, protocol, registry_type,
              provider_config, status, created_by)
           VALUES (:pid, :name, :domain, :port, :proto, :rtype,
                   :pcfg, 'active', :u)""",
        {"pid": profile_id, "name": name, "domain": domain, "port": port,
         "proto": protocol, "rtype": registry_type, "pcfg": jdump(provider_config),
         "u": created_by},
    )
    db.commit()
    return get_mcp_service_by_name(db, profile_id, name)


def get_mcp_service(db: Database, service_id: int) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_gateway_mcp_service WHERE id=:id",
        {"id": service_id},
    )


def get_mcp_service_by_name(
    db: Database, profile_id: int, name: str,
) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_gateway_mcp_service"
        " WHERE profile_id=:pid AND name=:n",
        {"pid": profile_id, "n": name},
    )


def list_mcp_services(db: Database, profile_id: int) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_gateway_mcp_service WHERE profile_id=:pid"
        " ORDER BY id",
        {"pid": profile_id},
    )


def update_mcp_service(
    db: Database, service_id: int, *, domain: str | None = None,
    port: int | None = None, protocol: str | None = None,
    registry_type: str | None = None, provider_config: dict | None = None,
    status: str | None = None,
) -> dict | None:
    sets, params = [], {"id": service_id}
    if domain is not None:
        sets.append("domain=:domain"); params["domain"] = domain
    if port is not None:
        sets.append("port=:port"); params["port"] = port
    if protocol is not None:
        sets.append("protocol=:proto"); params["proto"] = protocol
    if registry_type is not None:
        sets.append("registry_type=:rtype"); params["rtype"] = registry_type
    if provider_config is not None:
        sets.append("provider_config=:pcfg")
        params["pcfg"] = jdump(provider_config)
    if status is not None:
        sets.append("status=:st"); params["st"] = status
    if sets:
        db.execute(
            f"UPDATE eag_gateway_mcp_service SET {', '.join(sets)}"
            " WHERE id=:id", params,
        )
        db.commit()
    return get_mcp_service(db, service_id)


def routes_referencing_mcp(db: Database, service_id: int) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_gateway_route WHERE upstream_mcp=:id",
        {"id": service_id},
    )


def delete_mcp_service(db: Database, service_id: int) -> None:
    db.execute(
        "DELETE FROM eag_gateway_mcp_service WHERE id=:id", {"id": service_id},
    )
    db.commit()


# ===========================================================================
# 路由（ai-route ConfigMap 的库侧镜像）
# ===========================================================================
MATCH_TYPES = ("PRE", "EXACT", "REGEX")


def create_route(
    db: Database, *, profile_id: int, name: str, match_type: str,
    match_value: str, upstream_mcp: int, weight: int = 100,
    auth_enabled: bool = True, auth_type: str = "key-auth",
    retry_enabled: bool = True, retry_attempts: int = 3,
    created_by: str,
) -> dict:
    db.execute(
        """INSERT INTO eag_gateway_route
             (profile_id, name, match_type, match_value, upstream_mcp,
              weight, auth_enabled, auth_type, retry_enabled, retry_attempts,
              status, created_by)
           VALUES (:pid, :name, :mt, :mv, :up, :w, :ae, :at, :re, :ra,
                   'active', :u)""",
        {"pid": profile_id, "name": name, "mt": match_type, "mv": match_value,
         "up": upstream_mcp, "w": weight, "ae": 1 if auth_enabled else 0,
         "at": auth_type, "re": 1 if retry_enabled else 0, "ra": retry_attempts,
         "u": created_by},
    )
    db.commit()
    return get_route_by_name(db, profile_id, name)


def get_route(db: Database, route_id: int) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_gateway_route WHERE id=:id", {"id": route_id},
    )


def get_route_by_name(db: Database, profile_id: int, name: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_gateway_route WHERE profile_id=:pid AND name=:n",
        {"pid": profile_id, "n": name},
    )


def list_routes(db: Database, profile_id: int) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_gateway_route WHERE profile_id=:pid ORDER BY id",
        {"pid": profile_id},
    )


def update_route(
    db: Database, route_id: int, *, match_type: str | None = None,
    match_value: str | None = None, upstream_mcp: int | None = None,
    weight: int | None = None, auth_enabled: bool | None = None,
    auth_type: str | None = None, retry_enabled: bool | None = None,
    retry_attempts: int | None = None, status: str | None = None,
) -> dict | None:
    sets, params = [], {"id": route_id}
    if match_type is not None:
        sets.append("match_type=:mt"); params["mt"] = match_type
    if match_value is not None:
        sets.append("match_value=:mv"); params["mv"] = match_value
    if upstream_mcp is not None:
        sets.append("upstream_mcp=:up"); params["up"] = upstream_mcp
    if weight is not None:
        sets.append("weight=:w"); params["w"] = weight
    if auth_enabled is not None:
        sets.append("auth_enabled=:ae"); params["ae"] = 1 if auth_enabled else 0
    if auth_type is not None:
        sets.append("auth_type=:at"); params["at"] = auth_type
    if retry_enabled is not None:
        sets.append("retry_enabled=:re")
        params["re"] = 1 if retry_enabled else 0
    if retry_attempts is not None:
        sets.append("retry_attempts=:ra"); params["ra"] = retry_attempts
    if status is not None:
        sets.append("status=:st"); params["st"] = status
    if sets:
        db.execute(
            f"UPDATE eag_gateway_route SET {', '.join(sets)} WHERE id=:id",
            params,
        )
        db.commit()
    return get_route(db, route_id)


def delete_route(db: Database, route_id: int) -> None:
    db.execute(
        "DELETE FROM eag_gateway_route WHERE id=:id", {"id": route_id},
    )
    db.commit()


# ===========================================================================
# consumer（key-auth 的库侧镜像；credential 为 AES-GCM 密文块）
# ===========================================================================
def create_consumer(
    db: Database, *, profile_id: int, name: str, credential: bytes,
    route_ids: list[int] | None = None, created_by: str,
) -> dict:
    db.execute(
        """INSERT INTO eag_gateway_consumer
             (profile_id, name, credential, route_ids, status, created_by)
           VALUES (:pid, :name, :cred, :rids, 'active', :u)""",
        {"pid": profile_id, "name": name, "cred": credential,
         "rids": jdump(route_ids or []), "u": created_by},
    )
    db.commit()
    return get_consumer_by_name(db, profile_id, name)


def get_consumer(db: Database, consumer_id: int) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_gateway_consumer WHERE id=:id",
        {"id": consumer_id},
    )


def get_consumer_by_name(
    db: Database, profile_id: int, name: str,
) -> dict | None:
    return db.query_one(
        "SELECT * FROM eag_gateway_consumer WHERE profile_id=:pid AND name=:n",
        {"pid": profile_id, "n": name},
    )


def list_consumers(db: Database, profile_id: int) -> list[dict]:
    return db.query_all(
        "SELECT * FROM eag_gateway_consumer WHERE profile_id=:pid ORDER BY id",
        {"pid": profile_id},
    )


def update_consumer(
    db: Database, consumer_id: int, *, credential: bytes | None = None,
    prev_credential: bytes | None = None, prev_expires_at: datetime | None = None,
    route_ids: list[int] | None = None, status: str | None = None,
    clear_prev: bool = False,
) -> dict | None:
    """更新 consumer；轮换=新 credential + 旧值挪 prev_*（宽限期）；
    clear_prev 在宽限期到期后下线旧 key。"""
    sets, params = [], {"id": consumer_id}
    if credential is not None:
        sets.append("credential=:cred"); params["cred"] = credential
    if prev_credential is not None:
        sets.append("prev_credential=:pcred"); params["pcred"] = prev_credential
    if prev_expires_at is not None:
        sets.append("prev_expires_at=:pexp"); params["pexp"] = prev_expires_at
    if clear_prev:
        sets.append("prev_credential=NULL, prev_expires_at=NULL")
    if route_ids is not None:
        sets.append("route_ids=:rids"); params["rids"] = jdump(route_ids)
    if status is not None:
        sets.append("status=:st"); params["st"] = status
    if sets:
        db.execute(
            f"UPDATE eag_gateway_consumer SET {', '.join(sets)}"
            " WHERE id=:id", params,
        )
        db.commit()
    return get_consumer(db, consumer_id)


def delete_consumer(db: Database, consumer_id: int) -> None:
    db.execute(
        "DELETE FROM eag_gateway_consumer WHERE id=:id", {"id": consumer_id},
    )
    db.commit()


def expired_grace_consumers(db: Database) -> list[dict]:
    """宽限期已到期的 consumer（旧 key 该下线）。"""
    return db.query_all(
        "SELECT * FROM eag_gateway_consumer"
        " WHERE prev_credential IS NOT NULL AND prev_expires_at <= :n",
        {"n": now()},
    )


# ===========================================================================
# 审计（所有写操作含失败均落库，03 T14）
# ===========================================================================
RESOURCE_TYPES = (
    "profile", "mcpbridge", "wasmplugin", "route", "consumer", "sync",
)
AUDIT_ACTIONS = ("create", "update", "delete")


def record_audit(
    db: Database, *, profile_id: int, resource_type: str, action: str,
    payload_before: dict | None, payload_after: dict | None,
    result: str, operator: str,
) -> None:
    """审计写入自带独立事务语义：失败不回滚业务已提交的变更。"""
    try:
        db.execute(
            """INSERT INTO eag_gateway_audit
                 (profile_id, resource_type, action, payload_before,
                  payload_after, result, operator)
               VALUES (:pid, :rt, :act, :pb, :pa, :res, :u)""",
            {"pid": profile_id, "rt": resource_type, "act": action,
             "pb": jdump(payload_before), "pa": jdump(payload_after or {}),
             "res": result, "u": operator},
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def list_audits(
    db: Database, profile_id: int, *, resource_type: str | None = None,
    result: str | None = None, page: int = 1, page_size: int = 50,
) -> tuple[list[dict], int]:
    where = "profile_id=:pid"
    params: dict = {"pid": profile_id}
    if resource_type:
        where += " AND resource_type=:rt"; params["rt"] = resource_type
    if result:
        where += " AND result=:res"; params["res"] = result
    total = db.query_one(
        f"SELECT COUNT(*) AS n FROM eag_gateway_audit WHERE {where}", params,
    )["n"]
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    rows = db.query_all(
        f"SELECT * FROM eag_gateway_audit WHERE {where}"
        " ORDER BY id DESC LIMIT :lim OFFSET :off",
        {**params, "lim": page_size, "off": (page - 1) * page_size},
    )
    for r in rows:
        r["payload_before"] = jload(r.get("payload_before"))
        r["payload_after"] = jload(r.get("payload_after"))
    return rows, total
