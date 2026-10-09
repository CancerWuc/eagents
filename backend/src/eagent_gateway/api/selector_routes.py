"""Management-console selector endpoints."""
from contextlib import closing

from fastapi import APIRouter, Depends, Query

from ..errors import NotFoundError
from ..permission.page_permissions import require_page, require_selector_access
from ..persistence.db import Database

router = APIRouter(prefix="/api/v1")


@router.get("/selectors/services")
def service_selector(for_page: str = Depends(require_selector_access)) -> dict:
    with closing(Database()) as db:
        rows = db.query_all(
            "SELECT service_id, slug, display_name, source_type, enabled "
            "FROM eag_a2a_service_registration ORDER BY display_name",
        )
        return {"items": [
            {"serviceId": r["service_id"], "slug": r["slug"],
             "displayName": r["display_name"], "sourceType": r["source_type"],
             "enabled": bool(r["enabled"])} for r in rows
        ]}


@router.get("/selectors/agents")
def agent_selector(for_page: str = Depends(require_selector_access)) -> dict:
    with closing(Database()) as db:
        rows = db.query_all(
            "SELECT master_id, slug, display_name, enabled "
            "FROM eag_master_agent ORDER BY display_name",
        )
        return {"items": [
            {"masterId": r["master_id"], "slug": r["slug"],
             "displayName": r["display_name"], "enabled": bool(r["enabled"])}
            for r in rows
        ]}


@router.get("/selectors/releases")
def release_selector(
    serviceId: str = Query(..., min_length=1, max_length=64),
    for_page: str = Depends(require_selector_access),
) -> dict:
    with closing(Database()) as db:
        rows = db.query_all(
            "SELECT release_id, agent_version, state FROM eag_agent_release "
            "WHERE service_id=:sid ORDER BY created_at DESC",
            {"sid": serviceId},
        )
        return {"items": [
            {"releaseId": r["release_id"], "agentVersion": r["agent_version"],
             "state": r["state"]} for r in rows
        ]}


@router.get("/selectors/knowledge-bases")
def knowledge_base_selector(
    for_page: str = Depends(require_selector_access),
) -> dict:
    with closing(Database()) as db:
        rows = db.query_all(
            "SELECT kb_id, slug, display_name, status FROM eag_knowledge_base "
            "ORDER BY display_name",
        )
        return {"items": [
            {"kbId": r["kb_id"], "slug": r["slug"],
             "displayName": r["display_name"], "status": r["status"]}
            for r in rows
        ]}


@router.get("/selectors/agents/{master_id}/loop-context",
            dependencies=[Depends(require_page("loop", "view"))])
def agent_loop_context(master_id: str) -> dict:
    with closing(Database()) as db:
        master = db.query_one(
            "SELECT master_id, current_prompt_version_id FROM eag_master_agent "
            "WHERE master_id=:mid", {"mid": master_id},
        )
        if not master:
            raise NotFoundError("Agent Team 不存在")
        bindings = db.query_all(
            "SELECT tool_name FROM eag_master_binding WHERE master_id=:mid",
            {"mid": master_id},
        )
        prompt = None
        if master.get("current_prompt_version_id"):
            prompt = db.query_one(
                "SELECT prompt_version_id, version_no, prompt_text FROM "
                "eag_master_prompt_version WHERE prompt_version_id=:pid",
                {"pid": master["current_prompt_version_id"]},
            )
        return {"toolNames": [b["tool_name"] for b in bindings],
                "currentPrompt": prompt}
