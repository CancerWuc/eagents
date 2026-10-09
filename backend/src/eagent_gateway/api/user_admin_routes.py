# -*- coding: utf-8 -*-
"""用户管理与我的权限（permission-control v1）。

- 管理员（eagent_admin / 本地 admin）：查看登录过的用户、授予/回收
  管理员角色、启用/禁用账号（禁用即登录 403 并剔除存量会话）；
- 普通用户：/me/permissions 查看自己的角色与可见服务（含来源），
  以及管理员联系方式（线下申请渠道）。

角色数据在 eag_user_role（eagent_user / eagent_admin）；服务可见性
沿用组可见 ∪ 个人直授四表。全部写操作落 eag_audit_event 审计。
"""
from __future__ import annotations

from contextlib import closing
import json
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import errors
from ..config import settings
from ..persistence.db import Database
from ..permission.scopes import require_admin, require_console_user
from ..permission import page_permissions
from ..security import cap_directory, console_auth
from .pagination import Page, optional_page
from .list_pages import list_page
from .serializers import _public_user

router = APIRouter(prefix="/api/v1")


class PagePermissionItem(BaseModel):
    pageKey: str
    level: str


class PagePermissionsIn(BaseModel):
    permissions: list[PagePermissionItem]


def _audit(db: Database, operator: str, action: str, user_id: str,
           detail: dict) -> None:
    db.execute(
        """
        INSERT INTO eag_audit_event
            (audit_id, cap_user_name, action, resource_type, resource_id, detail)
        VALUES (:id, :op, :act, 'console_user', :uid, :detail)
        """,
        {
            "id": uuid.uuid4().hex[:32],
            "op": operator,
            "act": action,
            "uid": user_id,
            "detail": json.dumps(detail, ensure_ascii=False),
        },
    )


def _audit_page_permissions(
    db: Database, operator: str, subject_type: str, subject_id: str,
    before: list[dict], after: list[dict],
) -> None:
    db.execute(
        """
        INSERT INTO eag_audit_event
            (audit_id, cap_user_name, action, resource_type, resource_id, detail)
        VALUES (:id, :op, 'page_permissions.replace',
                'page_permission', :rid, :detail)
        """,
        {
            "id": uuid.uuid4().hex[:32], "op": operator,
            "rid": f"{subject_type}:{subject_id}",
            "detail": json.dumps(
                {"subjectType": subject_type, "subjectId": subject_id,
                 "before": before, "after": after},
                ensure_ascii=False,
            ),
        },
    )


@router.get("/admin/page-permissions/catalog",
            dependencies=[Depends(require_admin)])
def page_permission_catalog() -> dict:
    return {"pages": list(page_permissions.PAGE_CATALOG)}


def _subject_exists(db: Database, subject_type: str, subject_id: str) -> bool:
    if subject_type == "USER":
        return bool(db.query_one(
            "SELECT 1 AS x FROM eag_console_user WHERE user_id=:id", {"id": subject_id},
        ))
    return bool(db.query_one(
        "SELECT 1 AS x FROM eag_user_group WHERE group_id=:id", {"id": subject_id},
    ))


def _get_subject_page_permissions(subject_type: str, subject_id: str) -> dict:
    with closing(Database()) as db:
        if not _subject_exists(db, subject_type, subject_id):
            raise errors.NotFoundError("用户不存在" if subject_type == "USER" else "用户组不存在")
        return {
            "subjectType": subject_type.lower(), "subjectId": subject_id,
            "permissions": page_permissions.direct_permissions(
                db, subject_type, subject_id,
            ),
        }


def _put_subject_page_permissions(
    subject_type: str, subject_id: str, body: PagePermissionsIn, operator: str,
) -> dict:
    with closing(Database()) as db:
        try:
            if not _subject_exists(db, subject_type, subject_id):
                raise errors.NotFoundError("用户不存在" if subject_type == "USER" else "用户组不存在")
            before = page_permissions.direct_permissions(db, subject_type, subject_id)
            after = page_permissions.replace_permissions(
                db, subject_type, subject_id,
                [item.model_dump() for item in body.permissions],
            )
            _audit_page_permissions(
                db, operator, subject_type, subject_id, before, after,
            )
            db.commit()
            return {"subjectType": subject_type.lower(), "subjectId": subject_id,
                    "permissions": after}
        except Exception:
            db.rollback()
            raise


@router.get("/admin/users/{user_id}/page-permissions",
            dependencies=[Depends(require_admin)])
def get_user_page_permissions(user_id: str) -> dict:
    return _get_subject_page_permissions("USER", user_id)


@router.put("/admin/users/{user_id}/page-permissions",
            dependencies=[Depends(require_admin)])
def put_user_page_permissions(
    user_id: str, body: PagePermissionsIn,
    operator: str = Depends(require_admin),
) -> dict:
    return _put_subject_page_permissions("USER", user_id, body, operator)


@router.get("/admin/groups/{group_id}/page-permissions",
            dependencies=[Depends(require_admin)])
def get_group_page_permissions(group_id: str) -> dict:
    return _get_subject_page_permissions("GROUP", group_id)


@router.put("/admin/groups/{group_id}/page-permissions",
            dependencies=[Depends(require_admin)])
def put_group_page_permissions(
    group_id: str, body: PagePermissionsIn,
    operator: str = Depends(require_admin),
) -> dict:
    return _put_subject_page_permissions("GROUP", group_id, body, operator)


@router.get("/me/page-permissions")
def my_page_permissions(
    me: tuple[str, bool] = Depends(require_console_user),
) -> dict:
    user_id, is_admin = me
    with closing(Database()) as db:
        grants = page_permissions.effective_permissions(
            db, user_id, is_admin=is_admin,
        )
        return {
            "userId": user_id, "isAdmin": is_admin,
            "permissions": list(grants.values()),
        }


def _pluto_admin_access_token(db: Database, request: Request) -> str:
    """CAP 目录查询只允许 Pluto 管理员会话，沿用 Flowable 的调用身份。"""
    ctx = getattr(request.state, "session_ctx", None)
    raw = console_auth.bearer_token(request)
    if ctx is None and raw:
        ctx = console_auth.auth_context_from_request(db, request)
    if ctx is None:
        raise errors.UnauthenticatedError("请先登录")
    if not ctx.is_admin():
        raise errors.ForbiddenError("需要管理员角色")
    if ctx.kind != "console":
        raise errors.ForbiddenError("请使用 Pluto 管理员账号操作")
    session = console_auth.console_session_from_token(db, raw)
    if session is None:
        raise errors.UnauthenticatedError("登录会话无效或已过期")
    token = (session.get("pluto_access_token") or "").strip()
    if not token:
        raise errors.ValidationError("当前 Pluto 会话没有可用的 access token，请重新登录")
    return token


def _directory_access_token(request: Request) -> str:
    """数据库目录只依赖 require_admin；HTTP 目录继续要求 Pluto 管理员 token。"""
    if cap_directory.database_backend_enabled():
        return ""
    with closing(Database()) as db:
        return _pluto_admin_access_token(db, request)


def _upsert_verified_user(db: Database, directory_user: dict) -> dict:
    if directory_user.get("status") != "active":
        raise errors.ValidationError("CAP 用户已停用，不能录入")
    user_id = (directory_user.get("userId") or "").strip()
    if not user_id:
        raise errors.ValidationError("CAP 用户缺少工号，不能录入")
    raw = directory_user.get("raw") or directory_user
    db.execute(
        """
        INSERT INTO eag_console_user
            (user_id, pluto_sub, preferred_username, email, wecom_id,
             display_name, department, raw_pluto_profile,
             profile_sync_status, status)
        VALUES (:uid, :sub, :pun, :email, :wecom,
                :dn, :dept, :raw, 'verified', 'active')
        ON DUPLICATE KEY UPDATE
            preferred_username=VALUES(preferred_username),
            email=VALUES(email),
            wecom_id=VALUES(wecom_id),
            display_name=VALUES(display_name),
            department=VALUES(department),
            raw_pluto_profile=VALUES(raw_pluto_profile),
            profile_sync_status=IF(profile_sync_status='synced', 'synced', 'verified'),
            status=VALUES(status)
        """,
        {
            "uid": user_id,
            "sub": directory_user.get("plutoSub") or f"verified:{user_id}",
            "pun": directory_user.get("preferredUsername") or directory_user.get("username") or user_id,
            "email": directory_user.get("email") or "",
            "wecom": directory_user.get("wecomId") or "",
            "dn": directory_user.get("displayName") or user_id,
            "dept": directory_user.get("department") or "",
            "raw": json.dumps(raw, ensure_ascii=False, default=str),
        },
    )
    db.execute(
        "INSERT IGNORE INTO eag_user_role (user_id, role) VALUES (:u, :r)",
        {"u": user_id, "r": console_auth.ROLE_USER},
    )
    user = db.query_one(
        """
        SELECT user_id, display_name, department, email, status,
               profile_sync_status, created_at, last_login_at
          FROM eag_console_user
         WHERE user_id=:u
        """,
        {"u": user_id},
    )
    if user is None:
        raise errors.DependencyError("用户录入失败")
    return _public_user(user, console_auth.user_roles(db, user_id))


@router.get("/admin/users", dependencies=[Depends(require_admin)])
def list_users(paging: Page | None = Depends(optional_page)) -> list[dict] | dict:
    """登录过的用户列表（权限管理·用户 Tab）。

    合并角色与可见服务数；按创建时间倒序（最近接入的排前面）。"""
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "admin-users", paging)
        users = db.query_all(
            """
            SELECT user_id, display_name, department, email, status,
                   profile_sync_status, created_at, last_login_at
              FROM eag_console_user
          ORDER BY created_at DESC
            """
        )
        roles = db.query_all("SELECT user_id, role FROM eag_user_role")
        counts = db.query_all(
            """
            SELECT t.cap_user_name AS u, COUNT(DISTINCT t.service_id) AS n
              FROM (
                SELECT gv.service_id, gm.cap_user_name
                  FROM eag_group_membership gm
                  JOIN eag_group_visibility gv ON gv.group_id = gm.group_id
                UNION
                SELECT service_id, cap_user_name FROM eag_user_visibility
              ) t
              JOIN eag_a2a_service_registration s ON s.service_id=t.service_id
             GROUP BY t.cap_user_name
            """
        )
        role_map: dict[str, list[str]] = {}
        for r in roles:
            role_map.setdefault(r["user_id"], []).append(r["role"])
        count_map = {c["u"]: c["n"] for c in counts}
        return [
            _public_user(
                u, role_map.get(u["user_id"], []), count_map.get(u["user_id"], 0),
            )
            for u in users
        ]


@router.get("/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def get_user(user_id: str) -> dict:
    """本地用户档案详情。"""
    with closing(Database()) as db:
        user = db.query_one(
            """
            SELECT user_id, display_name, department, email, status,
                   profile_sync_status, created_at, last_login_at
              FROM eag_console_user
             WHERE user_id=:u
            """,
            {"u": user_id},
        )
        if user is None:
            raise errors.NotFoundError("用户不存在")
        return _public_user(user, console_auth.user_roles(db, user_id))


class DirectorySearchIn(BaseModel):
    name: str = ""
    deptId: str = ""
    pageNo: int = Field(default=1, ge=1)
    pageSize: int = Field(default=10, ge=1, le=200)


@router.post("/admin/user-directory/search", dependencies=[Depends(require_admin)])
async def search_user_directory(
    body: DirectorySearchIn, request: Request,
) -> dict:
    """按部署模式查询 CAP/Pluto 用户目录。"""
    access_token = _directory_access_token(request)
    return await cap_directory.search_users(
        name=body.name, dept_id=body.deptId, page_no=body.pageNo,
        page_size=body.pageSize, pluto_access_token=access_token,
    )


@router.get("/admin/user-directory/{user_id}", dependencies=[Depends(require_admin)])
async def get_user_directory_detail(user_id: str, request: Request) -> dict:
    """创建前二次校验：按工号从 CAP/Pluto 目录读取真实用户。"""
    access_token = _directory_access_token(request)
    return await cap_directory.get_user(user_id, pluto_access_token=access_token)


class CreateUserIn(BaseModel):
    userId: str


@router.post("/admin/users", dependencies=[Depends(require_admin)])
async def create_user_from_directory(
    body: CreateUserIn, request: Request, operator: str = Depends(require_admin),
) -> dict:
    """管理员从 CAP/Pluto 目录录入真实员工，不创建登录会话。"""
    access_token = _directory_access_token(request)
    directory_user = await cap_directory.get_user(
        body.userId, pluto_access_token=access_token,
    )
    with closing(Database()) as db:
        user = _upsert_verified_user(db, directory_user)
        _audit(db, operator, "user.directory_import", user["userId"], {
            "by": operator, "source": "cap_pluto",
        })
        db.commit()
        return user


class RoleIn(BaseModel):
    isAdmin: bool


@router.put("/admin/users/{user_id}/role", dependencies=[Depends(require_admin)])
def set_user_role(user_id: str, body: RoleIn,
                  operator: str = Depends(require_admin)) -> dict:
    """授予/回收管理员角色。eagent_user 基础角色始终保留。"""
    if user_id == "admin":
        raise errors.ValidationError("内置 admin 账号的角色不可修改")
    with closing(Database()) as db:
        user = db.query_one(
            "SELECT user_id FROM eag_console_user WHERE user_id=:u",
            {"u": user_id},
        )
        if user is None:
            raise errors.NotFoundError("用户不存在（仅显示登录过的用户）")
        if body.isAdmin:
            db.execute(
                "INSERT IGNORE INTO eag_user_role (user_id, role) VALUES (:u, :r)",
                {"u": user_id, "r": "eagent_admin"},
            )
            action = "user.grant_admin"
        else:
            db.execute(
                "DELETE FROM eag_user_role WHERE user_id=:u AND role=:r",
                {"u": user_id, "r": "eagent_admin"},
            )
            action = "user.revoke_admin"
        db.execute(
            "INSERT IGNORE INTO eag_user_role (user_id, role) VALUES (:u, :r)",
            {"u": user_id, "r": "eagent_user"},
        )
        _audit(db, operator, action, user_id, {"by": operator})
        db.commit()
        return {"userId": user_id, "isAdmin": body.isAdmin,
                "roles": console_auth.user_roles(db, user_id)}


class StatusIn(BaseModel):
    status: str


@router.put("/admin/users/{user_id}/status", dependencies=[Depends(require_admin)])
def set_user_status(user_id: str, body: StatusIn,
                    operator: str = Depends(require_admin)) -> dict:
    """启用/禁用账号。禁用立即生效：登录 403 + 剔除存量会话（踢下线）。"""
    if body.status not in ("active", "disabled"):
        raise errors.ValidationError("status 仅支持 active / disabled")
    if user_id == "admin":
        raise errors.ValidationError("内置 admin 账号不可禁用")
    with closing(Database()) as db:
        user = db.query_one(
            "SELECT user_id FROM eag_console_user WHERE user_id=:u",
            {"u": user_id},
        )
        if user is None:
            raise errors.NotFoundError("用户不存在（仅显示登录过的用户）")
        db.execute(
            "UPDATE eag_console_user SET status=:s WHERE user_id=:u",
            {"s": body.status, "u": user_id},
        )
        if body.status == "disabled":
            db.execute(
                "DELETE FROM eag_console_session WHERE user_id=:u",
                {"u": user_id},
            )
        _audit(db, operator, f"user.{body.status}", user_id, {"by": operator})
        db.commit()
        return {"userId": user_id, "status": body.status}


@router.get("/admin/users/{user_id}/permissions",
            dependencies=[Depends(require_admin)])
def user_permission_detail(user_id: str) -> dict:
    """某用户的权限明细（用户 Tab 行展开）：所属组（含各组服务）+ 直授。"""
    with closing(Database()) as db:
        user = db.query_one(
            "SELECT user_id FROM eag_console_user WHERE user_id=:u",
            {"u": user_id},
        )
        if user is None:
            raise errors.NotFoundError("用户不存在")
        from ..permission import service as perm_svc
        roles = console_auth.user_roles(db, user_id)
        effective = page_permissions.effective_permissions(
            db, user_id,
            is_admin=console_auth.ROLE_ADMIN in roles,
        )
        return {
            "userId": user_id,
            **perm_svc.user_permission_detail(db, user_id),
            "pagePermissions": {
                "direct": page_permissions.direct_permissions(db, "USER", user_id),
                "effective": list(effective.values()),
            },
        }


@router.get("/me/permissions")
def my_permissions(me: tuple[str, bool] = Depends(require_console_user), paging: Page | None = Depends(optional_page)) -> dict:
    """我的权限（permission-control v1）：角色 + 可见服务（含来源）+
    管理员联系方式（线下申请渠道）。"""
    user_id, is_admin = me
    with closing(Database()) as db:
        roles = console_auth.user_roles(db, user_id)
        if isinstance(paging, Page):
            result = list_page(db, "my-services", paging, user_id=user_id)
            return {**result, "services": result["items"], "userId": user_id, "roles": roles,
                    "isAdmin": is_admin or "eagent_admin" in roles, "adminContact": settings.admin_contact}
        rows = db.query_all(
            """
            SELECT s.service_id, s.display_name, s.slug, t.source, t.group_name
              FROM (
                SELECT uv.service_id, 'direct' AS source, NULL AS group_name
                  FROM eag_user_visibility uv
                 WHERE uv.cap_user_name = :u
                UNION ALL
                SELECT gv.service_id, 'group' AS source, g.name AS group_name
                  FROM eag_group_membership gm
                  JOIN eag_user_group g ON g.group_id = gm.group_id
                  JOIN eag_group_visibility gv ON gv.group_id = gm.group_id
                 WHERE gm.cap_user_name = :u
              ) t
              JOIN eag_a2a_service_registration s ON s.service_id = t.service_id
          ORDER BY s.display_name
            """,
            {"u": user_id},
        )
        services = [
            {
                "serviceId": r["service_id"],
                "displayName": r["display_name"],
                "slug": r["slug"],
                "source": r["source"],
                "groupName": r["group_name"],
            }
            for r in rows
        ]
        # 去重（同一服务可能既在组里又被直授）：保留先出现的来源
        seen: set[str] = set()
        unique = [s for s in services
                  if not (s["serviceId"] in seen or seen.add(s["serviceId"]))]
        return {
            "userId": user_id,
            "roles": roles,
            "isAdmin": is_admin or "eagent_admin" in roles,
            "services": unique,
            "adminContact": settings.admin_contact,
        }
