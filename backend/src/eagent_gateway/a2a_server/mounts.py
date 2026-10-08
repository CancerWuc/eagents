# -*- coding: utf-8 -*-
"""总控 A2A 子应用的运行期挂载注册表。

背景：原先 /a2a/{slug} 只在 main.py 的 lifespan 里挂载一次，管理台新建/删除
总控后端点要等进程重启才生效（管理台可动态管理 vs 静态挂载的冲突）。本模块把
挂载做成运行期可增删的操作。

三个实现要点，改这里前请先读：

1. **路由顺序**：main.py 最后可能挂一个 catch-all 的静态资源应用（
   `app.mount("/", StaticFiles(...))`，Starlette 会把它的 path 归一化成 `""`）。
   Starlette 的 Router 是顺序匹配，新增的 `/a2a/*` 必须插到它**之前**，否则永
   不命中。见 `_insert_index`。

2. **并发**：同步路由处理函数跑在线程池里，而事件循环正在遍历
   `app.router.routes`。因此这里用「写时复制 + 整体替换」：构造新 list 后一次性
   赋值（赋值是原子的），读者只会看到旧的或新的完整 list，不会看到中间态。
   多个写者之间用 `threading.Lock` 串行化。

3. **TaskStore 保活**：`build_master_a2a_app` 每次会新建 `InMemoryTaskStore`。
   若 remount 时换掉它，进行中的 A2A 任务状态会丢。所以任务存储按 master_id
   缓存在这里，跨 remount 复用。

⚠️ 单进程内一致。多副本部署时 A 副本的新建对 B 副本不可见（项目当前没有 Redis
   或消息队列做广播）；此时需周期性调用 `sync_from_db()` 对账，或仍旧重启。
"""
from __future__ import annotations

import logging
import threading

from starlette.routing import Mount

from ..persistence.db import Database
from . import publisher

logger = logging.getLogger("eagent.a2a.mounts")

_app = None                       # 宿主 FastAPI 应用
_lock = threading.Lock()          # 仅保护写路径
_mounts: dict[str, Mount] = {}    # slug -> 已挂载的 Mount
_task_stores: dict[str, object] = {}   # master_id -> InMemoryTaskStore


def bind_app(app) -> None:
    """登记宿主应用。create_app 里调用一次。

    ⚠️ 挂载记录是相对某个 app 实例的，换 app 必须连带清空：否则新 app 会因为
    「注册表里已经有这个 slug」而跳过挂载，`mounted_slugs()` 显示已挂载但请求
    404。生产环境一进程一个 app 碰不到，但测试里每个用例新建 app 会踩到。
    """
    global _app
    with _lock:
        _app = app
        _mounts.clear()
        # 旧 app 的子应用已不可达，其任务存储一并丢弃
        _task_stores.clear()


def _prefix(slug: str) -> str:
    return f"/a2a/{slug}"


def _insert_index(routes: list) -> int:
    """catch-all 静态挂载（path == ""）之前的位置；没有则追加到末尾。"""
    for i, route in enumerate(routes):
        if isinstance(route, Mount) and route.path == "":
            return i
    return len(routes)


def _task_store_for(master_id: str):
    store = _task_stores.get(master_id)
    if store is None:
        from a2a.server.tasks import InMemoryTaskStore

        store = InMemoryTaskStore()
        _task_stores[master_id] = store
    return store


def _install_route(slug: str, sub) -> None:
    """把子应用写入路由表（写时复制 + 整体替换，见模块头第 2 点）。"""
    route = Mount(_prefix(slug), app=sub)
    with _lock:
        routes = list(_app.router.routes)
        old = _mounts.get(slug)
        if old is not None and old in routes:
            # 原地替换，保持顺序稳定
            routes[routes.index(old)] = route
        else:
            routes.insert(_insert_index(routes), route)
        _mounts[slug] = route
        _app.router.routes = routes   # 原子替换


def mount_master(db: Database, master: dict) -> bool:
    """挂载（或重新挂载）一个总控的 A2A 子应用。幂等。

    返回 True 表示路由表发生了变化。
    """
    if _app is None:
        logger.warning("宿主应用未登记，跳过挂载 %s", master.get("slug"))
        return False
    slug = master["slug"]
    sub = publisher.build_master_a2a_app(
        db, master, task_store=_task_store_for(master["master_id"]),
    )
    _install_route(slug, sub)
    logger.info("mounted A2A endpoint: /a2a/%s/ (COMPOSITE)", slug)
    return True


def mount_hiagent(db: Database, svc: dict) -> bool:
    """挂载一个 HIAGENT 来源注册服务的 A2A 子应用（hiagent-integration/01）。

    与总控共用 `/a2a/{slug}` 命名空间与挂载机制；slug 跨表唯一性由注册
    校验保证（hiagent/service.assert_slug_free + create_master）。"""
    if _app is None:
        logger.warning("宿主应用未登记，跳过挂载 %s", svc.get("slug"))
        return False
    from ..hiagent import executor as hia_executor
    from ..hiagent.service import get_hiagent_config
    from ..hiagent.card import parse_agent_config

    config = parse_agent_config((get_hiagent_config(db, svc["service_id"]) or {}).get("agent_config_json"))
    if "inputsSchema" not in config:
        logger.warning("HiAgent %s requires registration with the A2A 1.0 contract", svc["slug"])
        return False

    slug = svc["slug"]
    sub = hia_executor.build_hiagent_a2a_app(
        db, svc, task_store=_task_store_for(svc["service_id"]),
    )
    _install_route(slug, sub)
    logger.info("mounted A2A endpoint: /a2a/%s/ (HIAGENT)", slug)
    return True


def remount_hiagent(db: Database, svc: dict) -> bool:
    if slug_mounted(svc["slug"]):
        return mount_hiagent(db, svc)
    return False


def unmount_master(slug: str, master_id: str | None = None) -> bool:
    """摘除一个总控的 A2A 子应用。未挂载时返回 False。"""
    if _app is None:
        return False
    with _lock:
        route = _mounts.pop(slug, None)
        if route is None:
            return False
        routes = [r for r in list(_app.router.routes) if r is not route]
        _app.router.routes = routes
        if master_id:
            _task_stores.pop(master_id, None)
    logger.info("unmounted A2A endpoint: /a2a/%s/", slug)
    return True


def remount_master(db: Database, master: dict) -> bool:
    """绑定变更后刷新 Card。

    Card 的 skills 在 build 时就由当时的绑定快照生成（publisher.build_master_card），
    所以改绑定后必须重建子应用才能让对外 Card 同步。注意运行时的工具清单本身是
    动态的（run_master 每次从库里读绑定），此处只影响对外广告的 skills。
    """
    if slug_mounted(master["slug"]):
        return mount_master(db, master)
    return False


def slug_mounted(slug: str) -> bool:
    return slug in _mounts


def mounted_slugs() -> list[str]:
    return sorted(_mounts)


def sync_from_db(db: Database) -> dict[str, list[str]]:
    """与库对账：挂载缺失的、摘除已删的。启动期与多副本对账共用。

    统一注册表口径（hiagent-integration/01 §3.3）：总控（COMPOSITE）与
    HIAGENT 来源注册服务两张表合并对账。"""
    wanted: dict[str, tuple[str, dict]] = {}
    for m in db.query_all(
        "SELECT * FROM eag_master_agent WHERE enabled=1",
    ):
        wanted[m["slug"]] = ("master", m)
    for s in db.query_all(
        "SELECT * FROM eag_a2a_service_registration"
        " WHERE enabled=1 AND source_type='HIAGENT'",
    ):
        wanted[s["slug"]] = ("hiagent", s)
    added, removed = [], []
    for slug, (kind, row) in wanted.items():
        if slug not in _mounts:
            mounted = (
                mount_master(db, row) if kind == "master"
                else mount_hiagent(db, row)
            )
            if mounted:
                added.append(slug)
    for slug in [s for s in _mounts if s not in wanted]:
        if unmount_master(slug):
            removed.append(slug)
    return {"mounted": added, "unmounted": removed}
