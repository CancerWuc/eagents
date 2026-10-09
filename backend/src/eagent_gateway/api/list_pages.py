"""SQL-backed pages for administration lists, shared by opt-in REST routes."""
import json

from ..errors import ValidationError
from ..persistence.db import now
from .pagination import Page, add_filter, query_page, search_filter


# Only server-owned SQL fragments are interpolated. Values are always bound.
_SIMPLE = {
    "releases": ("eag_agent_release", "release_id", "created_at DESC", "service_id", "service_id"),
    "datasets": ("eag_eval_dataset", "dataset_id", "created_at DESC", "master_id", "master_id"),
    "perf-datasets": ("eag_perf_dataset", "perf_dataset_id", "created_at DESC", "master_id", "masterId"),
    "perf-tests": ("eag_perf_test", "perf_test_id", "created_at DESC", "master_id", "masterId"),
    "perf-cases": ("eag_perf_case", "case_id", "seq_no", "perf_dataset_id", "dataset_id"),
    "perf-samples": ("eag_perf_sample", "sample_id", "seq_no", "perf_test_id", "test_id"),
    "alerts": ("eag_alert_event", "alert_id", "status='FIRED' DESC, created_at DESC", "service_id", "serviceId"),
    "faqs": ("eag_faq_candidate", "faq_id", "freq DESC, created_at DESC", None, None),
    "gaps": ("eag_knowledge_gap", "gap_id", "hit_count DESC, last_seen_at DESC", None, None),
    "kbs": ("eag_knowledge_base", "kb_id", "created_at DESC", None, None),
    "kb-sources": ("eag_kb_source", "source_id", "created_at DESC", "kb_id", "kb_id"),
    "kb-tasks": ("eag_kb_task", "task_id", "created_at DESC", "kb_id", "kb_id"),
    "kb-plans": ("eag_kb_ingest_plan", "plan_id", "created_at DESC", "kb_id", "kb_id"),
    "kb-reviews": ("eag_kb_review", "review_id", "created_at DESC", "kb_id", "kb_id"),
    "kb-revs": ("eag_kb_rev", "rev", "created_at DESC", "kb_id", "kb_id"),
    "gateway-services": ("eag_gateway_mcp_service", "id", "id", "profile_id", "profile_id"),
    "gateway-routes": ("eag_gateway_route", "id", "id", "profile_id", "profile_id"),
    "gateway-consumers": ("eag_gateway_consumer", "id", "id", "profile_id", "profile_id"),
}


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _serialize(db, name, row):
    if name == "releases":
        return row
    if name == "kbs":
        from ..kb.service import kb_out
        return kb_out(db, row)
    if name == "kb-tasks":
        from ..kb.serializers import _task_out
        return _task_out(row)
    if name == "kb-plans":
        from ..kb.serializers import _plan_out
        return _plan_out(row)
    if name == "kb-revs":
        return {
            "rev": row["rev"], "status": row["status"],
            "summary": _json(row.get("summary_json")), "report": _json(row.get("report_json")),
            "created_by": row["created_by"], "created_at": row["created_at"],
            "finished_at": row.get("finished_at"),
        }
    if name == "kb-reviews":
        row["payload"] = _json(row.pop("payload_json", None))
    if name == "perf-datasets":
        from ..perf.serializers import _dataset_out
        return _dataset_out(row)
    if name == "gateway-services":
        from ..gateway.mcp_service import _service_out
        return _service_out(row)
    if name == "gateway-routes":
        from ..gateway.route import _route_out
        return _route_out(db, row)
    if name == "gateway-consumers":
        from ..gateway.consumer import _consumer_out
        return _consumer_out(db, row)
    return row


def list_page(db, name: str, page: Page, **context) -> dict:
    if name == "my-services":
        sql = ("SELECT s.service_id AS serviceId, s.display_name AS displayName, s.slug, "
               "CASE WHEN EXISTS(SELECT 1 FROM eag_user_visibility uv WHERE uv.service_id=s.service_id "
               "AND uv.cap_user_name=:u) THEN 'direct' ELSE 'group' END AS source, "
               "CASE WHEN EXISTS(SELECT 1 FROM eag_user_visibility uv WHERE uv.service_id=s.service_id "
               "AND uv.cap_user_name=:u) THEN NULL ELSE (SELECT MIN(g.name) FROM eag_group_membership gm "
               "JOIN eag_user_group g ON g.group_id=gm.group_id JOIN eag_group_visibility gv ON gv.group_id=gm.group_id "
               "WHERE gm.cap_user_name=:u AND gv.service_id=s.service_id) END AS groupName "
               "FROM eag_a2a_service_registration s WHERE EXISTS(SELECT 1 FROM eag_user_visibility uv "
               "WHERE uv.cap_user_name=:u AND uv.service_id=s.service_id) OR EXISTS(SELECT 1 "
               "FROM eag_group_membership gm JOIN eag_group_visibility gv ON gv.group_id=gm.group_id "
               "WHERE gm.cap_user_name=:u AND gv.service_id=s.service_id)")
        return query_page(db, sql, "s.display_name, s.service_id", {"u": context["user_id"]}, page)
    if name == "services":
        return services_page(db, page, **context)
    if name == "groups":
        def group_out(row):
            row["members"] = [r["cap_user_name"] for r in db.query_all(
                "SELECT cap_user_name FROM eag_group_membership WHERE group_id=:gid ORDER BY cap_user_name",
                {"gid": row["group_id"]})]
            row["service_ids"] = [r["service_id"] for r in db.query_all(
                "SELECT v.service_id FROM eag_group_visibility v JOIN eag_a2a_service_registration s "
                "ON s.service_id=v.service_id WHERE v.group_id=:gid ORDER BY v.service_id",
                {"gid": row["group_id"]})]
            return row
        return query_page(db, "SELECT * FROM eag_user_group", "name, group_id", {}, page, group_out)
    if name == "directs":
        def direct_out(row):
            rows = db.query_all(
                "SELECT v.service_id, s.slug, s.display_name, v.created_at FROM eag_user_visibility v "
                "JOIN eag_a2a_service_registration s USING(service_id) "
                "WHERE v.cap_user_name=:u ORDER BY v.created_at, v.service_id",
                {"u": row["cap_user_name"]})
            return {**row, "services": rows}
        return query_page(db, "SELECT DISTINCT v.cap_user_name FROM eag_user_visibility v "
                          "JOIN eag_a2a_service_registration s ON s.service_id=v.service_id",
                          "cap_user_name", {}, page, direct_out)
    if name == "admin-users":
        from .serializers import _public_user
        from ..security.console_auth import user_roles
        def user_out(row):
            count = db.query_one(
                "SELECT COUNT(DISTINCT visible.service_id) AS n FROM ("
                "SELECT service_id FROM eag_user_visibility WHERE cap_user_name=:u UNION "
                "SELECT gv.service_id FROM eag_group_membership gm JOIN eag_group_visibility gv "
                "ON gv.group_id=gm.group_id WHERE gm.cap_user_name=:u) visible "
                "JOIN eag_a2a_service_registration s ON s.service_id=visible.service_id",
                {"u": row["user_id"]})["n"]
            return _public_user(row, user_roles(db, row["user_id"]), count)
        params = {}
        sql = search_filter("SELECT * FROM eag_console_user", ("user_id", "display_name", "department"), params, page)
        status = page.filters.get("status")
        if status:
            if status not in {"active", "disabled"}:
                raise ValidationError("status must be active or disabled")
            sql = add_filter(sql, "status=:user_status")
            params["user_status"] = status
        return query_page(db, sql, "created_at DESC, user_id", params, page, user_out)
    if name == "installations":
        sql = ("SELECT i.*, r.agent_version, r.state AS release_state, s.slug AS service_slug "
               "FROM eag_installation i JOIN eag_agent_release r ON r.release_id=i.release_id "
               "JOIN eag_a2a_service_registration s ON s.service_id=i.service_id WHERE i.workspace_id=:ws")
        return query_page(db, sql, "i.created_at DESC, i.installation_id", {"ws": context["workspace_id"]}, page)
    if name == "instances":
        from ..registry.model import Instance
        from .serializers import _instance_out
        return query_page(db, "SELECT * FROM eag_agent_instance WHERE service_id=:sid "
                          "AND (lease_expires_at IS NULL OR lease_expires_at>NOW(6))",
                          "created_at, instance_id", {"sid": context["service_id"]}, page,
                          lambda row: _instance_out(Instance.from_row(row)))
    if name == "cases":
        def case_out(row):
            row["annotations"] = db.query_all(
                "SELECT * FROM eag_annotation WHERE case_id=:cid ORDER BY created_at DESC, annotation_id",
                {"cid": row["case_id"]})
            return row
        sql = ("SELECT c.*, r.trace_id AS source_trace_id, r.status AS source_status, "
               "r.error_code AS source_error_code FROM eag_eval_case c LEFT JOIN eag_run_record r "
               "ON r.run_id=c.source_run_id WHERE c.dataset_id=:did")
        return query_page(db, sql, "c.created_at, c.case_id", {"did": context["dataset_id"]}, page, case_out)
    if name == "optimizations":
        sql = ("SELECT o.*, d.name AS dataset_name, (SELECT COUNT(*) FROM eag_prompt_candidate c "
               "WHERE c.optimization_id=o.optimization_id) AS candidate_count, "
               "(SELECT c.recommendation FROM eag_prompt_candidate c WHERE c.optimization_id=o.optimization_id "
               "ORDER BY c.created_at DESC, c.candidate_id DESC LIMIT 1) AS recommendation "
               "FROM eag_optimization_run o LEFT JOIN eag_eval_dataset d ON d.dataset_id=o.dataset_id")
        params = {}
        for key in ("master_id", "dataset_id"):
            if context.get(key):
                sql = add_filter(sql, f"o.{key}=:{key}"); params[key] = context[key]
        return query_page(db, sql, "o.created_at DESC, o.optimization_id", params, page)
    if name == "prompts":
        from ..runtime.prompt_middleware import get_published_prompt
        _, current = get_published_prompt(db, context["master_id"])
        columns = "prompt_version_id, version_no, status, source, created_by, created_at"
        if context.get("with_text"):
            columns += ", prompt_text"
        return query_page(db, f"SELECT {columns} FROM eag_master_prompt_version WHERE master_id=:mid",
                          "version_no DESC, prompt_version_id", {"mid": context["master_id"]}, page,
                          current_prompt_version_id=current)
    if name == "runs":
        from .run_filters import RUN_SOURCE_PREFIXES, _like_prefix
        sql = "SELECT * FROM eag_run_record WHERE master_id=:mid"
        params = {"mid": context["master_id"]}
        source = context.get("source", "all")
        if source in RUN_SOURCE_PREFIXES:
            sql += " AND context_id LIKE :cp ESCAPE '!'"; params["cp"] = _like_prefix(RUN_SOURCE_PREFIXES[source])
        elif source == "a2a":
            for i, prefix in enumerate(RUN_SOURCE_PREFIXES.values()):
                sql += f" AND context_id NOT LIKE :np{i} ESCAPE '!'"; params[f"np{i}"] = _like_prefix(prefix)
        elif source not in ("all", ""):
            raise ValidationError("Invalid run source")
        if context.get("only_problems"):
            sql += " AND status<>'SUCCEEDED'"
        return query_page(db, sql, "started_at DESC, run_id", params, page)
    if name == "memory":
        return query_page(db, "SELECT * FROM eag_memory_item WHERE scope=:sc AND owner_id=:own "
                          "AND (expires_at IS NULL OR expires_at>:ts)", "created_at DESC, memory_id",
                          {"sc": context["scope"], "own": context["ownerId"], "ts": now()}, page)
    if name == "external-tools":
        return query_page(db, "SELECT tool_id, master_id, kind, name, description, endpoint, method, "
                          "params_json, enabled, created_by, created_at FROM eag_external_tool",
                          "created_at DESC, tool_id", {}, page)
    if name == "kb-pages":
        sql = ("SELECT p.* FROM eag_kb_page p WHERE p.kb_id=:k AND NOT EXISTS "
               "(SELECT 1 FROM eag_kb_page newer WHERE newer.kb_id=p.kb_id AND newer.rel_path=p.rel_path "
               "AND (newer.updated_at>p.updated_at OR (newer.updated_at=p.updated_at AND newer.page_id>p.page_id)))")
        def page_out(row):
            return {"page_id": row["page_id"], "rel_path": row["rel_path"], "title": row["title"],
                    "content_hash": row["content_hash"][:12], "chunk_count": row["chunk_count"],
                    "embedded": bool(row["embedded"]), "updated_at": row["updated_at"]}
        return query_page(db, sql, "p.rel_path, p.page_id", {"k": context["kb_id"]}, page, page_out)
    table, primary, order, root_column, root_key = _SIMPLE[name]
    sql, params = f"SELECT * FROM {table}", {}
    if name == "perf-tests":
        sql = ("SELECT perf_test_id, master_id, perf_dataset_id, target_type, target_id, test_type, "
               "status, created_by, created_at, started_at, finished_at FROM eag_perf_test")
    if root_key and context.get(root_key) is not None:
        sql = add_filter(sql, f"{root_column}=:root"); params["root"] = context[root_key]
    if name in {"perf-datasets", "kb-sources"}:
        sql = add_filter(sql, "status='ACTIVE'" if name == "perf-datasets" else "status<>'DELETED'")
    status = context.get("status")
    if status:
        sql = add_filter(sql, "status=:status"); params["status"] = status
    if name == "perf-tests" and page.filters.get("testType"):
        test_type = page.filters["testType"]
        if test_type not in {"PRESSURE", "SIMULATION"}:
            raise ValidationError("Invalid testType")
        sql = add_filter(sql, "test_type=:test_type"); params["test_type"] = test_type
    if name == "perf-tests" and page.filters.get("active") == "true":
        sql = add_filter(sql, "status IN ('PENDING','RUNNING')")
    return query_page(db, sql, f"{order}, {primary}", params, page,
                      lambda row: _serialize(db, name, row))


def services_page(db, page, scope=None, cap_user_name=None):
    from .serializers import _service_out
    from ..permission.service import visible_service_ids
    base = ("SELECT s.*, COALESCE(h.health,'UNKNOWN') AS health, h.latency_ms AS health_latency_ms, "
            "h.detail AS health_detail, h.observed_at AS health_observed_at FROM eag_a2a_service_registration s "
            "LEFT JOIN eag_a2a_health_observation h ON h.observation_id=(SELECT h2.observation_id "
            "FROM eag_a2a_health_observation h2 WHERE h2.service_id=s.service_id "
            "ORDER BY h2.observed_at DESC,h2.observation_id DESC LIMIT 1)")
    params = {}
    if scope != "admin":
        ids = visible_service_ids(db, cap_user_name or "anonymous")
        base = add_filter(base, _in_clause("s.service_id", ids, params))
    stats = db.query_one(
        "SELECT COUNT(*) total, SUM(enabled=1) enabled, SUM(enabled=0) disabled, "
        "SUM(health='HEALTHY') healthy, SUM(health='DEGRADED') degraded, "
        "SUM(health='UNREACHABLE') unreachable, SUM(health='UNKNOWN') unknown, "
        "SUM(health IN ('DEGRADED','UNREACHABLE')) abnormal, "
        "SUM(compatibility='COMPATIBLE') compatible, SUM(compatibility<>'COMPATIBLE') compatRisk, "
        "ROUND(AVG(CASE WHEN health='HEALTHY' THEN health_latency_ms END)) avgLatency "
        f"FROM ({base}) service_stats", params)
    stats = {key: value if key == "avgLatency" else int(value or 0) for key, value in stats.items()}
    sql = search_filter(base, ("s.slug", "s.display_name", "s.tags", "s.agent_card_url"), params, page)
    sql = _enabled_filter(sql, "s.enabled", params, page)
    tile = page.filters.get("tile", "")
    conditions = {"healthy": "h.health='HEALTHY'", "abnormal": "h.health IN ('DEGRADED','UNREACHABLE')",
                  "incompat": "s.compatibility<>'COMPATIBLE'"}
    if tile in conditions:
        sql = add_filter(sql, conditions[tile])
    return query_page(db, sql, "s.created_at DESC, s.service_id", params, page, _service_out, stats=stats)


def _in_clause(column, values, params):
    if not values:
        return "1=0"
    keys = []
    for i, value in enumerate(values):
        key = f"page_id_{i}"; keys.append(f":{key}"); params[key] = value
    return f"{column} IN ({','.join(keys)})"


def _enabled_filter(sql, column, params, page):
    value = page.filters.get("enabled", "")
    if value in {"on", "off"}:
        sql = add_filter(sql, f"{column}=:page_enabled"); params["page_enabled"] = int(value == "on")
    return sql


async def teams_page(db, page):
    from .team_liveness import team_states
    from starlette.concurrency import run_in_threadpool
    skinny = await run_in_threadpool(db.query_all, "SELECT master_id, slug FROM eag_master_agent ORDER BY master_id")
    states = await team_states(skinny)
    return await run_in_threadpool(_teams_page_with_states, db, page, states)


def _teams_page_with_states(db, page, states):
    from .serializers import _public_master
    base = ("SELECT m.*, (SELECT COUNT(*) FROM eag_master_binding b WHERE b.master_id=m.master_id) binding_count, "
            "(SELECT COUNT(*) FROM eag_kb_binding k WHERE k.target_type='MASTER' AND k.target_id=m.master_id "
            ") kb_binding_count, (SELECT COUNT(*) FROM eag_master_prompt_version p "
            "WHERE p.master_id=m.master_id) prompt_count, (SELECT MAX(p.version_no) FROM eag_master_prompt_version p "
            "WHERE p.master_id=m.master_id) latest_prompt_version_no, "
            "(SELECT COUNT(*) FROM eag_run_record r WHERE r.master_id=m.master_id) runs_total, "
            "(SELECT COUNT(*) FROM eag_run_record r WHERE r.master_id=m.master_id AND r.status='SUCCEEDED') runs_ok, "
            "(SELECT COUNT(*) FROM eag_run_record r WHERE r.master_id=m.master_id AND r.status='FAILED') runs_failed "
            "FROM eag_master_agent m")
    stats = db.query_one("SELECT COUNT(*) total, SUM(enabled=1) enabled, SUM(enabled=0) disabled, "
                         "SUM(binding_count) bindings, SUM(runs_total) runs, SUM(runs_ok) ok, "
                         "SUM(runs_failed) failed, SUM(binding_count=0 AND kb_binding_count=0) unbound "
                         f"FROM ({base}) team_stats")
    stats = {key: int(value or 0) for key, value in stats.items()}
    stats.update(live=sum(v == "live" for v in states.values()),
                 pending=sum(v == "pending" for v in states.values()),
                 services=db.query_one("SELECT COUNT(*) n FROM eag_a2a_service_registration")["n"])
    params = {}
    sql = search_filter(base, ("m.slug", "m.display_name", "m.description"), params, page)
    sql = _enabled_filter(sql, "m.enabled", params, page)
    tile = page.filters.get("tile", "")
    if tile in {"live", "pending"}:
        sql = add_filter(sql, _in_clause("m.master_id", [key for key, value in states.items() if value == tile], params))
    if tile == "unbound":
        sql = add_filter(sql, "NOT EXISTS(SELECT 1 FROM eag_master_binding b WHERE b.master_id=m.master_id) "
                         "AND NOT EXISTS(SELECT 1 FROM eag_kb_binding k WHERE k.target_type='MASTER' "
                         "AND k.target_id=m.master_id)")
    def team_out(row):
        row = _public_master(row)
        row["liveness"] = states.get(row["master_id"], "pending")
        row["bindings"] = db.query_all("SELECT tool_name FROM eag_master_binding WHERE master_id=:mid ORDER BY binding_id", {"mid": row["master_id"]})
        row["kb_bindings"] = db.query_all("SELECT k.display_name FROM eag_kb_binding b JOIN eag_knowledge_base k ON k.kb_id=b.kb_id "
                                           "WHERE b.target_type='MASTER' AND b.target_id=:mid ORDER BY b.binding_id", {"mid": row["master_id"]})
        return row
    return query_page(db, sql, "m.created_at DESC, m.master_id", params, page, team_out, stats=stats)
