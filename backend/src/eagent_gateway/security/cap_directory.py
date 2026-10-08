# -*- coding: utf-8 -*-
"""CAP/Pluto 用户目录。\n\n``http`` 模式沿用 CAP manage 接口，``database`` 模式查询当前数据库的\n``user`` 视图。这里只负责目录查询与字段归一，不处理本地会话、权限或落库。\n"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

import httpx

from ..config import settings
from ..errors import DependencyError, NotFoundError, ValidationError
from ..persistence.db import Database

logger = logging.getLogger("eagent.cap-directory")
_BACKENDS = {"http", "database"}
_DATABASE_ACTIVE_STATUS = "启用"


def directory_backend() -> str:
    backend = (settings.cap_user_directory_backend or "http").strip().lower()
    if backend not in _BACKENDS:
        raise DependencyError(
            "CAP_USER_DIRECTORY_BACKEND 配置错误，仅支持 http/database",
        )
    return backend


def database_backend_enabled() -> bool:
    return directory_backend() == "database"


def _join_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/")
    path = (path or "").strip()
    if not base:
        return ""
    if not path:
        return base
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{base}/{path.lstrip('/')}"


def _search_url() -> str:
    return settings.cap_user_search_url or _join_url(
        settings.cap_user_base_url, settings.cap_user_search_path,
    )


def _detail_url() -> str:
    return settings.cap_user_detail_url or _join_url(
        settings.cap_user_base_url, settings.cap_user_detail_path,
    )


def _headers(pluto_access_token: str) -> dict[str, str]:
    token = (pluto_access_token or "").strip()
    if not token:
        raise ValidationError("当前 Pluto 会话没有可用的 access token，请重新登录")
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _ensure_success(body: dict) -> dict | list:
    if "code" in body:
        code = body.get("code")
        if code not in (1, "1", 200, "200", "OK", "ok"):
            raise DependencyError("CAP 用户目录查询失败")
        return body.get("data") or {}
    return body.get("data") if "data" in body else body


def _first(raw: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        val = raw.get(key)
        if val is not None and val != "":
            return str(val)
    return ""


def _dept_name(raw: dict) -> str:
    direct = _first(raw, ("department", "deptName", "dept_name", "unitName", "unit_name"))
    if direct:
        return direct
    dept = raw.get("dept")
    if isinstance(dept, dict):
        name = _first(dept, ("deptName", "dept_name", "unitName", "unit_name", "name"))
        if name:
            return name
    depts = raw.get("depts")
    if isinstance(depts, list) and depts:
        first = depts[0]
        if isinstance(first, dict):
            return _first(first, ("deptName", "dept_name", "unitName", "unit_name", "name"))
    return ""


def _dept_id(raw: dict) -> str:
    direct = _first(raw, ("departmentId", "department_id", "deptId", "dept_id", "unitCode", "unit_code"))
    if direct:
        return direct
    dept = raw.get("dept")
    if isinstance(dept, dict):
        dept_id = _first(dept, ("deptId", "dept_id", "unitCode", "unit_code", "id"))
        if dept_id:
            return dept_id
    depts = raw.get("depts")
    if isinstance(depts, list) and depts:
        first = depts[0]
        if isinstance(first, dict):
            return _first(first, ("deptId", "dept_id", "unitCode", "unit_code", "id"))
    return ""


def _status(raw: dict) -> str:
    val = _first(raw, ("status", "state", "enabled"))
    text = val.strip().lower()
    if text in ("disabled", "disable", "deleted", "delete", "inactive", "false", "1"):
        return "disabled"
    return "active"


def normalize_user(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    user_id = _first(raw, (
        "userId", "user_id", "badgeId", "badge_id", "username", "userName",
    )).strip()
    username = _first(raw, ("username", "userName", "preferred_username", "user_id", "userId")).strip()
    return {
        "userId": user_id or username,
        "username": username or user_id,
        "preferredUsername": username or user_id,
        "displayName": _first(raw, ("displayName", "display_name", "realName", "real_name", "nickName", "nick_name", "name")) or user_id or username,
        "email": _first(raw, ("email", "mail")),
        "mobile": _first(raw, ("mobile", "phonenumber", "phone", "phoneNumber")),
        "wecomId": _first(raw, ("wecom_id", "wecomId")),
        "department": _dept_name(raw),
        "departmentId": _dept_id(raw),
        "plutoSub": _first(raw, ("plutoSub", "pluto_sub", "sub", "id")),
        "status": _status(raw),
        "source": "cap_pluto",
        "raw": raw,
    }


def _normalize_database_user(raw: dict) -> dict:
    """映射 CAP `user` 视图行；username 是 eAgent 的真实工号。"""
    if not isinstance(raw, dict):
        return {}
    username = str(raw.get("username") or "").strip()
    if not username:
        return {}
    cap_status = str(raw.get("status") or "").strip()
    return {
        "userId": username,
        "username": username,
        "preferredUsername": username,
        "displayName": str(raw.get("real_name") or username),
        "email": str(raw.get("email") or ""),
        "mobile": str(raw.get("mobile") or ""),
        "wecomId": "",
        "department": str(raw.get("department") or ""),
        "departmentId": "",
        "plutoSub": str(raw.get("pluto_user_id") or ""),
        "status": "active" if cap_status == _DATABASE_ACTIVE_STATUS else "disabled",
        "source": "cap_database",
        "raw": raw,
    }


def _database_search(
    *, name: str, dept_id: str, page_no: int, page_size: int,
) -> dict:
    keyword = (name or "").strip()
    department = (dept_id or "").strip()
    page = max(1, int(page_no or 1))
    size = min(100, max(1, int(page_size or 10)))
    params = {
        "keyword": keyword,
        "pattern": f"%{keyword}%",
        "department": department,
        "limit": size,
        "offset": (page - 1) * size,
    }
    where = """
        user_code = 'pluto'
        AND (:keyword = '' OR username LIKE :pattern OR real_name LIKE :pattern)
        AND (:department = '' OR department = :department)
    """
    db: Database | None = None
    try:
        db = Database()
        total_row = db.query_one(
            f"SELECT COUNT(*) AS total FROM `user` WHERE {where}", params,
        ) or {"total": 0}
        rows = db.query_all(
            f"""
            SELECT username, real_name, email, mobile, department,
                   pluto_user_id, status
              FROM `user`
             WHERE {where}
             ORDER BY username
             LIMIT :limit OFFSET :offset
            """,
            params,
        )
        items = [
            user for user in (_normalize_database_user(row) for row in rows)
            if user.get("userId")
        ]
        return {"items": items, "total": int(total_row.get("total") or 0)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("CAP 用户目录数据库搜索失败")
        raise DependencyError(
            "CAP 用户目录数据库查询失败，请检查 `user` 视图及查询权限",
        ) from exc
    finally:
        if db is not None:
            db.close()


def _database_get_user(user_id: str) -> dict:
    db: Database | None = None
    try:
        db = Database()
        row = db.query_one(
            """
            SELECT username, real_name, email, mobile, department,
                   pluto_user_id, status
              FROM `user`
             WHERE username=:username AND user_code='pluto'
             LIMIT 1
            """,
            {"username": user_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("CAP 用户目录数据库详情查询失败")
        raise DependencyError(
            "CAP 用户目录数据库查询失败，请检查 `user` 视图及查询权限",
        ) from exc
    finally:
        if db is not None:
            db.close()
    if row is None:
        raise NotFoundError("CAP 用户不存在")
    normalized = _normalize_database_user(row)
    if not normalized.get("userId"):
        raise NotFoundError("CAP 用户不存在")
    return normalized


def _items_and_total(data: dict | list) -> tuple[list[dict], int]:
    if isinstance(data, list):
        return [u for u in (normalize_user(x) for x in data) if u.get("userId")], len(data)
    if not isinstance(data, dict):
        return [], 0
    for key in ("items", "records", "rows", "list", "users", "content"):
        val = data.get(key)
        if isinstance(val, list):
            items = [u for u in (normalize_user(x) for x in val) if u.get("userId")]
            total = data.get("total") or data.get("totalCount") or data.get("count") or len(items)
            return items, int(total or 0)
    user = normalize_user(data)
    if user.get("userId"):
        return [user], 1
    return [], 0


async def search_users(
    *, name: str = "", dept_id: str = "", page_no: int = 1, page_size: int = 10,
    pluto_access_token: str = "",
) -> dict:
    if database_backend_enabled():
        return await asyncio.to_thread(
            _database_search, name=name, dept_id=dept_id,
            page_no=page_no, page_size=page_size,
        )
    url = _search_url()
    if not url:
        raise DependencyError("CAP 用户目录搜索接口未配置")
    payload = {
        "name": (name or "").strip(),
        "dept_id": (dept_id or "").strip(),
        "page_no": max(1, int(page_no or 1)),
        "page_size": min(100, max(1, int(page_size or 10))),
    }
    async with httpx.AsyncClient(timeout=settings.pluto_timeout_s) as client:
        resp = await client.post(url, json=payload, headers=_headers(pluto_access_token))
    if resp.status_code != 200:
        raise DependencyError("CAP 用户目录搜索接口调用失败")
    data = _ensure_success(resp.json())
    items, total = _items_and_total(data)
    return {"items": items, "total": total}


async def get_user(user_id: str, *, pluto_access_token: str = "") -> dict:
    user = (user_id or "").strip()
    if not user:
        raise ValidationError("userId 不能为空")
    if database_backend_enabled():
        return await asyncio.to_thread(_database_get_user, user)
    detail_url = _detail_url()
    if detail_url:
        sep = "&" if "?" in detail_url else "?"
        url = f"{detail_url}{sep}{urlencode({'username': user})}"
        async with httpx.AsyncClient(timeout=settings.pluto_timeout_s) as client:
            resp = await client.get(url, headers=_headers(pluto_access_token))
        if resp.status_code != 200:
            raise DependencyError("CAP 用户详情接口调用失败")
        normalized = normalize_user(_ensure_success(resp.json()))
        if normalized.get("userId"):
            return normalized
    result = await search_users(
        name=user, page_no=1, page_size=10, pluto_access_token=pluto_access_token,
    )
    for item in result["items"]:
        candidates = {item.get("userId"), item.get("username"), item.get("preferredUsername")}
        if user in candidates:
            return item
    raise NotFoundError("CAP 用户不存在")
