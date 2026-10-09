# -*- coding: utf-8 -*-
"""评测/标注/优化 管理台路由（§9.1 评测组）。"""
from contextlib import closing
from datetime import datetime, timedelta
import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, Depends, Header
from pydantic import BaseModel

from ..config import settings
from ..errors import ConflictError, NotFoundError, ValidationError
from ..eval import service as eval_svc
from ..optimizer import engine as opt_engine
from ..permission.scopes import require_agent_manage
from ..persistence.db import Database, now
from .pagination import Page, optional_page
from .list_pages import list_page

router = APIRouter(prefix="/api/v1")


@router.get("/runs", dependencies=[Depends(require_agent_manage)])
def list_runs(
    master_id: str | None = None, status: str | None = None, limit: int = 50,
):
    with closing(Database()) as db:
        conds, params = [], {"lim": min(limit, 200)}
        if master_id:
            conds.append("master_id=:mid")
            params["mid"] = master_id
        if status:
            conds.append("status=:st")
            params["st"] = status
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        return db.query_all(
            f"SELECT * FROM eag_run_record {where}"
            f" ORDER BY started_at DESC LIMIT :lim",
            params,
        )


def _require_dataset(db: Database, dataset_id: str) -> dict:
    row = db.query_one(
        "SELECT * FROM eag_eval_dataset WHERE dataset_id=:did",
        {"did": dataset_id},
    )
    if not row:
        raise NotFoundError("评测集不存在")
    return row


class DatasetIn(BaseModel):
    name: str
    masterId: str
    runIds: list[str] = []


@router.post("/datasets", dependencies=[Depends(require_agent_manage)])
def create_dataset(body: DatasetIn):
    with closing(Database()) as db:
        return eval_svc.create_dataset_from_runs(
            db, name=body.name, master_id=body.masterId, run_ids=body.runIds,
        )


@router.get("/datasets", dependencies=[Depends(require_agent_manage)])
def list_datasets(master_id: str | None = None, paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "datasets", paging, master_id=master_id)
        return eval_svc.list_datasets(db, master_id)


class DatasetPatchIn(BaseModel):
    name: str


@router.patch("/datasets/{dataset_id}", dependencies=[Depends(require_agent_manage)])
def rename_dataset(dataset_id: str, body: DatasetPatchIn):
    with closing(Database()) as db:
        _require_dataset(db, dataset_id)
        return eval_svc.rename_dataset(db, dataset_id, body.name.strip())


@router.delete("/datasets/{dataset_id}", dependencies=[Depends(require_agent_manage)])
def delete_dataset(dataset_id: str):
    with closing(Database()) as db:
        _require_dataset(db, dataset_id)
        return eval_svc.delete_dataset(db, dataset_id)


class AppendCasesIn(BaseModel):
    runIds: list[str]


@router.post("/datasets/{dataset_id}/cases", dependencies=[Depends(require_agent_manage)])
def append_cases(dataset_id: str, body: AppendCasesIn):
    """把运行记录追加进已有评测集（运行监控/调用链上的「加入测试集」入口）。"""
    with closing(Database()) as db:
        _require_dataset(db, dataset_id)
        return eval_svc.append_cases_from_runs(
            db, dataset_id=dataset_id, run_ids=body.runIds,
        )


@router.get("/datasets/{dataset_id}/cases", dependencies=[Depends(require_agent_manage)])
def list_cases(dataset_id: str, paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        # 与 PATCH/DELETE/POST 同口径: 集不存在就 404。否则前端打开一个已被
        # 删掉的集会拿到空数组，显示成"没有用例"，分不清"空集"和"集没了"。
        _require_dataset(db, dataset_id)
        if isinstance(paging, Page):
            return list_page(db, "cases", paging, dataset_id=dataset_id)
        return eval_svc.list_cases(db, dataset_id)


class AnnotationIn(BaseModel):
    intentCorrect: bool | None = None
    subjectiveNote: str | None = None
    # 013: 标注者给出"正确答案该是哪个服务"，回写 case.expected_service
    correctedService: str | None = None
    # 014（DEV-07）: 标准答案文本，回写 case.expected_text，参与文本指标
    expectedText: str | None = None


@router.post("/cases/{case_id}/annotations", dependencies=[Depends(require_agent_manage)])
def annotate(
    case_id: str, body: AnnotationIn,
    cap_user_name: str | None = Header(None),
):
    with closing(Database()) as db:
        if not db.query_one(
            "SELECT 1 AS x FROM eag_eval_case WHERE case_id=:cid",
            {"cid": case_id},
        ):
            raise NotFoundError("case 不存在")
        return eval_svc.add_annotation(
            db, case_id=case_id, cap_user_name=cap_user_name or "anonymous",
            intent_correct=body.intentCorrect,
            subjective_note=body.subjectiveNote,
            corrected_service=body.correctedService,
            expected_text=body.expectedText,
        )


class OptimizationIn(BaseModel):
    masterId: str
    datasetId: str


def sweep_stale_optimizations(db: Database) -> int:
    """把超时仍 RUNNING 的优化任务标记为 FAILED，返回处理条数。

    任务跑在 FastAPI 的 BackgroundTasks 里，是进程内的：dev 的 `--reload`
    保存一次源码、生产的滚动更新，都会把在跑的任务直接杀掉，而 DB 里那行
    永远停在 RUNNING。没有这个兜底，前端只能一直转圈，且没有任何地方能
    看出任务其实已经死了。

    在列表/详情查询时惰性调用（进程启动时也扫一次），不额外起后台循环。
    """
    rows = db.query_all(
        "SELECT optimization_id FROM eag_optimization_run"
        " WHERE status='RUNNING' AND created_at < :cut",
        {"cut": datetime.now() - timedelta(seconds=settings.opt_run_timeout_s)},
    )
    for row in rows:
        db.execute(
            "UPDATE eag_optimization_run SET status='FAILED',"
            " report_json=:r, finished_at=NOW(6)"
            " WHERE optimization_id=:oid AND status='RUNNING'",
            {"r": '{"error": "任务超时未完成——进程可能在执行期间重启，'
                  '请重新发起"}',
             "oid": row["optimization_id"]},
        )
    if rows:
        db.commit()
    return len(rows)


@router.get("/optimizations", dependencies=[Depends(require_agent_manage)])
def list_optimizations(
    master_id: str | None = None, dataset_id: str | None = None,
    limit: int = 20,
    paging: Page | None = Depends(optional_page),
):
    """优化任务列表。

    没有这个接口的时候，optimization_id 只存在于前端那次调用的内存变量里，
    页面一刷新就再也找不回任务——任务还在跑，结果却永远看不到。
    """
    with closing(Database()) as db:
        sweep_stale_optimizations(db)
        if isinstance(paging, Page):
            return list_page(db, "optimizations", paging, master_id=master_id, dataset_id=dataset_id)
        conds, params = [], {"lim": min(limit, 100)}
        if master_id:
            conds.append("o.master_id=:mid")
            params["mid"] = master_id
        if dataset_id:
            conds.append("o.dataset_id=:did")
            params["did"] = dataset_id
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        rows = db.query_all(
            f"""SELECT o.*, d.name AS dataset_name,
                       (SELECT COUNT(*) FROM eag_prompt_candidate c
                         WHERE c.optimization_id=o.optimization_id)
                         AS candidate_count,
                       (SELECT c.recommendation FROM eag_prompt_candidate c
                         WHERE c.optimization_id=o.optimization_id
                         ORDER BY c.created_at DESC LIMIT 1)
                         AS recommendation
                  FROM eag_optimization_run o
                  LEFT JOIN eag_eval_dataset d
                         ON d.dataset_id=o.dataset_id
                  {where}
                 ORDER BY o.created_at DESC LIMIT :lim""",
            params,
        )
        return rows


@router.post("/optimizations", dependencies=[Depends(require_agent_manage)])
def create_optimization(
    body: OptimizationIn, background: BackgroundTasks,
    cap_user_name: str | None = Header(None),
):
    with closing(Database()) as db:
        from ..persistence.db import new_id, now

        # 前置校验: 空集直接发起会白跑一轮回放才发现无事可做；
        # 同一 master 并发两个优化则会互相覆盖 DRAFT 版本号。
        sweep_stale_optimizations(db)
        _require_dataset(db, body.datasetId)
        case_count = db.query_one(
            "SELECT COUNT(*) AS c FROM eag_eval_case WHERE dataset_id=:did",
            {"did": body.datasetId},
        )["c"]
        if not case_count:
            raise ValidationError("评测集里没有用例，先从运行记录生成或追加")
        running = db.query_one(
            "SELECT optimization_id FROM eag_optimization_run"
            " WHERE master_id=:mid AND status='RUNNING' LIMIT 1",
            {"mid": body.masterId},
        )
        if running:
            raise ConflictError(
                f"该总控已有优化任务在进行中（{running['optimization_id']}），"
                "等它结束再发起",
            )

        oid = new_id("opt_")
        from ..runtime.prompt_middleware import get_published_prompt

        _, base_vid = get_published_prompt(db, body.masterId)
        db.execute(
            """INSERT INTO eag_optimization_run
                 (optimization_id, master_id, dataset_id,
                  base_prompt_version_id, status, created_by, created_at)
               VALUES (:oid, :mid, :did, :bvid, 'RUNNING', :u, :ts)""",
            {"oid": oid, "mid": body.masterId, "did": body.datasetId,
             "bvid": base_vid or "", "u": cap_user_name or "anonymous",
             "ts": now()},
        )
        db.commit()
        background.add_task(_run_optimization_task, oid)
        return {"optimization_id": oid, "status": "RUNNING"}


def _run_optimization_task(oid: str) -> None:
    db = Database()
    try:
        asyncio.run(opt_engine.run_optimization(db, optimization_id=oid))
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        db.execute(
            "UPDATE eag_optimization_run SET status='FAILED',"
            " report_json=:r, finished_at=NOW(6) WHERE optimization_id=:oid",
            {"r": f'{{"error": "{str(exc)[:400]}"}}', "oid": oid},
        )
        db.commit()
    finally:
        db.close()


@router.get("/optimizations/{optimization_id}", dependencies=[Depends(require_agent_manage)])
def get_optimization(optimization_id: str):
    with closing(Database()) as db:
        sweep_stale_optimizations(db)
        opt = db.query_one(
            "SELECT * FROM eag_optimization_run WHERE optimization_id=:oid",
            {"oid": optimization_id},
        )
        if not opt:
            raise NotFoundError("资源不存在")
        candidates = db.query_all(
            "SELECT candidate_id, prompt_version_id, metric_json,"
            " react_config_json, prompt_text, recommendation, rationale,"
            " created_at"
            " FROM eag_prompt_candidate WHERE optimization_id=:oid",
            {"oid": optimization_id},
        )
        return {"optimization": opt, "candidates": candidates}


def _get_candidate(db, optimization_id: str, candidate_id: str) -> dict:
    """取属于指定优化运行的候选（L5），不存在则 404。"""
    cand = db.query_one(
        "SELECT * FROM eag_prompt_candidate WHERE candidate_id=:cid"
        " AND optimization_id=:oid",
        {"cid": candidate_id, "oid": optimization_id},
    )
    if not cand:
        raise NotFoundError("候选不存在")
    return cand


def _apply_candidate_react_config(
    db: Database, master_id: str, cand: dict,
) -> dict | None:
    """把候选搜出来的 ReAct 参数落到 master（与提示词同批生效）。

    只落认得的键（当前只有 max_iters），且值与现状相同时不写库——避免为了
    一个没变的值刷 updated_at。候选没带配置、解析失败或值不合法时返回 None，
    发布照常进行（退化成只发提示词，与改造前行为一致）。
    """
    raw = cand.get("react_config_json")
    if not raw:
        return None
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None
    try:
        max_iters = int(cfg.get("max_iters"))
    except (TypeError, ValueError):
        return None
    if not 1 <= max_iters <= 64:
        return None

    master = db.query_one(
        "SELECT react_config_json FROM eag_master_agent WHERE master_id=:mid",
        {"mid": master_id},
    )
    try:
        current = json.loads((master or {}).get("react_config_json") or "{}")
    except ValueError:
        current = {}
    if current.get("max_iters") == max_iters:
        return {"max_iters": max_iters, "changed": False}

    db.execute(
        "UPDATE eag_master_agent SET react_config_json=:cfg, updated_at=:ts"
        " WHERE master_id=:mid",
        {"cfg": json.dumps({**current, "max_iters": max_iters}),
         "ts": now(), "mid": master_id},
    )
    db.commit()
    return {"max_iters": max_iters, "changed": True,
            "previous": current.get("max_iters")}


@router.post(
    "/optimizations/{optimization_id}/candidates/{candidate_id}/approve",
dependencies=[Depends(require_agent_manage)],
)
def approve_candidate(
    optimization_id: str, candidate_id: str,
    cap_user_name: str | None = Header(None),
):
    """L5（from_hermes_a2a/05）: 人工复核通过 → 发布候选 DRAFT 提示词。

    复用既有 publish 语义（SUPERSEDED 旧版本 + PUBLISH 新版本 + 指针 +
    审计），发布后下一次 run 生效；绝不自动发布（D7），本接口即人工确认。

    候选是**「提示词 + ReAct 参数」两件套**（L3）: 优化在 max_iters 小网格上
    搜过最优值，指标是按那个轮数测出来的。只发布提示词、不落 react_config
    的话，线上跑的是"新提示词 + 旧轮数"，与评测组合不一致，报告里的准确率
    在线上不可复现。因此这里连同 react_config 一起落。
    """
    with closing(Database()) as db:
        cand = _get_candidate(db, optimization_id, candidate_id)
        if cand["recommendation"] != "PROPOSE":
            raise ConflictError("仅 PROPOSE 候选可发布")
        pvid = cand["prompt_version_id"]
        if not pvid:
            raise ConflictError("该候选未生成 DRAFT 提示词版本")
        version = db.query_one(
            "SELECT master_id, status FROM eag_master_prompt_version"
            " WHERE prompt_version_id=:pvid",
            {"pvid": pvid},
        )
        if not version:
            raise NotFoundError("提示词版本不存在")
        if version["status"] != "DRAFT":
            raise ConflictError(
                f"版本状态为 {version['status']}，仅 DRAFT 可经审核发布",
            )
        master_id = version["master_id"]
        react_applied = _apply_candidate_react_config(db, master_id, cand)
    # 复用 routes.publish_prompt（自带校验/SUPERSEDED/指针/审计/提交）
    from .routes import publish_prompt

    result = publish_prompt(master_id, pvid, cap_user_name)
    return {
        "approved": True,
        "candidate_id": candidate_id,
        "published_prompt_version_id": result["published"],
        "hot_update": result["hot_update"],
        # 本次发布顺带落的 ReAct 参数（无可用值时为 None，表示未改动）
        "react_config": react_applied,
    }


@router.post(
    "/optimizations/{optimization_id}/candidates/{candidate_id}/reject",
dependencies=[Depends(require_agent_manage)],
)
def reject_candidate(
    optimization_id: str, candidate_id: str,
    cap_user_name: str | None = Header(None),
):
    """L5: 人工驳回候选（幂等）——标记 REJECTED，不触碰提示词版本。"""
    with closing(Database()) as db:
        cand = _get_candidate(db, optimization_id, candidate_id)
        if cand["recommendation"] == "REJECTED":
            return {"rejected": True, "candidate_id": candidate_id}
        from ..persistence.db import new_id, now

        db.execute(
            "UPDATE eag_prompt_candidate SET recommendation='REJECTED'"
            " WHERE candidate_id=:cid",
            {"cid": candidate_id},
        )
        db.execute(
            "INSERT INTO eag_audit_event (audit_id, cap_user_name, action,"
            " resource_type, resource_id, created_at) VALUES (:a, :u,"
            " 'candidate.reject', 'prompt_candidate', :cid, :ts)",
            {"a": new_id("aud_"), "u": cap_user_name or "anonymous",
             "cid": candidate_id, "ts": now()},
        )
        db.commit()
        return {"rejected": True, "candidate_id": candidate_id}
