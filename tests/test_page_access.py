"""One effective policy protects routes, the matrix and safe landing pages."""

import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from flask import g
from sqlalchemy import event
from werkzeug.datastructures import MultiDict

from app import create_app, db
from app.models import LeaveRequest, User, UserPrivilege
from app.page_access import (
    ALL_ROLES, PAGE_DEFINITIONS, SPECIAL_ENDPOINTS, UTILITY_ENDPOINTS,
    can_access_endpoint, can_access_page, invalidate_access_cache,
    landing_url, page_for_endpoint,
)
from app.page_access_models import PageAccessSettings, PageRolePermission


class PageAccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + os.path.join(self.directory.name, "access.db"),
            "SECRET_KEY": "page-access-test-secret", "EMAIL_WORKER_ENABLED": "false",
        })
        self.environment.start()
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.app.add_url_rule("/unmapped-private", "unmapped_private", lambda: "hidden")
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for role in UserPrivilege:
            user = User(username=role.value, email=f"{role.value}@example.invalid", password_hash="unused", privilege=role)
            db.session.add(user)
            self.users[role.value] = user
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.environment.stop()
        self.directory.cleanup()

    def login(self, role="developer"):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.users[role].get_id()
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)

    def rule(self, key, role, allowed):
        row = db.session.get(PageRolePermission, (key, role))
        if row is None:
            row = PageRolePermission(page_key=key, role=role)
            db.session.add(row)
        row.allowed = allowed
        db.session.commit()
        invalidate_access_cache()

    def form(self, selected=None):
        response = self.client.get("/page-access")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            token = session["page_access_csrf_token"]
        if selected is None:
            selected = [
                f"{page['key']}:{role}" for page in PAGE_DEFINITIONS for role in page["default_roles"]
                if page["key"] != "page_access.settings"
            ]
        return MultiDict([
            ("csrf_token", token), ("revision", db.session.get(PageAccessSettings, 1).revision),
            *[("permissions", value) for value in selected],
        ])

    def save(self, data):
        with patch("app.notification_delivery.wake_notifications"):
            return self.client.post("/page-access", data=data)

    def test_initialisation_creates_revision_without_overriding_default_roles(self):
        self.assertEqual(PageAccessSettings.query.count(), 1)
        self.assertEqual(PageRolePermission.query.count(), 0)
        self.assertEqual(len(db.session.get(PageAccessSettings, 1).revision), 32)
        for page in PAGE_DEFINITIONS:
            for role, user in self.users.items():
                expected = role in page["default_roles"]
                if page["key"] in {"leaves", "worktime.index"}:
                    expected = False
                if page["key"] == "manage_leaves":
                    expected = role == "ceo"
                with self.subTest(page=page["key"], role=role):
                    self.assertEqual(can_access_page(page["key"], user), expected)

    def test_every_application_route_is_mapped_or_explicit_utility(self):
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint == "unmapped_private":
                continue
            self.assertTrue(
                page_for_endpoint(rule.endpoint) or rule.endpoint in UTILITY_ENDPOINTS or rule.endpoint in SPECIAL_ENDPOINTS,
                rule.endpoint,
            )

    def test_unknown_authenticated_route_is_denied_by_default(self):
        self.assertEqual(self.client.get("/unmapped-private").status_code, 403)
        self.login()
        self.assertEqual(self.client.get("/unmapped-private").status_code, 403)
        self.assertEqual(self.client.get("/does-not-exist").status_code, 404)
        self.assertFalse(can_access_page("a-new-unknown-page", self.users["developer"]))

    def test_matrix_is_permanently_developer_only_even_with_corrupt_rows(self):
        self.assertEqual(self.client.get("/page-access").status_code, 302)
        for role in ("employee", "hr", "ceo"):
            self.rule("page_access.settings", role, True)
            self.login(role)
            self.assertFalse(can_access_page("page_access.settings", self.users[role]))
            self.assertEqual(self.client.get("/page-access").status_code, 403)
            self.assertEqual(self.client.post("/page-access").status_code, 403)
        self.rule("page_access.settings", "developer", False)
        self.login()
        response = self.client.get("/page-access")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="page_access.settings:developer" checked disabled', response.data)

    def test_developer_can_remove_every_other_permission_and_retains_matrix(self):
        self.login()
        response = self.save(self.form([]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PageRolePermission.query.count(), len(PAGE_DEFINITIONS) * len(ALL_ROLES))
        self.assertEqual(PageRolePermission.query.filter_by(allowed=True).count(), 1)
        self.assertEqual(self.client.get("/page-access").status_code, 200)
        self.assertEqual(self.client.get("/dashboard").status_code, 403)
        with self.app.test_request_context():
            self.assertEqual(landing_url(self.users["developer"]), "/page-access")
        self.assertEqual(db.session.get(PageAccessSettings, 1).updated_by_id, self.users["developer"].id)

    def test_malformed_cells_csrf_and_locked_non_developer_grants_are_atomic(self):
        self.login()
        for value in ("unknown:employee", "dashboard:unknown", "dashboard", "page_access.settings:employee"):
            data = self.form()
            data.add("permissions", value)
            self.assertEqual(self.save(data).status_code, 400)
            self.assertEqual(PageRolePermission.query.count(), 0)
        for key, value in (("csrf_token", ""), ("csrf_token", "árvíz"), ("unexpected", "1")):
            data = self.form()
            data[key] = value
            self.assertEqual(self.save(data).status_code, 400)
            self.assertEqual(PageRolePermission.query.count(), 0)
        data = self.form()
        data.add("permissions", "dashboard:employee")
        self.assertEqual(self.save(data).status_code, 400)

    def test_stale_form_cannot_restore_removed_permissions(self):
        self.login()
        stale = self.form()
        self.assertEqual(self.save(self.form([])).status_code, 302)
        self.assertEqual(self.save(stale).status_code, 302)
        self.assertFalse(db.session.get(PageRolePermission, ("dashboard", "employee")).allowed)
        self.assertIn(b"changed in another session", self.client.get("/page-access").data)

    def test_concurrent_update_rolls_back_all_permission_cells(self):
        self.login()
        form = self.form([])

        def competing_update(session, flush_context, instances):
            with db.engine.begin() as connection:
                connection.execute(PageAccessSettings.__table__.update().values(revision="f" * 32))

        event.listen(db.session(), "before_flush", competing_update, once=True)
        self.assertEqual(self.save(form).status_code, 302)
        self.assertEqual(PageRolePermission.query.count(), 0)
        self.assertEqual(db.session.get(PageAccessSettings, 1).revision, "f" * 32)

    def test_revocation_is_seen_next_request_in_existing_login_session(self):
        self.login("employee")
        self.assertEqual(self.client.get("/profile").status_code, 200)
        self.rule("edit_profile", "employee", False)
        self.assertEqual(self.client.get("/profile").status_code, 403)
        self.assertEqual(self.client.post("/profile", data={"last_name": "Must not save"}).status_code, 403)
        self.rule("edit_profile", "employee", True)
        self.assertEqual(self.client.get("/profile").status_code, 200)

    def test_all_denied_routes_and_children_reject_get_and_post_before_mutation(self):
        for page in PAGE_DEFINITIONS:
            if page["key"] != "page_access.settings":
                db.session.add(PageRolePermission(page_key=page["key"], role="developer", allowed=False))
        db.session.commit()
        self.login()
        adapter = self.app.url_map.bind("localhost")
        for rule in self.app.url_map.iter_rules():
            parent = page_for_endpoint(rule.endpoint)
            if parent is None or parent == "page_access.settings":
                continue
            values = {argument: 1 for argument in rule.arguments}
            for method in ("GET", "POST"):
                if method not in rule.methods:
                    continue
                path = adapter.build(rule.endpoint, values, method=method)
                with self.subTest(endpoint=rule.endpoint, method=method):
                    self.assertEqual(self.client.open(path, method=method).status_code, 403)

    def test_export_and_photo_utilities_keep_independent_ownership_scope(self):
        employee = self.users["employee"]
        other = self.users["hr"]
        self.assertTrue(can_access_endpoint("profile_photos.show_photo", employee, user_id=employee.id))
        self.assertFalse(can_access_endpoint("profile_photos.show_photo", employee, user_id=other.id))
        self.rule("worktime.groups", "employee", True)
        self.assertFalse(can_access_endpoint("worktime.export", employee, user_id=other.id))
        self.rule("worktime.management", "employee", True)
        self.assertTrue(can_access_endpoint("worktime.export", employee, user_id=other.id))
        self.rule("manage_user_profiles", "employee", True)
        self.assertTrue(can_access_endpoint("profile_photos.show_photo", employee, user_id=other.id))

    def test_gyap_download_requires_one_of_its_parent_pages(self):
        employee = SimpleNamespace(
            is_authenticated=True, privilege=UserPrivilege.employee,
            contracts=[SimpleNamespace(leadership_positions=[])],
        )
        self.assertTrue(can_access_endpoint("gyap.download_form", employee))
        self.rule("leaves", "employee", False)
        self.assertFalse(can_access_endpoint("gyap.download_form", employee))
        self.rule("gyap.manage_forms", "employee", True)
        self.assertTrue(can_access_endpoint("gyap.download_form", employee))

    def test_accounts_without_contract_cannot_open_or_submit_own_leave_calendar(self):
        for role, user in self.users.items():
            self.rule("leaves", role, True)
            self.login(role)
            with self.subTest(role=role):
                self.assertFalse(can_access_page("leaves", user))
                self.assertEqual(self.client.get("/leaves").status_code, 403)
                self.assertEqual(self.client.post("/leaves", data={"action": "create"}).status_code, 403)
                dashboard = self.client.get("/dashboard")
                self.assertEqual(dashboard.status_code, 200)
                self.assertNotIn(b'href="/leaves"', dashboard.data)
                self.assertNotIn(b"Your time away, at a glance.", dashboard.data)
        self.assertEqual(LeaveRequest.query.count(), 0)
        self.login("ceo")
        self.assertEqual(self.client.get("/leaves/manage").status_code, 200)

    def test_uncontracted_gyap_download_needs_an_independent_management_grant(self):
        user = self.users["employee"]
        self.assertFalse(can_access_endpoint("gyap.download_form", user))
        self.rule("gyap.manage_forms", "employee", True)
        self.assertTrue(can_access_endpoint("gyap.download_form", user))

    def test_leadership_applicability_honours_inclusive_explicit_day(self):
        appointment = SimpleNamespace(start_date=date(2030, 1, 2), end_date=date(2030, 1, 3))
        user = SimpleNamespace(is_authenticated=True, privilege=UserPrivilege.employee,
                               contracts=[SimpleNamespace(leadership_positions=[appointment])])
        self.assertFalse(can_access_page("manage_leaves", user, day=date(2030, 1, 1)))
        self.assertTrue(can_access_page("manage_leaves", user, day=date(2030, 1, 2)))
        self.assertTrue(can_access_page("manage_leaves", user, day=date(2030, 1, 3)))
        self.assertFalse(can_access_page("manage_leaves", user, day=date(2030, 1, 4)))
        self.rule("manage_leaves", "employee", False)
        self.assertFalse(can_access_page("manage_leaves", user, day=date(2030, 1, 2)))

    def test_empty_access_has_usable_logout_and_safe_landing(self):
        for page in PAGE_DEFINITIONS:
            self.rule(page["key"], "employee", False)
        self.login("employee")
        self.assertEqual(self.client.get("/").location, "/access-unavailable")
        response = self.client.get("/access-unavailable")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No pages available", response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/logout").status_code, 302)


if __name__ == "__main__":
    unittest.main()
