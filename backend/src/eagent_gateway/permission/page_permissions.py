# -*- coding: utf-8 -*-
"""控制台页面权限：固定目录、有效权限合并与请求级授权。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import Query, Request

from .. import errors
from ..persistence.db import Database


PAGE_CATALOG = (
    {"pageKey": "multi", "name": "Agent Team", "levels": ["view", "manage"]},
    {"pageKey": "registry", "name": "服务注册", "levels": ["view", "manage"]},
    {"pageKey": "kb", "name": "知识库", "levels": ["view", "manage"]},
    {"pageKey": "performance", "name": "性能测试", "levels": ["view", "manage"]},
    {"pageKey": "gateway", "name": "网关配置", "levels": ["view", "manage"]},
    {"pageKey": "loop", "name": "提示词调优", "levels": ["view", "manage"]},
    {"pageKey": "release", "name": "版本发布", "levels": ["view", "manage"]},
    {"pageKey": "installation", "name": "工作空间安装", "levels": ["view", "manage"]},
    {"pageKey": "trace", "name": "调用链路追踪", "levels": ["view"]},
    {"pageKey": "instance", "name": "服务实例", "levels": ["view", "manage"]},
    {"pageKey": "enhance", "name": "平台增强", "levels": ["view", "manage"]},
)
CATALOG_BY_KEY = {p["pageKey"]: p for p in PAGE_CATALOG}
LEVEL_RANK = {"view": 1, "manage": 2}


@dataclass(frozen=True)
class PageRequirement:
    page_key: str
    level: str


def validate_permissions(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    normalized: list[dict] = []
    for item in items:
        page_key = str(item.get("pageKey") or "").strip()
        level = str(item.get("level") or "").strip().lower()
        page = CATALOG_BY_KEY.get(page_key)
        if not page:
            raise errors.ValidationError(f"未知页面权限: {page_key}")
        if page_key in seen:
            raise errors.ValidationError(f"页面权限重复: {page_key}")
        if level not in page["levels"]:
            raise errors.ValidationError(
                f"页面 {page_key} 不支持权限级别 {level}",
            )
        seen.add(page_key)
        normalized.append({"pageKey": page_key, "level": level})
    return normalized


def direct_permissions(db: Database, subject_type: str, subject_id: str) -> list[dict]:
    rows = db.query_all(
        "SELECT page_key, permission_level FROM eag_page_permission "
        "WHERE subject_type=:t AND subject_id=:s ORDER BY page_key",
        {"t": subject_type, "s": subject_id},
    )
    return [{"pageKey": r["page_key"], "level": r["permission_level"]} for r in rows]


def replace_permissions(
    db: Database, subject_type: str, subject_id: str, items: list[dict],
) -> list[dict]:
    normalized = validate_permissions(items)
    db.execute(
        "DELETE FROM eag_page_permission WHERE subject_type=:t AND subject_id=:s",
        {"t": subject_type, "s": subject_id},
    )
    for item in normalized:
        db.execute(
            "INSERT INTO eag_page_permission "
            "(subject_type, subject_id, page_key, permission_level) "
            "VALUES (:t, :s, :p, :l)",
            {"t": subject_type, "s": subject_id,
             "p": item["pageKey"], "l": item["level"]},
        )
    return normalized


def effective_permissions(db: Database, user_id: str, *, is_admin: bool = False) -> dict:
    if is_admin:
        return {
            p["pageKey"]: {
                "pageKey": p["pageKey"],
                "name": p["name"],
                "level": "manage" if "manage" in p["levels"] else "view",
                "sources": [{"type": "role", "id": "eagent_admin", "name": "管理员"}],
            }
            for p in PAGE_CATALOG
        }
    rows = db.query_all(
        """
        SELECT p.page_key, p.permission_level, p.subject_type, p.subject_id,
               NULL AS group_name
          FROM eag_page_permission p
         WHERE p.subject_type='USER' AND p.subject_id=:u
        UNION ALL
        SELECT p.page_key, p.permission_level, p.subject_type, p.subject_id,
               g.name AS group_name
          FROM eag_group_membership gm
          JOIN eag_user_group g ON g.group_id=gm.group_id
          JOIN eag_page_permission p
            ON p.subject_type='GROUP' AND p.subject_id=gm.group_id
         WHERE gm.cap_user_name=:u
        """,
        {"u": user_id},
    )
    result: dict[str, dict] = {}
    for row in rows:
        page_key = row["page_key"]
        if page_key not in CATALOG_BY_KEY:
            continue
        source = {
            "type": row["subject_type"].lower(),
            "id": row["subject_id"],
            "name": row.get("group_name") or ("个人直授" if row["subject_type"] == "USER" else row["subject_id"]),
            "level": row["permission_level"],
        }
        current = result.get(page_key)
        if current is None:
            result[page_key] = {
                "pageKey": page_key,
                "name": CATALOG_BY_KEY[page_key]["name"],
                "level": row["permission_level"],
                "sources": [source],
            }
        else:
            current["sources"].append(source)
            if LEVEL_RANK[row["permission_level"]] > LEVEL_RANK[current["level"]]:
                current["level"] = row["permission_level"]
    return result


def _route_requirement(method: str, path: str) -> PageRequirement | None:
    """控制台 REST 的页面归属。None 表示继续由原有角色依赖处理。"""
    if path.startswith("/api/v1/selectors/"):
        return None
    if path.startswith("/api/v1/knowledge-bases"):
        page = "kb"
    elif re.match(r"^/api/v1/agents/[^/]+/kb-bindings$", path):
        page = "multi"
    elif path.startswith("/api/v1/perf/"):
        page = "performance"
    elif path.startswith("/api/v1/gateway/"):
        page = "gateway"
    elif path == "/api/v1/runs" or path.startswith("/api/v1/datasets") or path.startswith("/api/v1/cases") or path.startswith("/api/v1/optimizations"):
        page = "loop"
    elif path.startswith("/api/v1/traces/"):
        page = "trace"
    elif path.startswith("/api/v1/releases/") or re.match(r"^/api/v1/services/[^/]+/releases", path):
        page = "release"
    elif path.startswith("/api/v1/installations/") or re.match(r"^/api/v1/workspaces/[^/]+/installations", path):
        page = "installation"
    elif path.startswith("/api/v1/instances/") or re.match(r"^/api/v1/services/[^/]+/instances", path):
        page = "instance"
    elif path.startswith(("/api/v1/observability/", "/api/v1/efficiency/", "/api/v1/external-tools", "/api/v1/traffic-policies", "/api/v1/memory")):
        page = "enhance"
    elif re.match(r"^/api/v1/services/[^/]+/(traffic-policy|canary)", path):
        page = "enhance"
    elif path.startswith("/api/v1/services"):
        page = "registry"
    elif path.startswith("/api/v1/agents"):
        page = "multi"
    else:
        return None
    level = "view" if method.upper() == "GET" else "manage"
    # 导出可选择包含 consumer 密钥，统一要求管理权限，避免只读授权泄密。
    if page == "gateway" and path.endswith("/export"):
        level = "manage"
    return PageRequirement(page, level)


def request_permissions(request: Request, user_id: str, is_admin: bool) -> dict:
    cached = getattr(request.state, "page_permissions", None)
    if cached is not None:
        return cached
    db = Database()
    try:
        cached = effective_permissions(db, user_id, is_admin=is_admin)
    finally:
        db.close()
    request.state.page_permissions = cached
    return cached


def is_allowed(grants: dict, page_key: str, level: str) -> bool:
    grant = grants.get(page_key)
    return bool(grant and LEVEL_RANK[grant["level"]] >= LEVEL_RANK[level])


def authorize_mapped_request(request: Request, user_id: str, is_admin: bool) -> None:
    requirement = _route_requirement(request.method, request.url.path)
    if requirement is None or is_admin:
        return
    grants = request_permissions(request, user_id, is_admin)
    if not is_allowed(grants, requirement.page_key, requirement.level):
        raise errors.ForbiddenError(
            f"缺少页面权限: {requirement.page_key}:{requirement.level}",
        )


def mapped_request_allowed(request: Request, user_id: str, is_admin: bool) -> bool:
    requirement = _route_requirement(request.method, request.url.path)
    if requirement is None:
        return False
    return is_admin or is_allowed(
        request_permissions(request, user_id, is_admin),
        requirement.page_key, requirement.level,
    )


SELECTOR_PAGES = {
    "services": {"multi", "performance", "release", "installation", "trace", "instance", "enhance"},
    "agents": {"loop", "trace", "performance"},
    "releases": {"installation", "enhance"},
    "knowledge-bases": {"multi", "enhance"},
}


def require_page(page_key: str, level: str = "view"):
    def _dep(request: Request) -> str:
        ctx = getattr(request.state, "session_ctx", None)
        if ctx is None:
            return page_key
        if ctx.is_admin() or is_allowed(
            request_permissions(request, ctx.user_id, ctx.is_admin()),
            page_key, level,
        ):
            return page_key
        raise errors.ForbiddenError(f"缺少页面权限: {page_key}:{level}")
    return _dep


def require_selector_access(
    request: Request, forPage: str = Query(..., min_length=1, max_length=32),
) -> str:
    kind = request.url.path.rsplit("/", 1)[-1]
    if forPage not in SELECTOR_PAGES.get(kind, set()):
        raise errors.ValidationError("该页面不能使用此选择器")
    ctx = getattr(request.state, "session_ctx", None)
    if ctx is None:
        # 已由 strict 中间件验证的 X-Admin-Token 兼容通道。
        return forPage
    if ctx.is_admin() or is_allowed(
        request_permissions(request, ctx.user_id, ctx.is_admin()), forPage, "view",
    ):
        return forPage
    raise errors.ForbiddenError(f"缺少页面权限: {forPage}:view")
