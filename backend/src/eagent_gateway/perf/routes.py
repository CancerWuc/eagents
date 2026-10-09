# -*- coding: utf-8 -*-
"""性能测试 REST API（T05 数据集 / T06 测试任务，01 §6）。

- 管理面鉴权沿用 GatewayAuthMiddleware（strict 模式 X-Admin-Token）；
  身份头 cap-user-name 作为 created_by。
- 只负责创建/查询/取消；真正发压调度在独立 perf-worker 进程。
- 全局锁：创建时检查 PENDING/RUNNING → 409（01 §6.3）。
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, UploadFile, Query
from pydantic import BaseModel, Field

from .. import errors
from ..permission.scopes import require_agent_manage
from ..persistence.db import Database
from ..api.pagination import Page, optional_page, reject_mixed_pagination
from ..api.list_pages import list_page
from . import dao, dataset_service
from .runner import next_fire_at, resolve_target_endpoint
from .serializers import _str_ts, _dataset_out

router = APIRouter(prefix="/api/v1/perf")


def _user(cap_user_name: str | None) -> str:
    return cap_user_name or "anonymous"


# ===========================================================================
# 数据集管理（T05）
# ===========================================================================
class DatasetIn(BaseModel):
    masterId: str = Field(min_length=4, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    datasetType: str


@router.post("/datasets", status_code=201, dependencies=[Depends(require_agent_manage)])
def create_dataset(body: DatasetIn, cap_user_name: str | None = Header(None)):
    with closing(Database()) as db:
        ds = dataset_service.create_dataset(
            db, master_id=body.masterId, name=body.name,
            dataset_type=body.datasetType, created_by=_user(cap_user_name),
        )
        return _dataset_out(ds)


@router.post("/datasets/{dataset_id}:upload", dependencies=[Depends(require_agent_manage)])
async def upload_dataset(
    dataset_id: str, file: UploadFile,
    cap_user_name: str | None = Header(None),
):
    with closing(Database()) as db:
        data = await file.read()
        out = dataset_service.upload_cases(
            db, dataset_id=dataset_id,
            file_name=file.filename or "upload.bin", data=data,
        )
        out["dataset"] = _dataset_out(dao.get_dataset(db, dataset_id))
        return out


@router.get("/datasets", dependencies=[Depends(require_agent_manage)])
def list_datasets(masterId: str | None = None, paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "perf-datasets", paging, masterId=masterId)
        return {"datasets": [
            _dataset_out(ds) for ds in dao.list_datasets(db, masterId)
        ]}


@router.get("/datasets/{dataset_id}", dependencies=[Depends(require_agent_manage), Depends(reject_mixed_pagination)])
def get_dataset(dataset_id: str, page: Annotated[int, Query(ge=1)] = 1, pageSize: Annotated[int, Query(ge=1, le=200)] = 50):
    with closing(Database()) as db:
        ds = dao.get_dataset(db, dataset_id)
        if not ds or ds["status"] != "ACTIVE":
            raise errors.NotFoundError("数据集不存在")
        out = list_page(db, "perf-cases", Page(page, pageSize), dataset_id=dataset_id)
        return {**out, "dataset": _dataset_out(ds), "cases": out["items"]}


@router.delete("/datasets/{dataset_id}", dependencies=[Depends(require_agent_manage)])
def delete_dataset(dataset_id: str):
    with closing(Database()) as db:
        return dataset_service.delete_dataset(db, dataset_id)


# ===========================================================================
# 测试任务（T06）
# ===========================================================================
class PressureIn(BaseModel):
    concurrency: int = Field(default=5, ge=1, le=500)
    totalRequests: int = Field(default=0, ge=0, le=1_000_000)
    durationSec: float = Field(default=0, ge=0, le=24 * 3600)
    rampUpSec: float = Field(default=0, ge=0, le=3600)
    timeoutMs: int = Field(default=60000, ge=1000, le=600_000)
    capUser: str = Field(default="perf-tester", min_length=1, max_length=64)
    caseOrder: str = "ROUND_ROBIN"
    headers: dict[str, str] = Field(default_factory=dict)


class SimulationIn(BaseModel):
    timeoutMs: int = Field(default=60000, ge=1000, le=600_000)
    capUser: str = Field(default="perf-tester", min_length=1, max_length=64)
    maxConcurrency: int = Field(default=0, ge=0, le=500)
    headers: dict[str, str] = Field(default_factory=dict)


class TestIn(BaseModel):
    masterId: str = Field(min_length=4, max_length=32)
    datasetId: str = Field(min_length=4, max_length=32)
    targetType: str
    targetId: str = Field(min_length=4, max_length=32)
    testType: str
    pressure: PressureIn | None = None
    simulation: SimulationIn | None = None


@router.post("/tests", status_code=201, dependencies=[Depends(require_agent_manage)])
def create_test(body: TestIn, cap_user_name: str | None = Header(None)):
    with closing(Database()) as db:
        if body.testType not in dao.TEST_TYPES:
            raise errors.ValidationError(
                f"testType 必须为 {list(dao.TEST_TYPES)}",
            )
        if body.targetType not in dao.TARGET_TYPES:
            raise errors.ValidationError(
                f"targetType 必须为 {list(dao.TARGET_TYPES)}",
            )
        master = db.query_one(
            "SELECT master_id FROM eag_master_agent WHERE master_id=:mid",
            {"mid": body.masterId},
        )
        if not master:
            raise errors.NotFoundError("Agent Team 不存在")
        ds = dao.get_dataset(db, body.datasetId)
        if not ds or ds["status"] != "ACTIVE":
            raise errors.NotFoundError("数据集不存在")
        if ds["master_id"] != body.masterId:
            raise errors.ValidationError("数据集不属于该总控 Agent")
        if ds["dataset_type"] != body.testType:
            raise errors.ValidationError(
                f"数据集类型为 {ds['dataset_type']}，与测试类型"
                f" {body.testType} 不匹配",
            )
        if ds["case_count"] == 0:
            raise errors.ValidationError("数据集未上传用例")
        if body.targetType == "MASTER" and body.targetId != body.masterId:
            raise errors.ValidationError(
                "MASTER 目标必须与数据集所属总控一致（targetId == masterId）",
            )
        cfg_block: dict
        if body.testType == "PRESSURE":
            if body.pressure is None:
                raise errors.ValidationError("压力测试需提供 pressure 配置")
            if body.pressure.totalRequests <= 0 and body.pressure.durationSec <= 0:
                raise errors.ValidationError(
                    "totalRequests 与 durationSec 至少配置一项",
                )
            cfg_block = {"pressure": body.pressure.model_dump()}
            planned_total = body.pressure.totalRequests or None
        else:
            if body.simulation is None:
                raise errors.ValidationError("仿真测试需提供 simulation 配置")
            cfg_block = {"simulation": body.simulation.model_dump()}
            planned_total = ds["case_count"]
        target = resolve_target_endpoint(db, body.targetType, body.targetId)
        # 全局锁：同一时间全局只允许一个测试（01 §6.3）
        if dao.has_active_test(db):
            raise errors.ConflictError(
                "已有测试在运行或排队（全局同一时间仅允许一个），"
                "请等待完成或取消后重试",
            )
        config = {
            **cfg_block,
            "endpoint": target["endpoint"],
            "slug": target["slug"],
            "targetName": target.get("name"),
            "plannedTotal": planned_total,
        }
        test = dao.create_test(
            db, master_id=body.masterId, dataset_id=body.datasetId,
            target_type=body.targetType, target_id=body.targetId,
            test_type=body.testType, config=config,
            created_by=_user(cap_user_name),
        )
        return _test_out(db, test)


def _test_out(db: Database, test: dict) -> dict:
    return {
        "perf_test_id": test["perf_test_id"],
        "master_id": test["master_id"],
        "perf_dataset_id": test["perf_dataset_id"],
        "target_type": test["target_type"],
        "target_id": test["target_id"],
        "test_type": test["test_type"],
        "status": test["status"],
        "config": test.get("config") or {},
        "report": test.get("report"),
        "created_by": test["created_by"],
        "created_at": _str_ts(test["created_at"]),
        "started_at": _str_ts(test["started_at"]),
        "finished_at": _str_ts(test["finished_at"]),
    }


@router.get("/tests", dependencies=[Depends(require_agent_manage)])
def list_tests(masterId: str | None = None, limit: int = 50, paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "perf-tests", paging, masterId=masterId)
        rows = []
        for t in dao.list_tests(db, masterId, limit=limit):
            t["created_at"] = _str_ts(t["created_at"])
            t["started_at"] = _str_ts(t["started_at"])
            t["finished_at"] = _str_ts(t["finished_at"])
            rows.append(t)
        return {"tests": rows}


def _next_fire_at(db: Database, test: dict) -> str | None:
    """仿真测试：未完成用例的下一次触发时刻（今天/明天 HH:mm:ss）。"""
    if test["test_type"] != "SIMULATION" or test["status"] != "RUNNING":
        return None
    done = dao.done_seq_nos(db, test["perf_test_id"])
    cases = dao.all_cases(db, test["perf_dataset_id"])
    pending = [c for c in cases if c["seq_no"] not in done]
    if not pending:
        return None
    now_dt = datetime.now()
    nxt = min(next_fire_at(now_dt, c["time_sec"]) for c in pending)
    return str(nxt)


@router.get("/tests/{test_id}", dependencies=[Depends(require_agent_manage)])
def get_test_detail(test_id: str):
    with closing(Database()) as db:
        test = dao.get_test(db, test_id)
        if not test:
            raise errors.NotFoundError("测试不存在")
        out = _test_out(db, test)
        progress = dao.sample_progress(db, test_id)
        progress["plannedTotal"] = (
            (test.get("config") or {}).get("plannedTotal")
        )
        out["progress"] = progress
        out["nextFireAt"] = _next_fire_at(db, test)
        out["cancelRequested"] = bool(test.get("cancel_flag"))
        return out


@router.get("/tests/{test_id}/samples", dependencies=[Depends(require_agent_manage), Depends(reject_mixed_pagination)])
def list_samples(
    test_id: str, status: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1, pageSize: Annotated[int, Query(ge=1, le=200)] = 50,
):
    with closing(Database()) as db:
        if not dao.get_test(db, test_id):
            raise errors.NotFoundError("测试不存在")
        out = list_page(db, "perf-samples", Page(page, pageSize), test_id=test_id, status=status)
        rows = out["items"]
        for r in rows:
            r["sent_at"] = _str_ts(r["sent_at"])
            r["finished_at"] = _str_ts(r["finished_at"])
        return {**out, "samples": rows}


@router.post("/tests/{test_id}:cancel", dependencies=[Depends(require_agent_manage)])
def cancel_test(test_id: str):
    """PENDING 直接终态 CANCELED；RUNNING 置 cancel_flag 由 worker 检查点处理。"""
    with closing(Database()) as db:
        test = dao.get_test(db, test_id)
        if not test:
            raise errors.NotFoundError("测试不存在")
        if test["status"] == "PENDING":
            dao.set_finished(db, test_id, status="CANCELED", report=None)
            return {"perf_test_id": test_id, "status": "CANCELED"}
        if test["status"] == "RUNNING":
            dao.request_cancel(db, test_id)
            return {
                "perf_test_id": test_id, "status": "RUNNING",
                "cancelRequested": True,
            }
        raise errors.ConflictError(
            f"测试已结束（{test['status']}），无需取消",
        )


@router.post("/tests/{test_id}:analyze", dependencies=[Depends(require_agent_manage)])
async def analyze_test(test_id: str):
    """DEV-09: LLM 生成测试分析报告（同步触发，结果入库可重复生成）。"""
    from . import analysis as perf_analysis
    from . import dao as perf_dao

    with closing(Database()) as db:
        test = perf_dao.get_test(db, test_id)
        if not test:
            raise errors.NotFoundError("测试不存在")
        try:
            result = await perf_analysis.generate_analysis(db, test_id)
        except ValueError as exc:
            raise errors.ConflictError(str(exc)) from exc
        if result is None:
            raise errors.ConflictError(
                "LLM 未配置（EAGENT_LLM_API_KEY 为空），无法生成分析报告",
            )
        return result


@router.get("/tests/{test_id}/analysis", dependencies=[Depends(require_agent_manage)])
def get_analysis(test_id: str, download: bool = False):
    """获取已生成的分析报告；download=true 以 Markdown 附件下载。"""
    with closing(Database()) as db:
        test = dao.get_test(db, test_id)
        if not test:
            raise errors.NotFoundError("测试不存在")
        if not test.get("analysis_md"):
            raise errors.NotFoundError("该测试还没有分析报告，先 POST :analyze")
        if download:
            from fastapi.responses import Response

            return Response(
                content=test["analysis_md"], media_type="text/markdown",
                headers={"Content-Disposition":
                         f'attachment; filename="{test_id}-analysis.md"'},
            )
        return {
            "perf_test_id": test_id,
            "analysis_md": test["analysis_md"],
            "analysis_model": test.get("analysis_model"),
            "analysis_at": _str_ts(test.get("analysis_at")),
        }
