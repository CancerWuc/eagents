"""User-group membership and service-visibility endpoints."""
from contextlib import closing
import json

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from ..errors import ConflictError, NotFoundError, ValidationError
from ..permission.scopes import require_admin
from ..persistence.db import Database, new_id, now
from .pagination import Page, optional_page
from .list_pages import list_page
from .route_helpers import _user

router = APIRouter(prefix="/api/v1")


def _normalize_user_ids(values: list[str]) -> list[str]:
    """Strip blanks and duplicates while keeping the first occurrence."""
    names = [value.strip() for value in values if value.strip()]
    return list(dict.fromkeys(names))


def _validate_grant_users(db: Database, user_ids: list[str]) -> None:
    """个人直授只能分配给已建档且启用的员工用户。"""
    missing: list[str] = []
    disabled: list[str] = []
    for user_id in user_ids:
        row = db.query_one(
            "SELECT status FROM eag_console_user WHERE user_id=:u",
            {"u": user_id},
        )
        if row is None:
            missing.append(user_id)
        elif row["status"] != "active":
            disabled.append(user_id)
    if missing:
        raise ValidationError(
            "用户不存在，请先让员工登录建档: " + ", ".join(missing),
        )
    if disabled:
        raise ValidationError(
            "用户已禁用，不能分配个人直授: " + ", ".join(disabled),
        )


# ===========================================================================
# 权限管理（§6）
# ===========================================================================
class GroupIn(BaseModel):
    name: str


@router.get("/groups", dependencies=[Depends(require_admin)])
def list_groups(paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "groups", paging)
        groups = db.query_all("SELECT * FROM eag_user_group ORDER BY name")
        for g in groups:
            g["members"] = [
                r["cap_user_name"] for r in db.query_all(
                    "SELECT cap_user_name FROM eag_group_membership"
                    " WHERE group_id=:gid", {"gid": g["group_id"]},
                )
            ]
            g["service_ids"] = [
                r["service_id"] for r in db.query_all(
                    "SELECT v.service_id FROM eag_group_visibility v"
                    " JOIN eag_a2a_service_registration s ON s.service_id=v.service_id"
                    " WHERE v.group_id=:gid", {"gid": g["group_id"]},
                )
            ]
        return groups


@router.post("/groups", dependencies=[Depends(require_admin)])
def create_group(body: GroupIn):
    with closing(Database()) as db:
        if db.query_one(
            "SELECT 1 AS x FROM eag_user_group WHERE name=:n", {"n": body.name},
        ):
            raise ConflictError("用户组已存在")
        gid = new_id("grp_")
        db.execute(
            "INSERT INTO eag_user_group (group_id, name, created_at)"
            " VALUES (:gid, :name, :ts)",
            {"gid": gid, "name": body.name, "ts": now()},
        )
        db.commit()
        return {"group_id": gid, "name": body.name}


@router.delete("/groups/{group_id}", dependencies=[Depends(require_admin)])
def delete_group(group_id: str, operator: str = Depends(require_admin)):
    """删除用户组并清理成员、服务可见性和页面授权。"""
    with closing(Database()) as db:
        group = db.query_one(
            "SELECT group_id, name FROM eag_user_group WHERE group_id=:gid",
            {"gid": group_id},
        )
        if group is None:
            raise NotFoundError("用户组不存在")
        memberships = db.execute(
            "DELETE FROM eag_group_membership WHERE group_id=:gid",
            {"gid": group_id},
        ).rowcount
        visibilities = db.execute(
            "DELETE FROM eag_group_visibility WHERE group_id=:gid",
            {"gid": group_id},
        ).rowcount
        page_permissions = db.execute(
            "DELETE FROM eag_page_permission "
            "WHERE subject_type='GROUP' AND subject_id=:gid",
            {"gid": group_id},
        ).rowcount
        db.execute(
            "DELETE FROM eag_user_group WHERE group_id=:gid",
            {"gid": group_id},
        )
        db.execute(
            "INSERT INTO eag_audit_event (audit_id, cap_user_name, action,"
            " resource_type, resource_id, detail, created_at)"
            " VALUES (:a, :u, 'group.delete', 'user_group', :gid, :d, :ts)",
            {
                "a": new_id("aud_"),
                "u": operator or "admin",
                "gid": group_id,
                "d": json.dumps({
                    "name": group["name"],
                    "deleted_memberships": memberships,
                    "deleted_visibilities": visibilities,
                    "deleted_page_permissions": page_permissions,
                }, ensure_ascii=False),
                "ts": now(),
            },
        )
        db.commit()
        return {
            "deleted": group_id,
            "name": group["name"],
            "deletedMemberships": memberships,
            "deletedVisibilities": visibilities,
            "deletedPagePermissions": page_permissions,
        }


class MembersIn(BaseModel):
    capUserNames: list[str]


@router.put("/groups/{group_id}/members", dependencies=[Depends(require_admin)])
def put_members(group_id: str, body: MembersIn):
    with closing(Database()) as db:
        names = _normalize_user_ids(body.capUserNames)
        _validate_grant_users(db, names)
        db.execute(
            "DELETE FROM eag_group_membership WHERE group_id=:gid",
            {"gid": group_id},
        )
        ts = now()
        for name in names:
            db.execute(
                "INSERT INTO eag_group_membership (group_id, cap_user_name,"
                " created_at) VALUES (:gid, :u, :ts)"
                " ON DUPLICATE KEY UPDATE cap_user_name=cap_user_name",
                {"gid": group_id, "u": name, "ts": ts},
            )
        db.commit()
        return {"ok": True}


class ServicesIn(BaseModel):
    serviceIds: list[str]


@router.put("/groups/{group_id}/services", dependencies=[Depends(require_admin)])
def put_group_services(group_id: str, body: ServicesIn):
    with closing(Database()) as db:
        db.execute(
            "DELETE FROM eag_group_visibility WHERE group_id=:gid",
            {"gid": group_id},
        )
        ts = now()
        for sid in body.serviceIds:
            db.execute(
                "INSERT INTO eag_group_visibility (group_id, service_id,"
                " created_at) VALUES (:gid, :sid, :ts)"
                " ON DUPLICATE KEY UPDATE service_id=service_id",
                {"gid": group_id, "sid": sid, "ts": ts},
            )
        db.commit()
        return {"ok": True}


@router.get("/users/known", dependencies=[Depends(require_admin)])
def list_known_users():
    """系统见过的工号候选池，供管理台做成员选择的自动补全。

    ⚠️ 这**不是**员工名录：库里没有用户主表，也没有对接组织架构/HR，
    cap_user_name 只是请求头里的自由文本。这里只能把散落在各表里出现过的工号
    汇总出来，用途是减少手输错字，不能当作"全部员工"或权威身份来源。
    因此管理台必须同时保留手工输入（新员工不会出现在任何表里）。

    来源按可信度排序：调用过总控 > 已在组内 > 已有直授 > 操作过管理台。
    """
    with closing(Database()) as db:
        rows = db.query_all(
            """
            SELECT cap_user_name, 'RUN' AS src, MAX(started_at) AS seen_at
              FROM eag_run_record GROUP BY cap_user_name
            UNION ALL
            SELECT cap_user_name, 'GROUP', MAX(created_at)
              FROM eag_group_membership GROUP BY cap_user_name
            UNION ALL
            SELECT cap_user_name, 'DIRECT', MAX(created_at)
              FROM eag_user_visibility GROUP BY cap_user_name
            UNION ALL
            SELECT cap_user_name, 'ANNOTATE', MAX(created_at)
              FROM eag_annotation GROUP BY cap_user_name
            UNION ALL
            SELECT cap_user_name, 'ADMIN', MAX(created_at)
              FROM eag_audit_event GROUP BY cap_user_name
            """,
        )
        rank = {"RUN": 0, "GROUP": 1, "DIRECT": 2, "ANNOTATE": 3, "ADMIN": 4}
        merged: dict[str, dict] = {}
        for r in rows:
            user = (r["cap_user_name"] or "").strip()
            if not user:
                continue
            entry = merged.setdefault(
                user, {"cap_user_name": user, "sources": [], "seen_at": None},
            )
            if r["src"] not in entry["sources"]:
                entry["sources"].append(r["src"])
            if r["seen_at"] and (
                entry["seen_at"] is None or r["seen_at"] > entry["seen_at"]
            ):
                entry["seen_at"] = r["seen_at"]
        users = sorted(
            merged.values(),
            key=lambda e: (min(rank.get(s, 9) for s in e["sources"]),
                           e["cap_user_name"]),
        )
        for entry in users:
            entry["sources"].sort(key=lambda s: rank.get(s, 9))
        return {"users": users}


@router.get("/users/directs", dependencies=[Depends(require_admin)])
def list_user_directs(paging: Page | None = Depends(optional_page)):
    """全部个人直授记录（治理面总览）。

    个人直授只允许指向 eag_console_user 中已建档的员工；这里按直授边表
    汇总，便于管理员回看和编辑。
    """
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "directs", paging)
        rows = db.query_all(
            "SELECT v.cap_user_name, v.service_id, v.created_at,"
            " s.slug, s.display_name"
            " FROM eag_user_visibility v"
            " JOIN eag_a2a_service_registration s USING(service_id)"
            " ORDER BY v.cap_user_name, v.created_at",
        )
        grouped: dict[str, dict] = {}
        for r in rows:
            entry = grouped.setdefault(
                r["cap_user_name"],
                {"cap_user_name": r["cap_user_name"], "services": []},
            )
            entry["services"].append({
                "service_id": r["service_id"], "slug": r["slug"],
                "display_name": r["display_name"],
                "created_at": r["created_at"],
            })
        return {"users": list(grouped.values())}


@router.get("/users/{cap_user_name}/services", dependencies=[Depends(require_admin)])
def get_user_services(cap_user_name: str):
    """某工号的个人直授（不含组继承）。

    PUT 是「全量替换」语义，管理台必须先拿到现有集合回填，否则保存时会把没勾上
    的旧授权静默清空。
    """
    with closing(Database()) as db:
        return {
            "cap_user_name": cap_user_name,
            "service_ids": [
                r["service_id"] for r in db.query_all(
                    "SELECT v.service_id FROM eag_user_visibility v"
                    " JOIN eag_a2a_service_registration s ON s.service_id=v.service_id"
                    " WHERE v.cap_user_name=:u", {"u": cap_user_name},
                )
            ],
        }


@router.put("/users/{cap_user_name}/services", dependencies=[Depends(require_admin)])
def put_user_services(cap_user_name: str, body: ServicesIn):
    """全量替换该工号的个人直授（先删后插）。"""
    with closing(Database()) as db:
        user = cap_user_name.strip()
        if not user:
            raise ValidationError("工号不能为空")
        if body.serviceIds:
            _validate_grant_users(db, [user])
        db.execute(
            "DELETE FROM eag_user_visibility WHERE cap_user_name=:u",
            {"u": user},
        )
        ts = now()
        for sid in body.serviceIds:
            db.execute(
                "INSERT INTO eag_user_visibility (cap_user_name, service_id,"
                " created_at) VALUES (:u, :sid, :ts)"
                " ON DUPLICATE KEY UPDATE service_id=service_id",
                {"u": user, "sid": sid, "ts": ts},
            )
        db.commit()
        return {"ok": True}


# --------------------------------------------------------------------------
# 服务维度授权（admin-ui 02 §2.7）：按服务配置可使用的用户工号列表。
# 底层复用 eag_user_visibility 个人直授边表（转置视角，全量替换），
# 运行时可见性计算（组可见 ∪ 个人直授）不变，存量组数据继续生效。
# --------------------------------------------------------------------------
@router.get("/services/{service_id}/users", dependencies=[Depends(require_admin)])
def get_service_users(service_id: str):
    with closing(Database()) as db:
        if not db.query_one(
            "SELECT 1 AS x FROM eag_a2a_service_registration"
            " WHERE service_id=:sid", {"sid": service_id},
        ):
            raise NotFoundError("服务不存在")
        rows = db.query_all(
            "SELECT cap_user_name FROM eag_user_visibility"
            " WHERE service_id=:sid ORDER BY cap_user_name",
            {"sid": service_id},
        )
        return {"service_id": service_id, "capUserNames": [r["cap_user_name"] for r in rows]}


@router.put("/services/{service_id}/users", dependencies=[Depends(require_admin)])
def put_service_users(service_id: str, body: MembersIn):
    """全量替换该服务的授权用户；空列表 = 清空授权（全员不可见，deny-by-default）。"""
    with closing(Database()) as db:
        if not db.query_one(
            "SELECT 1 AS x FROM eag_a2a_service_registration"
            " WHERE service_id=:sid", {"sid": service_id},
        ):
            raise NotFoundError("服务不存在")
        # 去空格、去重、保序
        names = _normalize_user_ids(body.capUserNames)
        _validate_grant_users(db, names)
        db.execute(
            "DELETE FROM eag_user_visibility WHERE service_id=:sid",
            {"sid": service_id},
        )
        ts = now()
        for name in names:
            db.execute(
                "INSERT INTO eag_user_visibility (cap_user_name, service_id,"
                " created_at) VALUES (:u, :sid, :ts)"
                " ON DUPLICATE KEY UPDATE service_id=service_id",
                {"u": name, "sid": service_id, "ts": ts},
            )
        db.commit()
        return {"service_id": service_id, "capUserNames": names}


@router.get("/users/me/visibility")
def my_visibility(cap_user_name: str | None = Header(None)):
    from ..permission import service as perm_svc

    with closing(Database()) as db:
        return {
            "cap_user_name": _user(cap_user_name),
            "services": perm_svc.visible_services_detail(db, _user(cap_user_name)),
        }
