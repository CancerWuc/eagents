"""Agent prompt versions, run records and test-call endpoints."""
from contextlib import closing

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from ..errors import NotFoundError, ValidationError
from ..permission.scopes import require_agent_manage, require_console_user
from ..persistence.db import Database, new_id, now
from ..router import RouterContext
from ..runtime import master_service
from ..runtime.kb_tool import build_kb_tools
from ..runtime.prompt_middleware import get_published_prompt
from ..security.prompt_injection import wrap_inbound
from .pagination import Page, optional_page
from .list_pages import list_page
from .route_helpers import _user, _require_master
from .run_filters import _like_prefix, RUN_SOURCE_PREFIXES

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# 提示词版本: 新建 DRAFT / 发布 / 回滚（热更新, §5.3）
# ---------------------------------------------------------------------------
class PromptIn(BaseModel):
    promptText: str = Field(min_length=1)
    source: str = "MANUAL"  # MANUAL | OPTIMIZER


@router.get("/agents/{master_id}/prompts")
def list_prompts(master_id: str, with_text: bool = False, paging: Page | None = Depends(optional_page)):
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "prompts", paging, master_id=master_id, with_text=with_text)
        rows = db.query_all(
            """SELECT prompt_version_id, version_no, status, source,
                      created_by, created_at FROM eag_master_prompt_version
                 WHERE master_id=:mid ORDER BY version_no DESC""",
            {"mid": master_id},
        )
        if with_text:
            full = db.query_all(
                """SELECT prompt_version_id, prompt_text FROM
                      eag_master_prompt_version WHERE master_id=:mid""",
                {"mid": master_id},
            )
            texts = {r["prompt_version_id"]: r["prompt_text"] for r in full}
            for r in rows:
                r["prompt_text"] = texts.get(r["prompt_version_id"])
        _, current = get_published_prompt(db, master_id)
        return {"prompts": rows, "current_prompt_version_id": current}


@router.post("/agents/{master_id}/prompts")
def create_prompt(
    master_id: str, body: PromptIn, cap_user_name: str | None = Header(None),
):
    with closing(Database()) as db:
        _require_master(db, master_id)
        last = db.query_one(
            "SELECT MAX(version_no) AS v FROM eag_master_prompt_version"
            " WHERE master_id=:mid",
            {"mid": master_id},
        )
        version_no = (last["v"] or 0) + 1
        version_id = new_id("pv_")
        db.execute(
            """
            INSERT INTO eag_master_prompt_version
                (prompt_version_id, master_id, version_no, prompt_text,
                 status, created_by, source, created_at)
            VALUES (:pvid, :mid, :vno, :text, 'DRAFT', :u, :src, :ts)
            """,
            {"pvid": version_id, "mid": master_id, "vno": version_no,
             "text": body.promptText, "u": _user(cap_user_name),
             "src": body.source, "ts": now()},
        )
        db.commit()
        return {"prompt_version_id": version_id, "version_no": version_no,
                "status": "DRAFT"}


@router.post("/agents/{master_id}/prompts/{version_id}:publish", dependencies=[Depends(require_agent_manage)])
def publish_prompt(
    master_id: str, version_id: str, cap_user_name: str | None = Header(None),
):
    with closing(Database()) as db:
        version = db.query_one(
            "SELECT * FROM eag_master_prompt_version WHERE prompt_version_id=:pvid"
            " AND master_id=:mid",
            {"pvid": version_id, "mid": master_id},
        )
        if not version:
            raise NotFoundError("版本不存在")
        ts = now()
        db.execute(
            "UPDATE eag_master_prompt_version SET status='SUPERSEDED'"
            " WHERE master_id=:mid AND status='PUBLISHED'",
            {"mid": master_id},
        )
        db.execute(
            "UPDATE eag_master_prompt_version SET status='PUBLISHED'"
            " WHERE prompt_version_id=:pvid",
            {"pvid": version_id},
        )
        db.execute(
            "UPDATE eag_master_agent SET current_prompt_version_id=:pvid,"
            " updated_at=:ts WHERE master_id=:mid",
            {"pvid": version_id, "ts": ts, "mid": master_id},
        )
        db.execute(
            "INSERT INTO eag_audit_event (audit_id, cap_user_name, action,"
            " resource_type, resource_id, created_at) VALUES (:a, :u,"
            " 'prompt.publish', 'prompt_version', :pvid, :ts)",
            {"a": new_id("aud_"), "u": _user(cap_user_name),
             "pvid": version_id, "ts": ts},
        )
        # DEV-01: 发布即沉淀 agent 级记忆（版本/时间/操作者），后续 run
        # 的提示词注入段可召回
        from ..runtime import memory as runtime_memory

        runtime_memory.add_memory(
            db, scope="agent", owner_id=master_id,
            content=(f"提示词已发布新版本 {version.get('version_no')}"
                     f"（{version_id}），发布者 {_user(cap_user_name)}"),
        )
        db.commit()
        return {"published": version_id, "hot_update": True}


@router.post("/agents/{master_id}/prompts/{version_id}:rollback", dependencies=[Depends(require_agent_manage)])
def rollback_prompt(
    master_id: str, version_id: str, cap_user_name: str | None = Header(None),
):
    """回滚 = 把历史版本重新 PUBLISH（§7.4）。"""
    return publish_prompt(master_id, version_id, cap_user_name)


@router.get("/agents/{master_id}/runs")
def master_runs(
    master_id: str,
    limit: int = 50,
    offset: int = 0,
    source: str = "all",
    only_problems: bool = False,
    paging: Page | None = Depends(optional_page),
):
    """运行记录列表（管理台运行监控 / 试运行历史共用）。

    - source: all | a2a（业务方经 A2A 端点调入）| testrun（管理台试运行）
      | perf（性能测试）。a2a 是"排除掉另外两类"，无法用单个前缀表达，
      所以这里给的是语义选项而不是裸前缀。
    - only_problems: 只看非 SUCCEEDED 的记录（排障时最常用的一屏）。
    - offset: 配合 limit 翻页；按 started_at 倒序稳定分页。
    """
    with closing(Database()) as db:
        if isinstance(paging, Page):
            return list_page(db, "runs", paging, master_id=master_id, source=source, only_problems=only_problems)
        sql = "SELECT * FROM eag_run_record WHERE master_id=:mid"
        params: dict = {
            "mid": master_id,
            "lim": min(max(limit, 1), 200),
            "off": max(offset, 0),
        }
        if source in RUN_SOURCE_PREFIXES:
            sql += " AND context_id LIKE :cp ESCAPE '!'"
            params["cp"] = _like_prefix(RUN_SOURCE_PREFIXES[source])
        elif source == "a2a":
            for i, prefix in enumerate(RUN_SOURCE_PREFIXES.values()):
                sql += f" AND context_id NOT LIKE :np{i} ESCAPE '!'"
                params[f"np{i}"] = _like_prefix(prefix)
        elif source not in ("all", ""):
            raise ValidationError(
                f"source 必须是 all/a2a/{'/'.join(RUN_SOURCE_PREFIXES)} 之一",
            )
        if only_problems:
            sql += " AND status <> 'SUCCEEDED'"
        sql += " ORDER BY started_at DESC LIMIT :lim OFFSET :off"
        return db.query_all(sql, params)


@router.delete("/agents/{master_id}/runs", dependencies=[Depends(require_agent_manage)])
def delete_master_runs(
    master_id: str,
    context_id: str = "",
    context_prefix: str = "",
    user: str = Depends(require_agent_manage),
):
    """删除运行记录（管理台清理试运行历史用）。

    必须二选一给出范围，**不接受空选择器**——否则一次手滑就清掉该总控
    全部调用历史：
      - context_id=xxx      删掉这一段对话
      - context_prefix=xxx  删掉该前缀下的全部（如 `testrun-` 清空试运行历史）

    只删 eag_run_record。eag_invocation_event 是 append-only 审计账本
    （invocation-api 契约），不随管理台的"清理展示数据"动作一起抹掉；
    它由 EAGENT_RETENTION_DAYS 的保留期任务统一回收。
    """
    if bool(context_id) == bool(context_prefix):
        raise ValidationError(
            "必须且只能指定 context_id 或 context_prefix 之一",
        )
    with closing(Database()) as db:
        _require_master(db, master_id)
        if context_id:
            sql = ("DELETE FROM eag_run_record WHERE master_id=:mid"
                   " AND context_id=:cid")
            params = {"mid": master_id, "cid": context_id}
        else:
            sql = ("DELETE FROM eag_run_record WHERE master_id=:mid"
                   " AND context_id LIKE :cp ESCAPE '!'")
            params = {"mid": master_id, "cp": _like_prefix(context_prefix[:64])}
        result = db.execute(sql, params)
        db.commit()
        return {"deleted": getattr(result, "rowcount", None)}


# ===========================================================================
# 试运行（管理台单点调用）
# ===========================================================================
class TestRunIn(BaseModel):
    """管理台单次试运行入参。"""

    text: str = Field(min_length=1, max_length=8000)
    # 省略则每次新建，互不串上下文；显式传入可做多轮追问（含澄清握手回填）
    contextId: str | None = Field(default=None, max_length=64)
    # 以谁的身份跑。A2A 工具受可见性约束（组可见 ∪ 个人直授），换工号即可
    # 验证授权是否配对；省略则用调用者本人。
    asUser: str | None = Field(default=None, max_length=64)
    # 是否套用 A2A 入站「不可信远端」framing。默认 false：试运行是管理台内部
    # 调用，不是远端 peer。置 true 可复现真实 A2A 入站的模型行为。
    inboundFraming: bool = False


def _toolbox_preview(db: Database, master_id: str, as_user: str) -> dict:
    """试运行前的工具箱快照：这次调用模型手上到底有哪些工具。

    单看回答很难区分「模型没选这个工具」和「这个工具压根没进工具箱」，
    前者是提示词/描述问题，后者是权限或发布链问题。这里把两者分开。
    """
    bindings = master_service.resolve_visible_bindings(db, master_id, as_user)
    targets, unavailable = master_service.resolve_tool_targets(
        db, master_id, bindings,
    )
    return {
        "a2a": [
            {
                "toolName": t["tool_name"],
                "toolDescription": t["tool_description"],
                "serviceSlug": t["slug"],
                "releaseId": t["release"]["release_id"],
                "agentVersion": t["release"]["agent_version"],
            }
            for t in targets
        ],
        "kb": [
            {"name": t.name, "description": t.description}
            for t in build_kb_tools(db, master_id)
        ],
        # {工具名: 不可用原因}，如 RELEASE_NOT_INSTALLED / INSTALLATION_DISABLED
        "unavailable": unavailable,
    }


@router.post("/agents/{master_id}:test-run")
async def test_run_master(
    master_id: str,
    body: TestRunIn,
    session_user: tuple[str, bool] = Depends(require_console_user),
):
    """在管理台里对总控发起一次真实调用，返回回答 + 路由轨迹 + 工具箱快照。

    与走 /a2a/{slug}/ 的效果等价（同一个 master_service.run_master），
    区别只在少了 A2A 协议外壳，且默认不套入站 framing。同样落
    run_record 与 invocation_event，运行监控里能查到。
    """
    with closing(Database()) as db:
        master = master_service.get_master(db, master_id)
        if not master:
            raise NotFoundError("Agent Team 不存在")
        if not master["enabled"]:
            raise ValidationError("总控 Agent 已停用，无法试运行")
        operator, operator_is_admin = session_user
        # 管理员可指定任意身份（asUser）预览他人工具箱；普通用户强制本人，
        # 防止身份冒用
        if operator_is_admin and (body.asUser or "").strip():
            as_user = body.asUser.strip()
        else:
            as_user = operator
        context_id = body.contextId or f"testrun-{new_id('')}"
        toolbox = _toolbox_preview(db, master_id, as_user)
        user_text = wrap_inbound(as_user, body.text) if body.inboundFraming             else body.text
        result = await master_service.run_master(
            db,
            master_id=master_id,
            cap_user_name=as_user,
            context_id=context_id,
            user_text=user_text,
            trace_ctx=RouterContext.new_root(),
        )
        return {
            "runId": result["run_id"],
            "status": result["status"],
            "errorCode": result.get("error_code"),
            "errorDetail": result.get("error_detail"),
            "output": result["output"],
            "inputRequired": bool(result.get("input_required")),
            "routedServices": result.get("routed_services") or [],
            "toolCalls": result.get("tool_calls") or [],
            "elapsedMs": result.get("elapsed_ms"),
            # 阶段耗时拆解: 总时长慢在模型还是慢在下游，一眼看得出
            "timing": result.get("timing"),
            "promptVersionId": result.get("prompt_version_id"),
            "traceId": (result.get("trace") or {}).get("traceId"),
            "contextId": context_id,
            "asUser": as_user,
            "toolbox": toolbox,
        }
