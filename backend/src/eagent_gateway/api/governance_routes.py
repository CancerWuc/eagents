# -*- coding: utf-8 -*-
"""治理面 REST: Release / Installation / Instance / Trace
（from_nekiro/02、04、06；P0-2 / P0-4 / P1-2）。

错误响应统一为 platform-error.v1（见 api/error_handlers.py）。
"""
from contextlib import closing
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from ..catalog import service as catalog
from ..config import settings
from ..errors import NotFoundError
from ..permission.scopes import require_agent_manage
from ..installation import service as installation_svc
from ..persistence.db import Database
from ..registry import (
    InstanceInput,
    MysqlInstanceDirectory,
)
from ..registry.model import READY
from ..release import service as release_svc
from ..router import ledger
from .pagination import Page, optional_page
from .list_pages import list_page
from .serializers import _instance_out

router = APIRouter(prefix="/api/v1")


def _user(cap_user_name: str | None) -> str:
    return cap_user_name or "anonymous"


# ===========================================================================
# Release 管理
# ===========================================================================
class ReleaseIn(BaseModel):
    agentVersion: str | None = None


@router.post("/services/{service_id}/releases", dependencies=[Depends(require_agent_manage)])
def create_release(
    service_id: str, body: ReleaseIn | None = None,
    cap_user_name: str | None = Header(None),
):
    """基于服务当前 Card Revision 创建 DRAFT Release。"""
    with closing(Database()) as db:
        return release_svc.create_release(
            db,
            service_id=service_id,
            agent_version=body.agentVersion if body else None,
            created_by=_user(cap_user_name),
        )


@router.get("/services/{service_id}/releases", dependencies=[Depends(require_agent_manage)])
def list_releases(service_id: str, paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "releases", paging, service_id=service_id)
        return release_svc.list_releases(db, service_id)


@router.post("/releases/{release_id}:publish", dependencies=[Depends(require_agent_manage)])
def publish_release(release_id: str):
    with closing(Database()) as db:
        return release_svc.publish_release(db, release_id)


@router.post("/releases/{release_id}:suspend", dependencies=[Depends(require_agent_manage)])
def suspend_release(release_id: str):
    with closing(Database()) as db:
        return release_svc.suspend_release(db, release_id)


@router.post("/releases/{release_id}:revoke", dependencies=[Depends(require_agent_manage)])
def revoke_release(release_id: str):
    with closing(Database()) as db:
        return release_svc.revoke_release(db, release_id)


# ===========================================================================
# Installation 管理
# ===========================================================================
class InstallIn(BaseModel):
    serviceId: str
    releaseId: str
    acceptedPermissions: list[str] | None = None


@router.post("/workspaces/{workspace_id}/installations", dependencies=[Depends(require_agent_manage)])
def install_release(workspace_id: str, body: InstallIn):
    with closing(Database()) as db:
        return installation_svc.install(
            db,
            workspace_id=workspace_id,
            service_id=body.serviceId,
            release_id=body.releaseId,
            accepted_permissions=body.acceptedPermissions,
        )


@router.get("/workspaces/{workspace_id}/installations", dependencies=[Depends(require_agent_manage)])
def list_installations(workspace_id: str, paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "installations", paging, workspace_id=workspace_id)
        return installation_svc.list_installations(db, workspace_id)


class InstallationPatch(BaseModel):
    status: str = Field(pattern="^(ENABLED|DISABLED)$")


@router.patch("/installations/{installation_id}", dependencies=[Depends(require_agent_manage)])
def patch_installation(installation_id: str, body: InstallationPatch):
    with closing(Database()) as db:
        return installation_svc.set_status(db, installation_id, body.status)


@router.delete("/installations/{installation_id}", dependencies=[Depends(require_agent_manage)])
def uninstall_installation(installation_id: str):
    with closing(Database()) as db:
        return installation_svc.uninstall(db, installation_id)


# ===========================================================================
# Trace 查询
# ===========================================================================
@router.get("/traces/{trace_id}", dependencies=[Depends(require_agent_manage)])
def get_trace(trace_id: str):
    with closing(Database()) as db:
        trace = ledger.get_trace(db, trace_id)
        if not trace:
            raise NotFoundError("Trace 不存在")
        return trace


# ===========================================================================
# 实例注册/心跳/注销
# ===========================================================================
class InstanceRegisterIn(BaseModel):
    serviceId: str
    instanceName: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(pattern="^https?://")
    leaseTtlSeconds: int | None = Field(default=None, ge=5, le=600)
    metadata: dict | None = None
    draining: bool = False


@router.post("/instances/register")
def register_instance(body: InstanceRegisterIn):
    with closing(Database()) as db:
        if not catalog.get_service(db, body.serviceId):
            raise NotFoundError("服务不存在")
        directory = MysqlInstanceDirectory(db)
        instance = directory.register(
            InstanceInput(
                service_id=body.serviceId,
                instance_name=body.instanceName,
                endpoint=body.endpoint,
                lease_ttl_seconds=(
                    body.leaseTtlSeconds or settings.instance_default_lease_s
                ),
                metadata=body.metadata or {},
                state="DRAINING" if body.draining else READY,
            ),
        )
        return _instance_out(instance)


@router.post("/instances/{instance_id}:heartbeat")
def heartbeat_instance(instance_id: str, lease_ttl_seconds: int | None = None):
    with closing(Database()) as db:
        directory = MysqlInstanceDirectory(db)
        instance = directory.heartbeat(
            instance_id,
            lease_ttl_seconds or settings.instance_default_lease_s,
        )
        return _instance_out(instance)


@router.post("/instances/{instance_id}:deregister")
def deregister_instance(instance_id: str):
    with closing(Database()) as db:
        return _instance_out(
            MysqlInstanceDirectory(db).deregister(instance_id),
        )


@router.get("/services/{service_id}/instances", dependencies=[Depends(require_agent_manage)])
def list_instances(service_id: str, paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "instances", paging, service_id=service_id)
        return [
            _instance_out(i)
            for i in MysqlInstanceDirectory(db).snapshot(service_id)
        ]
