"""Agent-team configuration, tool-binding and standalone-export endpoints."""
from contextlib import closing
import json
from typing import Literal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from ..catalog import service as catalog
from ..errors import ConflictError, DependencyError, NotFoundError, ValidationError
from ..permission.scopes import require_agent_manage
from ..persistence.db import Database, new_id, now
from ..runtime import master_service
from ..runtime.model_factory import parse_model_config
from ..runtime.prompt_middleware import get_published_prompt
from .pagination import Page, optional_page
from .list_pages import teams_page
from .route_helpers import SLUG_PATTERN, _user, _require_master
from .serializers import _public_master

router = APIRouter(prefix="/api/v1")


# ===========================================================================
# 统一 Agent 目录 / 注册（COMPOSITE + HIAGENT 来源）
# ===========================================================================
class MasterIn(BaseModel):
    model_config = {"extra": "forbid"}
    slug: str = Field(pattern=SLUG_PATTERN)
    displayName: str = Field(min_length=1, max_length=128)
    description: str | None = None
    maxIters: int = Field(default=8, ge=1, le=50)
    # 来源选择（缺省 COMPOSITE 总控；HIAGENT = HIA agent 注册）
    sourceType: str = Field(default="COMPOSITE", pattern=r"^(COMPOSITE|HIAGENT)$")
    # HIAGENT 专属：实例级服务地址（空=全局默认兜底链）、app_key（加密落库
    # 永不回显）、生成式 Card 素材、service_parameters 参数键声明
    baseUrl: str | None = None
    appKey: str | None = None
    skills: list[dict] | None = None
    inputsSchema: dict | None = None
    connectionTest: dict | None = None


@router.get("/agents")
async def list_agents(paging: Page | None = Depends(optional_page)):
    """统一 Agent 目录: COMPOSITE + remote（A2A_REMOTE / HIAGENT，带 sourceType）。"""
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return await teams_page(db, paging)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_legacy_agent_directory, db)


def _legacy_agent_directory(db):
    masters = [_public_master(m) for m in master_service.list_masters(db)]
    remotes = db.query_all(
        "SELECT service_id, slug, display_name, compatibility, enabled,"
        " source_type, created_at"
        " FROM eag_a2a_service_registration ORDER BY created_at DESC",
    )
    return {"composite": masters, "remote": remotes}


@router.post("/agents", dependencies=[Depends(require_agent_manage)])
async def create_agent(body: MasterIn, cap_user_name: str | None = Header(None)):
    """统一注册入口：sourceType=COMPOSITE 建总控（原 POST /agents 语义不变）；
    HIAGENT 注册 HIA agent 为标准单 Agent A2A 服务（注册即连通性测试，
    参数校验失败 400、远端失败 502，均不落库；成功立即挂载
    /a2a/{slug}/）。"""
    if body.sourceType == "HIAGENT":
        return await _create_hiagent_agent(body, cap_user_name)
    return _create_composite(body)


def _create_composite(body: MasterIn):
    with closing(Database()) as db:
        if master_service.get_master_by_slug(db, body.slug):
            raise ConflictError("slug 已存在")
        # slug 跨表唯一：HIAGENT 注册服务已挂 /a2a/{slug}，不允许抢占
        if db.query_one(
            "SELECT service_id FROM eag_a2a_service_registration WHERE slug=:s",
            {"s": body.slug},
        ):
            raise ConflictError("slug 已存在（服务注册表）")
        ts = now()
        master_id = new_id("mst_")
        db.execute(
            """
            INSERT INTO eag_master_agent
                (master_id, slug, display_name, description, source_type,
                 enabled, react_config_json, created_at, updated_at)
            VALUES (:mid, :slug, :dn, :dsc, 'COMPOSITE', 1, :cfg, :ts, :ts)
            """,
            {
                "mid": master_id, "slug": body.slug, "dn": body.displayName,
                "dsc": body.description,
                "cfg": json.dumps({"max_iters": body.maxIters}),
                "ts": ts,
            },
        )
        # 建总控即种入 PUBLISHED 基线提示词（与 master 行同事务）。
        # 否则要等下次进程启动的 seed_missing_prompts 才补上，这期间：
        # ①「提示词版本」页空白，运维没有可编辑/发布的基线；
        # ② run_record.prompt_version_id 为空，运行记录无法归属到提示词版本
        #    —— Loop Engineer 的评测与优化正建立在这个归属上。
        from ..runtime.seed_prompt import seed_prompt_for

        seed_prompt_for(db, master_id)
        db.commit()
        master = master_service.get_master(db, master_id)
        # 运行期挂载 A2A 端点：新建后立即可被调用，不再需要重启进程
        from ..a2a_server import mounts

        mounts.mount_master(db, master)
        from .team_liveness import invalidate_team
        invalidate_team(master["slug"])
        return _public_master(master)


async def _create_hiagent_agent(body: MasterIn, cap_user_name: str | None):
    from ..gateway.errors import GatewayConfigError
    from ..hiagent import service as hia_service

    with closing(Database()) as db:
        try:
            svc = await hia_service.register_hiagent_agent(
                db,
                slug=body.slug,
                display_name=body.displayName,
                description=body.description,
                base_url=body.baseUrl,
                app_key=body.appKey,
                skills=body.skills,
                inputs_schema=body.inputsSchema,
                connection_test=body.connectionTest,
                created_by=_user(cap_user_name),
            )
        except catalog.SlugConflictError as exc:
            raise ConflictError(str(exc)) from exc
        except GatewayConfigError as exc:
            # 实例级 AppKey 加密依赖网关主密钥（fail-secure）——未配置属部署
            # 配置错误，给明确的 400 指引而非 500
            raise ValidationError(
                f"{exc.message}；填写了实例级 AppKey 的注册依赖该密钥，"
                "不使用实例级 AppKey 可先完成注册",
            ) from exc
        except hia_service.HiagentRegistrationValidationError as exc:
            raise ValidationError(str(exc)) from exc
        except hia_service.HiagentRegistrationError as exc:
            # 连通性测试失败（含鉴权失败/未配置 api_key）→ 依赖错误 502
            raise DependencyError(str(exc)) from exc
        from ..a2a_server import mounts

        mounts.mount_hiagent(db, svc)
        return svc


@router.get("/agents/{master_id}")
def get_master(master_id: str):
    with closing(Database()) as db:
        master = master_service.get_master(db, master_id)
        if not master:
            raise NotFoundError("Agent Team 不存在")
        bindings = db.query_all(
            """
            SELECT b.*, s.slug AS service_slug, s.display_name AS service_name,
                   s.enabled AS service_enabled, s.compatibility
              FROM eag_master_binding b
              JOIN eag_a2a_service_registration s USING (service_id)
             WHERE b.master_id=:mid
            """,
            {"mid": master_id},
        )
        _, prompt_version_id = get_published_prompt(db, master_id)
        prompts = db.query_all(
            """SELECT prompt_version_id, version_no, status, source,
                      created_by, created_at, eval_report_json
                 FROM eag_master_prompt_version WHERE master_id=:mid
                ORDER BY version_no DESC""",
            {"mid": master_id},
        )
        runs_stat = db.query_one(
            """SELECT COUNT(*) AS total,
                      SUM(status='SUCCEEDED') AS ok,
                      SUM(status='FAILED') AS failed
                 FROM eag_run_record WHERE master_id=:mid""",
            {"mid": master_id},
        )
        return {
            "master": _public_master(master),
            "bindings": bindings,
            "current_prompt_version_id": prompt_version_id,
            "prompts": prompts,
            "runs_stat": runs_stat,
        }


# 总控的从属数据散在 12 张表，库内不建外键（schema.sql 约定「不使用外键约束；
# 服务内保证」），因此级联必须在这里显式按「叶子 → 根」顺序删，否则留下
# 无法关联归属的孤儿行。两级从属用子查询定位（子查询目标表均非删除目标，
# MySQL 允许）。顺序即列表顺序，勿随意调整。
_MASTER_CASCADE: tuple[tuple[str, str], ...] = (
    # 评测：标注 → 用例 → 数据集
    ("eag_annotation",
     "case_id IN (SELECT case_id FROM eag_eval_case WHERE dataset_id IN"
     " (SELECT dataset_id FROM eag_eval_dataset WHERE master_id=:mid))"),
    ("eag_eval_case",
     "dataset_id IN (SELECT dataset_id FROM eag_eval_dataset"
     " WHERE master_id=:mid)"),
    ("eag_eval_dataset", "master_id=:mid"),
    # Loop Engineer：候选 → 优化任务
    ("eag_prompt_candidate",
     "optimization_id IN (SELECT optimization_id FROM eag_optimization_run"
     " WHERE master_id=:mid)"),
    ("eag_optimization_run", "master_id=:mid"),
    # 性能测试：样本 → 任务、用例 → 数据集
    ("eag_perf_sample",
     "perf_test_id IN (SELECT perf_test_id FROM eag_perf_test"
     " WHERE master_id=:mid)"),
    ("eag_perf_test", "master_id=:mid"),
    ("eag_perf_case",
     "perf_dataset_id IN (SELECT perf_dataset_id FROM eag_perf_dataset"
     " WHERE master_id=:mid)"),
    ("eag_perf_dataset", "master_id=:mid"),
    # standalone 导出登记
    ("eag_standalone_export", "master_id=:mid"),
    # KB 绑定（多态外键：target_type='MASTER' 时 target_id 才是 master_id）
    ("eag_kb_binding", "target_type='MASTER' AND target_id=:mid"),
    # 审计/运行记录（不可恢复，删除前须由调用方确认）
    ("eag_invocation_event", "master_id=:mid"),
    ("eag_run_record", "master_id=:mid"),
    # 总控自身：绑定 → 提示词版本 → 主体
    ("eag_master_binding", "master_id=:mid"),
    ("eag_master_prompt_version", "master_id=:mid"),
    ("eag_master_agent", "master_id=:mid"),
)


@router.delete("/agents/{master_id}")
def delete_master(
    master_id: str,
    confirm: str = "",
    user: str = Depends(require_agent_manage),
):
    """级联删除总控 Agent 及其全部从属数据。**不可恢复**。

    fail-closed 三道闸:
      1. ?confirm= 必须等于该总控的 slug（防止误删 / 手滑打错 id）；
      2. 有 PENDING/RUNNING 的性能测试时拒绝（perf-worker 独立进程仍在引用）；
      3. 有 RUNNING 的 Loop Engineer 优化任务时拒绝（后台任务仍在写候选）。

    删除成功后同步摘除 /a2a/{slug} 子应用（a2a_server.mounts），该地址立即 404。
    多副本部署时只对本副本生效，其余副本需 mounts.sync_from_db 对账或重启。
    """
    with closing(Database()) as db:
        master = master_service.get_master(db, master_id)
        if not master:
            raise NotFoundError("Agent Team 不存在")
        if confirm != master["slug"]:
            raise ValidationError(
                "删除未确认：请传 ?confirm=<slug>，当前 slug 为 "
                f"{master['slug']}",
            )
        busy_perf = db.query_one(
            "SELECT perf_test_id FROM eag_perf_test WHERE master_id=:mid"
            " AND status IN ('PENDING','RUNNING') LIMIT 1",
            {"mid": master_id},
        )
        if busy_perf:
            raise ConflictError(
                "该总控存在进行中的性能测试"
                f"（{busy_perf['perf_test_id']}），请先取消后再删除",
            )
        busy_opt = db.query_one(
            "SELECT optimization_id FROM eag_optimization_run"
            " WHERE master_id=:mid AND status='RUNNING' LIMIT 1",
            {"mid": master_id},
        )
        if busy_opt:
            raise ConflictError(
                "该总控存在进行中的优化任务"
                f"（{busy_opt['optimization_id']}），请等待结束后再删除",
            )

        deleted: dict[str, int] = {}
        for table, where in _MASTER_CASCADE:
            result = db.execute(
                f"DELETE FROM {table} WHERE {where}", {"mid": master_id},
            )
            if result.rowcount:
                deleted[table] = result.rowcount
        db.execute(
            "INSERT INTO eag_audit_event (audit_id, cap_user_name, action,"
            " resource_type, resource_id, detail, created_at)"
            " VALUES (:a, :u, 'master.delete', 'master_agent', :mid, :d, :ts)",
            {"a": new_id("aud_"), "u": user, "mid": master_id,
             "d": json.dumps(
                 {"slug": master["slug"], "cascade": deleted},
                 ensure_ascii=False),
             "ts": now()},
        )
        db.commit()
        # 运行期摘除 A2A 端点：删除后该地址立即 404，不再残留到进程重启
        from ..a2a_server import mounts

        unmounted = mounts.unmount_master(master["slug"], master_id)
        from .team_liveness import invalidate_team
        invalidate_team(master["slug"])
        return {
            "deleted": master_id,
            "slug": master["slug"],
            "cascade": deleted,
            "endpoint_unmounted": unmounted,
        }


class ReactConfigIn(BaseModel):
    maxIters: int = Field(ge=1, le=50)
    responseMode: Literal["SYNTHESIZE", "SINGLE_TOOL_RAW"] | None = None


@router.put("/agents/{master_id}/react-config")
def update_react_config(master_id: str, body: ReactConfigIn):
    with closing(Database()) as db:
        master = _require_master(db, master_id)
        config = json.loads(master.get("react_config_json") or "{}")
        config["max_iters"] = body.maxIters
        if body.responseMode is not None:
            config["response_mode"] = body.responseMode
        db.execute(
            "UPDATE eag_master_agent SET react_config_json=:cfg, updated_at=:ts"
            " WHERE master_id=:mid",
            {"cfg": json.dumps(config),
             "ts": now(), "mid": master_id},
        )
        db.commit()
        return _public_master(master_service.get_master(db, master_id))


class ModelConfigIn(BaseModel):
    baseUrl: str = ""
    model: str = ""
    apiKey: str | None = None  # None/空 = 保留已有 key（不覆盖）


@router.put("/agents/{master_id}/model-config", dependencies=[Depends(require_agent_manage)])
def update_model_config(master_id: str, body: ModelConfigIn):
    """设置总控 Agent 独立模型配置（master-model-config 专题）。

    baseUrl/model 必填；apiKey 留空 = 保留已有 key。设置后 run_master 优先用
    此配置，不再走平台全局模型（脱钩单一模型）。
    """
    with closing(Database()) as db:
        master = master_service.get_master(db, master_id)
        if not master:
            raise NotFoundError("Agent Team 不存在")
        cur = parse_model_config(master.get("model_config_json")) or {}
        api_key = (body.apiKey or "").strip()
        if not api_key:
            api_key = (cur.get("api_key") or "").strip()
        cfg = {
            "provider": (cur.get("provider") or "").strip() or "custom",
            "base_url": body.baseUrl.strip(),
            "model": body.model.strip(),
            "api_key": api_key,
        }
        db.execute(
            "UPDATE eag_master_agent SET model_config_json=:cfg, updated_at=:ts"
            " WHERE master_id=:mid",
            {"cfg": json.dumps(cfg, ensure_ascii=False),
             "ts": now(), "mid": master_id},
        )
        db.commit()
        return _public_master(master_service.get_master(db, master_id))


@router.delete("/agents/{master_id}/model-config", dependencies=[Depends(require_agent_manage)])
def clear_model_config(master_id: str):
    """清空总控 Agent 独立模型配置，回退平台全局模型。"""
    with closing(Database()) as db:
        if not master_service.get_master(db, master_id):
            raise NotFoundError("Agent Team 不存在")
        db.execute(
            "UPDATE eag_master_agent SET model_config_json=NULL, updated_at=:ts"
            " WHERE master_id=:mid",
            {"ts": now(), "mid": master_id},
        )
        db.commit()
        return _public_master(master_service.get_master(db, master_id))


class BindingsIn(BaseModel):
    bindings: list[dict]  # {serviceId, toolName, toolDescription, releaseId?, interactionMode?}


_INTERACTION_MODES = ("TOOL", "FLOW")


@router.put("/agents/{master_id}/bindings")
def put_bindings(master_id: str, body: BindingsIn):
    """批量勾选绑定: 全量替换该 master 的绑定（幂等）。

    releaseId 可选: 显式 pin 到某个精确 Release（P0-2）；
    缺省时按工作空间安装解析。"""
    with closing(Database()) as db:
        _require_master(db, master_id)
        existing = {
            r["service_id"]: r["binding_id"]
            for r in db.query_all(
                "SELECT binding_id, service_id FROM eag_master_binding"
                " WHERE master_id=:mid", {"mid": master_id},
            )
        }
        wanted = {}
        for b in body.bindings:
            wanted[b["serviceId"]] = b
        # 校验显式 pin 的 release 存在且属于该服务（fail-closed，写入前拦截）
        pinned_releases = {
            b.get("releaseId"): b["serviceId"]
            for b in wanted.values() if b.get("releaseId")
        }
        for release_id, service_id in pinned_releases.items():
            rel = db.query_one(
                "SELECT service_id FROM eag_agent_release WHERE release_id=:rid",
                {"rid": release_id},
            )
            if not rel or rel["service_id"] != service_id:
                raise ValidationError(
                    f"releaseId {release_id} 不存在或不属于该服务",
                )
        # 交互方式：FLOW（原话透传 + 原样返回 + 活跃流程直达）只支持 HIAGENT 来源
        for service_id, b in wanted.items():
            mode = b.get("interactionMode") or "TOOL"
            if mode not in _INTERACTION_MODES:
                raise ValidationError(f"interactionMode 仅支持 {'/'.join(_INTERACTION_MODES)}")
            if mode == "FLOW":
                svc = db.query_one(
                    "SELECT source_type FROM eag_a2a_service_registration"
                    " WHERE service_id=:sid", {"sid": service_id},
                )
                if not svc or svc["source_type"] != "HIAGENT":
                    raise ValidationError("流程型交互方式仅支持 HiAgent 来源的服务")
            b["interactionMode"] = mode
        # 删除取消勾选
        for service_id, binding_id in existing.items():
            if service_id not in wanted:
                db.execute(
                    "DELETE FROM eag_master_binding WHERE binding_id=:bid",
                    {"bid": binding_id},
                )
        # 新增/更新
        ts = now()
        for service_id, b in wanted.items():
            if service_id in existing:
                db.execute(
                    "UPDATE eag_master_binding SET tool_name=:tn,"
                    " tool_description=:td, release_id=:rel, interaction_mode=:mode"
                    " WHERE binding_id=:bid",
                    {"tn": b.get("toolName", service_id),
                     "td": b.get("toolDescription", ""),
                     "rel": b.get("releaseId"),
                     "mode": b["interactionMode"],
                     "bid": existing[service_id]},
                )
            else:
                db.execute(
                    """
                    INSERT INTO eag_master_binding
                        (binding_id, master_id, service_id, release_id,
                         tool_name, tool_description, interaction_mode,
                         enabled, created_at)
                    VALUES (:bid, :mid, :sid, :rel, :tn, :td, :mode, 1, :ts)
                    """,
                    {"bid": new_id("bnd_"), "mid": master_id, "sid": service_id,
                     "rel": b.get("releaseId"),
                     "tn": b.get("toolName", service_id),
                     "td": b.get("toolDescription", ""),
                     "mode": b["interactionMode"], "ts": ts},
                )
        db.commit()
        # Card 的 skills 由 build 时的绑定快照生成，改绑定后重建子应用才能让对外
        # Card 同步（运行时工具清单本身是动态的，run_master 每次从库里读）
        from ..a2a_server import mounts

        master = master_service.get_master(db, master_id)
        if master:
            mounts.remount_master(db, master)
        return {"bindings": db.query_all(
            "SELECT * FROM eag_master_binding WHERE master_id=:mid",
            {"mid": master_id},
        )}


# Export endpoints retain their original position after agent activity routes.
export_router = APIRouter(prefix="/api/v1")


# ===========================================================================
# 独立部署导出（standalone-deployment 01 §7，S-06/S-07；agent:manage）
# ===========================================================================
class StandaloneExportIn(BaseModel):
    imageTag: str = Field(
        default="eagent-standalone:latest", min_length=1, max_length=128,
    )


@export_router.post("/agents/{master_id}:export-standalone")
def export_standalone(
    master_id: str,
    body: StandaloneExportIn | None = None,
    user: str = Depends(require_agent_manage),
):
    """导出该总控 Agent 为独立部署包（tar.gz：card + KB 快照 + 模板）。

    一期边界 fail-closed：含 A2A 子服务绑定 → 409；KB 绑定未 READY → 409；
    card.json 剔除 api_key（部署方 env 注入）；响应头带三件套 digest
    （X-Deploy-Digest / X-Card-Digest / X-Snapshot-Hash / X-Image-Tag）。
    """
    from fastapi.responses import Response

    from .. import standalone_pkg

    image_tag = (body.imageTag if body else None) or "eagent-standalone:latest"
    with closing(Database()) as db:
        out = standalone_pkg.export_master_standalone(
            db, master_id=master_id, image_tag=image_tag, user=user,
        )
    exp = out["export"]
    return Response(
        content=out["package"],
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{out["filename"]}"',
            "X-Deploy-Digest": exp["deploy_digest"],
            "X-Card-Digest": exp["card_digest"],
            "X-Snapshot-Hash": exp["snapshot_hash"],
            "X-Image-Tag": exp["image_tag"],
            "X-Export-Id": exp["export_id"],
        },
    )


@export_router.get("/agents/{master_id}/standalone-exports")
def list_standalone_exports(
    master_id: str, user: str = Depends(require_agent_manage),
):
    """导出登记列表（S-07：产物与配置状态经 master/version/digest 关联可查）。"""
    from .. import standalone_pkg

    with closing(Database()) as db:
        return {"exports": [
            {
                "export_id": e["export_id"],
                "agent_version": e["agent_version"],
                "image_tag": e["image_tag"],
                "card_digest": e["card_digest"],
                "snapshot_hash": e["snapshot_hash"],
                "deploy_digest": e["deploy_digest"],
                "state": e["state"],
                "created_by": e["created_by"],
                "created_at": str(e["created_at"]),
            }
            for e in standalone_pkg.list_exports(db, master_id)
        ]}
