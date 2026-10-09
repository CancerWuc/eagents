"""Management REST router assembly and compatibility entrypoints."""
from fastapi import APIRouter

from . import (
    access_routes,
    agent_activity_routes,
    agent_team_routes,
    enhancement_routes,
    selector_routes,
    service_registry_routes,
)
from .agent_activity_routes import publish_prompt as publish_prompt

router = APIRouter()
router.include_router(selector_routes.router)
router.include_router(service_registry_routes.router)
router.include_router(enhancement_routes.router)
router.include_router(service_registry_routes.detail_router)
router.include_router(agent_team_routes.router)
router.include_router(agent_activity_routes.router)
router.include_router(agent_team_routes.export_router)
router.include_router(access_routes.router)
