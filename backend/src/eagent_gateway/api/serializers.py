"""HTTP response projections shared by management routes and paged lists."""
from ..runtime.model_factory import mask_model_config, parse_model_config
from ..security import console_auth


def _public_master(master: dict | None) -> dict | None:
    """对外脱敏（master-model-config 专题）：model_config_json 的 api_key
    不落响应，替换为 model_config（base_url/model/尾4位，api_key_set）。"""
    if not master:
        return master
    m = dict(master)
    m["model_config"] = mask_model_config(
        parse_model_config(m.pop("model_config_json", None)),
    )
    return m


def _service_out(row: dict | None) -> dict | None:
    """服务行响应脱敏: auth_secret_ref 是 AES-GCM 密文，不出 API 面，
    只回显是否已配置。"""
    if row is None:
        return None
    out = dict(row)
    out["outbound_credential_configured"] = bool(out.pop("auth_secret_ref", None))
    return out


def _public_user(row: dict, roles: list[str], visible_services: int = 0) -> dict:
    return {
        "userId": row["user_id"],
        "displayName": row["display_name"] or row["user_id"],
        "department": row["department"] or "",
        "email": row["email"] or "",
        "status": row["status"],
        "roles": roles,
        "isAdmin": console_auth.ROLE_ADMIN in roles,
        "visibleServices": visible_services,
        "profileSyncStatus": row.get("profile_sync_status") or "",
        "createdAt": row.get("created_at"),
        "lastLoginAt": row.get("last_login_at"),
    }


def _instance_out(instance) -> dict:  # noqa: ANN001
    return {
        "instanceId": instance.instance_id,
        "serviceId": instance.service_id,
        "instanceName": instance.instance_name,
        "endpoint": instance.endpoint,
        "state": instance.state,
        "ready": instance.ready,
        "serving": instance.serving,
        "terminating": instance.terminating,
        "leaseExpiresAt": instance.lease_expires_at,
        "lastHeartbeatAt": instance.last_heartbeat_at,
        "metadata": instance.metadata,
    }
