# -*- coding: utf-8 -*-
"""eAgent A2A 网关入口。

一个进程承载:
  - /api/v1/*        管理台 REST
  - /a2a/{slug}/...  每个总控 Agent 的 A2A 端点（一等公民, 多实例）
  - /healthz         存活探针

启动时: 数据表存在性校验 + 默认总控 Agent 种子（无则创建）。
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import auth_routes, eval_routes, governance_routes, kb_routes, routes
from .api.error_handlers import register_error_handlers
from .a2a_server import mounts
from .config import settings
from .kb import runtime as kb_runtime
from .observability import TraceContextMiddleware
from .perf import routes as perf_routes
from .persistence.db import Database, new_id, now
from .persistence.migrations import _statements
from .security.auth import GatewayAuthMiddleware, assert_production_auth_configured

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("eagent")

DEFAULT_PROMPT = """你是 eAgent 总控 Agent，负责理解用户意图并调度背后的服务工具。

## 工作方式
1. 仔细理解用户请求，判断属于哪个服务的能力域
2. 选中合适的服务工具后调用它，把用户请求原文作为 message 传入
3. 根据工具返回结果，用简洁的中文回答用户
4. 若没有匹配的服务，如实说明并建议用户可用的能力
5. 通常一次调用即可完成；只有结果不足以回答时才调用第二个服务

## 注意
- 不要编造工具返回之外的事实
- 用户身份与服务可见性已由网关保证，工具清单即你的全部能力边界
"""


def seed(db: Database) -> None:
    """默认总控 Agent + 初始 DRAFT 提示词（幂等）。"""
    row = db.query_one(
        "SELECT master_id FROM eag_master_agent WHERE slug='general'",
    )
    if row:
        return
    ts = now()
    master_id = new_id("mst_")
    db.execute(
        """INSERT INTO eag_master_agent
             (master_id, slug, display_name, description, source_type,
              enabled, react_config_json, created_at, updated_at)
           VALUES (:mid, 'general', '通用总控 Agent',
                   '按用户意图调度已绑定 A2A 服务的 Agent Team',
                   'COMPOSITE', 1, :cfg, :ts, :ts)""",
        {"mid": master_id,
         "cfg": json.dumps({"max_iters": settings.default_max_iters}),
         "ts": ts},
    )
    version_id = new_id("pv_")
    db.execute(
        """INSERT INTO eag_master_prompt_version
             (prompt_version_id, master_id, version_no, prompt_text, status,
              created_by, source, created_at)
           VALUES (:pvid, :mid, 1, :text, 'PUBLISHED', 'system', 'MANUAL', :ts)""",
        {"pvid": version_id, "mid": master_id, "text": DEFAULT_PROMPT, "ts": ts},
    )
    db.execute(
        "UPDATE eag_master_agent SET current_prompt_version_id=:pvid"
        " WHERE master_id=:mid",
        {"pvid": version_id, "mid": master_id},
    )
    db.commit()
    logger.info("seeded default master agent: general (%s)", master_id)


def _bootstrap_statements(sql_text: str) -> list[str]:
    """schema.sql → 可执行语句列表（MySQL 方言）。

    行级去注释，复用迁移执行器的切分口径；跳过 CREATE DATABASE / USE——
    应用账号（intern）只有库内权限，库的存在由部署侧保证（compose
    initdb.d / RDS 建库）。
    （2026-08-28 修复：旧实现按块丢弃首行为 '--' 的整块，schema.sql 里
    7 张表的建表块因块首带注释被静默丢弃，含 eag_schema_migration 自身，
    空库自举后迁移执行器直接查询失败。）
    """
    stmts: list[str] = []
    for stmt in _statements(sql_text):
        head = stmt.split(None, 2)
        if head and head[0].upper() == "USE":
            continue
        if len(head) >= 2 and head[0].upper() == "CREATE" \
                and head[1].upper() == "DATABASE":
            continue
        stmts.append(stmt)
    return stmts


def bootstrap_schema(db: Database) -> None:
    """首启自举: 表不存在时执行镜像内 schema.sql（幂等，MySQL 方言）。"""
    try:
        db.query_one("SELECT 1 AS x FROM eag_schema_migration LIMIT 1")
        return
    except Exception:
        db.rollback()
    import os

    schema_path = os.environ.get(
        "EAGENT_SCHEMA_SQL", "/app/db/schema.sql",
    )
    if not os.path.isfile(schema_path):
        logger.warning("schema.sql 不存在(%s)，跳过自举（表需已建好）", schema_path)
        return
    with open(schema_path, encoding="utf-8") as f:
        for stmt in _bootstrap_statements(f.read()):
            db.execute(stmt)
    db.commit()
    logger.info("schema bootstrapped from %s", schema_path)


async def retention_loop() -> None:
    """每日清理超过保留期的运行记录与调用事件（0 = 禁用）。"""
    if settings.retention_days <= 0:
        return
    while True:
        try:
            db = Database()
            try:
                # 截止时刻 Python 侧计算
                from datetime import timedelta as _td

                _cut = now() - _td(days=settings.retention_days)
                db.execute(
                    "DELETE FROM eag_run_record WHERE started_at < :cut",
                    {"cut": _cut},
                )
                # P0-4: 调用事件与运行记录同保留期（from_nekiro/04 §8）
                db.execute(
                    "DELETE FROM eag_invocation_event WHERE started_at < :cut",
                    {"cut": _cut},
                )
                db.commit()
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logging.getLogger("eagent.retention").exception(
                "retention purge failed",
            )
        await asyncio.sleep(24 * 3600)


async def instance_lease_sweep_loop() -> None:
    """P1-2: 定期把租约过期的实例标记 UNAVAILABLE（0 = 禁用）。"""
    if settings.instance_sweep_interval_s <= 0:
        return
    from .registry import sweep_expired_instances

    while True:
        try:
            db = Database()
            try:
                swept = sweep_expired_instances(db)
                if swept:
                    logging.getLogger("eagent.registry").info(
                        "swept %d expired instance(s)", swept,
                    )
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logging.getLogger("eagent.registry").exception(
                "instance lease sweep failed",
            )
        await asyncio.sleep(settings.instance_sweep_interval_s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动初始化（自 on_event("startup") 迁移，消除 FastAPI 弃用告警）:
    自举 schema + 增量迁移 + 防环初始化 + 种子 + 挂载各总控 A2A 子应用。"""
    import contextlib

    async with contextlib.AsyncExitStack() as _stack:
        db = Database()
        try:
            bootstrap_schema(db)
            # 增量迁移（幂等; 升级零触碰）
            from .persistence.migrations import apply_pending_migrations

            applied = apply_pending_migrations(db)
            if applied:
                logger.info("applied schema migrations: %s", applied)
            # R1 跨 Agent 防环: 同 context 轮数上限（from_hermes_a2a/02）
            from .runtime import turn_tracker

            turn_tracker.init_turn_tracker(settings.max_pingpong_turns)
            seed(db)
            # P1 种子提示词（from_hermes_a2a/03）: 无 PUBLISHED 版本的 master
            # 种入结构化默认提示词（幂等；已有发布版本的 master 不受影响）
            from .runtime.seed_prompt import seed_missing_prompts

            seeded = seed_missing_prompts(db)
            if seeded:
                logger.info("seeded default prompts for masters: %s", seeded)
            # A2A 子应用挂载统一走 mounts 注册表（运行期可增删：管理台新建/删除
            # 总控后端点立即生效，不再需要重启进程）
            synced = mounts.sync_from_db(db)
            logger.info(
                "mounted A2A endpoints: %s", synced["mounted"] or "(none)",
            )
            # KB 知识库（from_llm_wiki）：滞留 RUNNING 任务清扫（幂等重跑）
            from .kb import tasks as kb_tasks

            kb_tasks.startup_sweep()
            # Loop Engineer（local-debug/07）：优化任务跑在进程内的
            # BackgroundTasks 里，上一个进程被 --reload / 滚动更新杀掉时
            # 库里会留下永远 RUNNING 的僵尸行，启动先扫一遍。
            from .api.eval_routes import sweep_stale_optimizations

            stale = sweep_stale_optimizations(db)
            if stale:
                logger.info("swept %d stale optimization run(s)", stale)
            # Higress 网关配置控制面（higress-mcp-gateway 03）：
            # fail-secure 启动校验 + 环境种子（幂等）
            from .gateway import seed as gateway_seed

            gateway_seed.assert_startup_config()
            gateway_seed.seed_gateway_profile(db)
        finally:
            db.close()
        # 常驻后台任务统一登记，停机时取消回收。不登记的话 uvicorn 停机会
        # 一直等这些永不结束的任务，进程耗尽 terminationGracePeriodSeconds
        # 后被 SIGKILL —— 每次升级都要白白停机一个宽限期。
        background: list[asyncio.Task] = []

        def _spawn(coro, name: str) -> None:  # noqa: ANN001
            background.append(asyncio.create_task(coro, name=name))

        _spawn(retention_loop(), "retention")
        if settings.perf_worker_embedded:
            # 单 Pod 部署形态：压测执行内嵌本进程（默认为独立 perf-worker
            # Deployment；EAGENT_PERF_WORKER_EMBEDDED=1 时并入 manager）
            from .perf.worker import worker_loop

            async def _perf_worker_guard() -> None:
                try:
                    await worker_loop()
                except Exception:  # noqa: BLE001
                    logging.getLogger("eagent.perf.embedded").exception(
                        "embedded perf-worker crashed")

            _spawn(_perf_worker_guard(), "perf-worker")
        _spawn(instance_lease_sweep_loop(), "instance-lease-sweep")
        # KB 后台循环：编译任务执行（B4）+ Rev GC（B5）
        # + OSS 增量同步/三方对账（08 §4，D-2026-08-16-02；间隔 0 = 禁用）
        from .kb.gc import kb_gc_loop
        from .kb.oss_sync import kb_oss_sync_loop
        from .kb.reconcile import kb_reconcile_loop
        from .kb.tasks import kb_task_loop

        _spawn(kb_task_loop(), "kb-task")
        _spawn(kb_gc_loop(), "kb-gc")
        _spawn(kb_oss_sync_loop(), "kb-oss-sync")
        _spawn(kb_reconcile_loop(), "kb-reconcile")
        # 网关密钥轮换宽限期到期下线（higress-mcp-gateway T13）
        _spawn(gateway_seed.grace_sweep_loop(), "gateway-grace-sweep")
        # DEV-12: 健康定时探测调度 + 告警推导（间隔 0 = 禁用）
        from . import monitoring as obs_monitor

        _spawn(obs_monitor.health_probe_loop(), "health-probe")
        # KB MCP（from_llm_wiki/09）：FastMCP 会话管理器需在宿主 lifespan
        # 内启动（Starlette Mount 不传播子应用 lifespan）
        if settings.kb_mcp_enabled:
            from .kb import mcp_server as kb_mcp_mod

            mcp = kb_mcp_mod.fastmcp_instance()
            if mcp is not None:
                await _stack.enter_async_context(mcp.session_manager.run())
                logger.info("KB MCP session manager started: /mcp/kb")
        yield
        # ---- 停机 ----------------------------------------------------
        # 1) 取消常驻后台任务并回收。这些循环都是 `while True: ... await
        #    sleep()`，不取消的话 uvicorn 会一直等，进程耗尽
        #    terminationGracePeriodSeconds 后被 SIGKILL。
        #    循环内一律 `except Exception`，CancelledError（BaseException
        #    子类）可正常传播出来，不会被吞。
        for task in background:
            task.cancel()
        if background:
            done = await asyncio.gather(*background, return_exceptions=True)
            for task, res in zip(background, done):
                if isinstance(res, Exception) and \
                        not isinstance(res, asyncio.CancelledError):
                    logger.warning(
                        "后台任务 %s 停机时异常退出: %r", task.get_name(), res,
                    )
            logger.info("已回收 %d 个后台任务", len(background))
        # 2) LLM 出站连接池
        from .runtime.model_factory import close_shared_http_clients

        await close_shared_http_clients()
        # 3) hiagent 出站连接池（hiagent-integration/01）
        from .hiagent.client import close_pooled

        await close_pooled()


def create_app() -> FastAPI:
    # fail-secure: strict 模式未配管理令牌直接拒绝启动
    assert_production_auth_configured()
    app = FastAPI(
        title="eAgent GitHub A2A Gateway", version="0.1.0", lifespan=lifespan,
    )
    # 运行期挂载注册表需要宿主应用引用。注意静态资源 catch-all 在本函数末尾才
    # 挂载，而 A2A 挂载发生在 lifespan（更晚），mounts 会把它插到 catch-all 之前。
    mounts.bind_app(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id", "X-Simulation-Id", "X-Simulation-Trace-Id", "X-Simulation-Audit-Warning"],
    )
    app.add_middleware(GatewayAuthMiddleware)
    # 后注册的在最外层: 鉴权失败的响应也带 trace 上下文
    app.add_middleware(TraceContextMiddleware)
    register_error_handlers(app)
    app.include_router(routes.router)
    # 管理面登录与会话（admin-auth 01：Pluto OAuth2 + admin 本地账号）
    app.include_router(auth_routes.router)
    # 用户管理与我的权限（permission-control v1）
    from .api import user_admin_routes

    app.include_router(user_admin_routes.router)
    from .api import simulator_routes

    app.include_router(simulator_routes.router)
    app.include_router(eval_routes.router)
    app.include_router(governance_routes.router)
    # KB Runtime 只读路由与写 API 物理分离（D6/L1，02 §6）
    app.include_router(kb_routes.router)
    app.include_router(kb_runtime.router)
    # KB MCP 对外暴露（from_llm_wiki/09：streamable-http /mcp/kb，
    # 默认不挂载零影响；⚠️ 本期无鉴权，仅限内网联调）
    if settings.kb_mcp_enabled:
        from .kb.mcp_server import mount_kb_mcp

        mount_kb_mcp(app)
    # 性能测试：API 只管创建/查询/取消，发压在独立 perf-worker 进程
    app.include_router(perf_routes.router)
    # Higress 网关配置控制面（higress-mcp-gateway，scope=gateway:manage）
    from .gateway import routes as gateway_routes

    app.include_router(gateway_routes.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    # 管理台前端静态资源（最后挂载，作为兜底路由）
    import os

    static_dir = os.environ.get("EAGENT_STATIC_DIR", "")
    if static_dir and os.path.isdir(static_dir):
        from fastapi.staticfiles import StaticFiles

        app.mount(
            "/", StaticFiles(directory=static_dir, html=True), name="static",
        )
        logger.info("serving admin UI from %s", static_dir)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "eagent_gateway.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
