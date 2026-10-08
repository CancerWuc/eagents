# -*- coding: utf-8 -*-
"""权限（§6）: cap-user-name → 可见服务子集 = 组可见 ∪ 个人直授。

安全语义（deny-by-default）：
- 没有任何映射记录的用户 → 可见集合为空（不可见任何服务）
- 身份缺失的请求在鉴权层已被拒绝，不会进入权限解析
- 「全员默认」等开放策略必须由管理员显式建组实现
"""
from ..persistence.db import Database


def visible_service_ids(db: Database, cap_user_name: str) -> list[str]:
    # 匿名（空用户）硬空集：防止给 "anonymous" 配了映射后放行所有匿名调用
    # （review P1-2；deny-by-default 的显式表达）
    if not cap_user_name:
        return []
    rows = db.query_all(
        """
        SELECT DISTINCT t.service_id FROM (
            SELECT gv.service_id
              FROM eag_group_membership gm
              JOIN eag_group_visibility gv ON gv.group_id = gm.group_id
             WHERE gm.cap_user_name = :u
            UNION
            SELECT service_id FROM eag_user_visibility WHERE cap_user_name = :u
        ) t
        JOIN eag_a2a_service_registration s ON s.service_id=t.service_id
        """,
        {"u": cap_user_name},
    )
    return [r["service_id"] for r in rows]


def visible_services_detail(db: Database, cap_user_name: str) -> list[dict]:
    ids = visible_service_ids(db, cap_user_name)
    if not ids:
        return []
    placeholders = ", ".join(f":v{i}" for i in range(len(ids)))
    params = {f"v{i}": v for i, v in enumerate(ids)}
    return db.query_all(
        f"SELECT service_id, slug, display_name, enabled, compatibility"
        f" FROM eag_a2a_service_registration WHERE service_id IN ({placeholders})",
        params,
    )


def user_permission_detail(db: Database, cap_user_name: str) -> dict:
    """权限明细（permission-control v1 联动视图）：按来源拆分——
    所属组（含各组可见服务）与个人直授，供管理端展示与逐层追溯。"""
    if not cap_user_name:
        return {"groups": [], "direct": []}
    groups = db.query_all(
        """
        SELECT g.group_id, g.name AS group_name,
               (SELECT COUNT(*) FROM eag_group_membership m2
                 WHERE m2.group_id = g.group_id) AS member_count
          FROM eag_group_membership gm
          JOIN eag_user_group g ON g.group_id = gm.group_id
         WHERE gm.cap_user_name = :u
         ORDER BY g.name
        """,
        {"u": cap_user_name},
    )
    out = []
    for g in groups:
        svcs = db.query_all(
            """
            SELECT s.service_id, s.slug, s.display_name
              FROM eag_group_visibility gv
              JOIN eag_a2a_service_registration s ON s.service_id = gv.service_id
             WHERE gv.group_id = :gid
             ORDER BY s.display_name
            """,
            {"gid": g["group_id"]},
        )
        out.append({
            "groupId": g["group_id"],
            "groupName": g["group_name"],
            "memberCount": g["member_count"],
            "services": [
                {"serviceId": s["service_id"], "slug": s["slug"],
                 "displayName": s["display_name"]}
                for s in svcs
            ],
        })
    direct = db.query_all(
        """
        SELECT s.service_id, s.slug, s.display_name
          FROM eag_user_visibility uv
          JOIN eag_a2a_service_registration s ON s.service_id = uv.service_id
         WHERE uv.cap_user_name = :u
         ORDER BY s.display_name
        """,
        {"u": cap_user_name},
    )
    return {
        "groups": out,
        "direct": [
            {"serviceId": s["service_id"], "slug": s["slug"],
             "displayName": s["display_name"]}
            for s in direct
        ],
    }
