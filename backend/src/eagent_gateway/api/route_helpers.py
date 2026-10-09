"""Shared management-route constants and lookup helpers."""
from ..errors import NotFoundError
from ..persistence.db import Database
from ..runtime import master_service


# slug 直接进 URL 路径（总控 → /a2a/{slug}，服务 → 工具名域），必须是
# URL 安全的 ASCII。与 KB 契约同一口径（contracts/schemas/kb.v1.schema.json）。
# ⚠️ 仅拦新写入：库中已有的非法 slug（如中文）不回溯改写，需人工订正。
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,63}$"


def _user(cap_user_name: str | None) -> str:
    return cap_user_name or "anonymous"


def _require_master(db: Database, master_id: str) -> dict:
    """子资源写操作前先确认总控存在，不存在直接 404。

    不查的后果不是"少个报错"而是脏数据：PUT bindings / POST prompts 都是
    INSERT，master 不存在时会静默写出孤儿行，级联删除也带不走它们
    （实际发生过：总控删掉后又被一次 PUT 把绑定写了回来）。
    UPDATE 类的则是 0 行受影响却返回 200 + null，调用方以为成功了。
    """
    master = master_service.get_master(db, master_id)
    if not master:
        raise NotFoundError("Agent Team 不存在")
    return master
