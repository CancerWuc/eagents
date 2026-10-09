"""Focused behavior checks for the administration code refactor; no live DB required.

Run with PYTHONPATH=backend/src python -m unittest discover -s backend/tests.
Endpoint lookup deliberately follows the router, so moving a handler is harmless.
"""
import asyncio
from contextlib import ExitStack
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from starlette.requests import Request

from eagent_gateway.api import routes, user_admin_routes
from eagent_gateway.api.pagination import Page, optional_page
from eagent_gateway.errors import ForbiddenError, NotFoundError, UnauthenticatedError, ValidationError
from eagent_gateway.permission.scopes import require_admin_dep


def endpoint(name):
    def handlers(router):
        for route in router.routes:
            if hasattr(route, "original_router"):
                yield from handlers(route.original_router)
            elif hasattr(route, "endpoint"):
                yield route.endpoint
    return next(function for function in handlers(routes.router)
                if function.__name__ == name)


class AdminBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.events = []
        self.db = Mock()
        self.db.execute.side_effect = lambda sql, params=None: self.events.append(
            ("execute", " ".join(sql.split()), params))
        self.db.commit.side_effect = lambda: self.events.append(("commit",))
        self.db.close.side_effect = lambda: self.events.append(("close",))

    def patch(self, target, **kwargs):
        return self.stack.enter_context(patch(target, **kwargs))

    def handler(self, name):
        function = endpoint(name)
        module = sys.modules[function.__module__]
        self.stack.enter_context(patch.object(module, "Database", return_value=self.db))
        return function, module

    def assert_closed(self, committed=False):
        self.db.close.assert_called_once_with()
        self.db.rollback.assert_not_called()
        if committed:
            self.db.commit.assert_called_once_with()
        else:
            self.db.commit.assert_not_called()
        self.assertEqual(self.events[-1], ("close",))

    def test_legacy_service_list_hides_credential_without_mutating_source(self):
        function, module = self.handler("list_services")
        row = {"service_id": "service-1", "auth_secret_ref": "test-ciphertext"}
        listing = self.stack.enter_context(patch.object(module.catalog, "list_services", return_value=[row]))
        self.assertEqual(function("admin", None, None), [
            {"service_id": "service-1", "outbound_credential_configured": True}])
        listing.assert_called_once_with(self.db)
        self.assertEqual(row["auth_secret_ref"], "test-ciphertext")
        self.assert_closed()

    def test_service_list_preserves_empty_visibility_filter(self):
        function, module = self.handler("list_services")
        visible = self.patch("eagent_gateway.permission.service.visible_service_ids", return_value=[])
        listing = self.stack.enter_context(patch.object(module.catalog, "list_services", return_value=[]))
        self.assertEqual(function(None, "employee", None), [])
        visible.assert_called_once_with(self.db, "employee")
        listing.assert_called_once_with(self.db, [])
        self.assert_closed()

    def test_paged_service_list_retains_envelope_and_hides_credentials(self):
        function, _ = self.handler("list_services")
        self.db.query_one.side_effect = [{"total": 1, "avgLatency": None}, {"n": 1}]
        self.db.query_all.return_value = [{"service_id": "service-1", "auth_secret_ref": ""}]
        result = function("admin", None, Page(number=2, size=3))
        self.assertEqual(result, {"items": [{"service_id": "service-1", "outbound_credential_configured": False}],
                                 "total": 1, "page": 2, "pageSize": 3,
                                 "stats": {"total": 1, "avgLatency": None}})
        params = self.db.query_all.call_args.args[1]
        self.assertEqual((params["page_limit"], params["page_offset"]), (3, 3))
        self.assert_closed()

    def test_read_exception_still_closes_without_committing(self):
        function, module = self.handler("list_services")
        self.stack.enter_context(patch.object(module.catalog, "list_services", side_effect=RuntimeError("read failed")))
        with self.assertRaisesRegex(RuntimeError, "read failed"):
            function("admin", None, None)
        self.assert_closed()

    def test_permission_write_failure_preserves_explicit_rollback_then_close(self):
        self.stack.enter_context(patch.object(user_admin_routes, "Database", return_value=self.db))
        self.db.query_one.return_value = {"user_id": "employee"}
        self.db.rollback.side_effect = lambda: self.events.append(("rollback",))
        self.patch("eagent_gateway.permission.page_permissions.direct_permissions", return_value=[])
        self.patch("eagent_gateway.permission.page_permissions.replace_permissions", side_effect=RuntimeError("write failed"))
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            user_admin_routes.put_user_page_permissions(
                "employee", SimpleNamespace(permissions=[]), "admin")
        self.assertEqual(self.events, [("rollback",), ("close",)])
        self.db.rollback.assert_called_once_with()
        self.db.close.assert_called_once_with()
        self.db.commit.assert_not_called()

    def test_pagination_remains_opt_in_and_rejects_mixed_modes(self):
        def request(query):
            return Request({"type": "http", "query_string": query.encode()})
        self.assertIsNone(optional_page(request(""), None, None))
        self.assertEqual(optional_page(request("pageSize=4"), None, 4),
                         Page(1, 4, {"pageSize": "4"}))
        with self.assertRaises(ValidationError):
            optional_page(request("page=1&limit=20"), 1, None)

    def test_create_master_seeds_before_commit_and_mounts_after_commit(self):
        function, module = self.handler("create_agent")
        self.db.query_one.return_value = None
        self.stack.enter_context(patch.object(module.master_service, "get_master_by_slug", return_value=None))
        master = {"master_id": "master-1", "slug": "new-agent", "model_config_json": json.dumps({
            "base_url": "https://model.invalid/v1", "model": "test", "api_key": "dummy-key-1234"})}
        self.stack.enter_context(patch.object(module.master_service, "get_master", return_value=master))
        self.patch("eagent_gateway.runtime.seed_prompt.seed_prompt_for", side_effect=lambda *args: self.events.append(("seed",)))
        self.patch("eagent_gateway.a2a_server.mounts.mount_master", side_effect=lambda *args: self.events.append(("mount",)))
        self.patch("eagent_gateway.api.team_liveness.invalidate_team", side_effect=lambda *args: self.events.append(("invalidate",)))
        body = SimpleNamespace(sourceType="COMPOSITE", slug="new-agent", displayName="New agent", description="", maxIters=3)
        result = asyncio.run(function(body, "employee"))
        self.assertEqual([event[0] for event in self.events], ["execute", "seed", "commit", "mount", "invalidate", "close"])
        self.assertNotIn("model_config_json", result)
        self.assertNotIn("dummy-key-1234", json.dumps(result))
        self.assertIn("model_config_json", master)
        self.assert_closed(committed=True)

    def test_binding_replace_validates_then_writes_commits_and_remounts(self):
        function, module = self.handler("put_bindings")
        self.stack.enter_context(patch.object(module.master_service, "get_master", return_value={"master_id": "master-1"}))
        self.db.query_all.side_effect = [[{"service_id": "old", "binding_id": "b-old"},
                                        {"service_id": "kept", "binding_id": "b-kept"}], []]
        self.patch("eagent_gateway.a2a_server.mounts.remount_master", side_effect=lambda *args: self.events.append(("remount",)))
        body = SimpleNamespace(bindings=[{"serviceId": "kept", "toolName": "kept-tool"}, {"serviceId": "new"}])
        self.assertEqual(function("master-1", body), {"bindings": []})
        self.assertEqual([event[0] for event in self.events], ["execute", "execute", "execute", "commit", "remount", "close"])
        writes = self.db.execute.call_args_list
        self.assertTrue(writes[0].args[0].startswith("DELETE"))
        self.assertTrue(writes[1].args[0].startswith("UPDATE"))
        self.assertTrue(writes[2].args[0].strip().startswith("INSERT"))
        self.assertEqual(writes[2].args[1]["mode"], "TOOL")
        self.assert_closed(committed=True)

    def test_invalid_binding_release_is_rejected_before_any_write(self):
        function, module = self.handler("put_bindings")
        self.stack.enter_context(patch.object(module.master_service, "get_master", return_value={"master_id": "master-1"}))
        self.db.query_all.return_value = []
        self.db.query_one.return_value = {"service_id": "other-service"}
        with self.assertRaises(ValidationError):
            function("master-1", SimpleNamespace(bindings=[{"serviceId": "service-1", "releaseId": "release-1"}]))
        self.db.execute.assert_not_called()
        self.assert_closed()

    def test_publish_and_rollback_preserve_audit_memory_commit_order(self):
        for name in ("publish_prompt", "rollback_prompt"):
            with self.subTest(name=name):
                self.events.clear()
                self.db.reset_mock()
                function, _ = self.handler(name)
                self.db.query_one.return_value = {"version_no": 4}
                self.patch("eagent_gateway.runtime.memory.add_memory", side_effect=lambda *args, **kwargs: self.events.append(("memory",)))
                self.assertEqual(function("master-1", "version-4", "employee"), {"published": "version-4", "hot_update": True})
                self.assertEqual([event[0] for event in self.events], ["execute"] * 4 + ["memory", "commit", "close"])
                self.assertIn("eag_audit_event", self.events[3][1])
                self.assertEqual(self.events[3][2]["u"], "employee")
                self.assert_closed(committed=True)

    def test_missing_prompt_does_not_write(self):
        function, _ = self.handler("publish_prompt")
        self.db.query_one.return_value = None
        with self.assertRaises(NotFoundError):
            function("master-1", "missing", "employee")
        self.db.execute.assert_not_called()
        self.assert_closed()

    def test_authorization_members_trim_deduplicate_and_preserve_order(self):
        function, _ = self.handler("put_service_users")
        self.db.query_one.side_effect = [{"x": 1}, {"status": "active"}, {"status": "active"}]
        result = function("service-1", SimpleNamespace(capUserNames=[" b ", "", "a", "b", "  ", " a "]))
        self.assertEqual(result, {"service_id": "service-1", "capUserNames": ["b", "a"]})
        inserted_users = [call.args[1]["u"] for call in self.db.execute.call_args_list[1:]]
        self.assertEqual(inserted_users, ["b", "a"])
        self.assert_closed(committed=True)

    def test_disabled_grant_target_fails_before_deleting_existing_grants(self):
        function, _ = self.handler("put_service_users")
        self.db.query_one.side_effect = [{"x": 1}, {"status": "disabled"}]
        with self.assertRaises(ValidationError):
            function("service-1", SimpleNamespace(capUserNames=["disabled-user"]))
        self.db.execute.assert_not_called()
        self.assert_closed()

    def test_admin_dependency_accepts_admin_and_rejects_employee_and_anonymous(self):
        for is_admin in (True, False, None):
            with self.subTest(is_admin=is_admin):
                request = Request({"type": "http", "headers": []})
                if is_admin is not None:
                    request.state.session_ctx = SimpleNamespace(user_id="employee", is_admin=lambda: is_admin)
                if is_admin:
                    self.assertEqual(require_admin_dep(request), "employee")
                else:
                    expected = UnauthenticatedError if is_admin is None else ForbiddenError
                    with self.assertRaises(expected):
                        require_admin_dep(request)


if __name__ == "__main__":
    unittest.main()
