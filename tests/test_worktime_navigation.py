"""Rendered navigation keeps personal records apart from HR/CEO management."""

import os
import tempfile
import unittest
from datetime import date, datetime
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flask import g, url_for

from app import create_app, db
from app.models import Contract, ContractType, LegalEntity, PlaceOfWork, User, UserPrivilege, UserProfile
from app.page_access import invalidate_access_cache
from app.page_access_models import PageRolePermission
from app.worktime_models import WorkSchedule, WorkTimeEntry
from app.worktime_service import build_payload


class SidebarLinks(HTMLParser):
    """Inspect semantic ancestry instead of coupling tests to CSS layout."""
    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__()
        self.stack, self.links, self.ids = [], [], set()
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        ancestors = [item[1] for item in self.stack]
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "a" and any("sidebar-nav" in parent.get("class", "").split() for parent in ancestors):
            self.links.append({**attrs, "ancestors": {parent["id"] for parent in ancestors if parent.get("id")}})
        if tag not in self.void_tags:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def matching(self, path):
        return [link for link in self.links if urlsplit(link.get("href", "")).path == path]


class WorktimeNavigationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///" + self.directory.name + "/navigation.db", "SECRET_KEY": "navigation-tests"}):
            self.app = create_app()
        self.app.config["TESTING"] = True
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users, self.contracts = {}, {}
        entity = LegalEntity(name="Test nursery", address="Example", om_id="123456", tax_number="12345678901")
        self.site = PlaceOfWork(legal_entity=entity, address="Shared workplace")
        db.session.add_all([entity, self.site])
        for name, privilege in (
            ("employee", UserPrivilege.employee), ("historical", UserPrivilege.employee),
            ("future", UserPrivilege.employee), ("uncontracted", UserPrivilege.employee),
            ("hr", UserPrivilege.hr), ("hr_no_contract", UserPrivilege.hr),
            ("ceo", UserPrivilege.ceo), ("developer", UserPrivilege.developer),
        ):
            user = User(username=name, email=f"{name}@example.invalid", password_hash="unused", privilege=privilege)
            user.profile = UserProfile(full_name=f"Navigation {name}")
            db.session.add(user)
            self.users[name] = user
        db.session.flush()
        for name in ("employee", "historical", "future", "hr", "ceo", "developer"):
            start = date(2099, 1, 1) if name == "future" else date(2020, 1, 1)
            end = date(2020, 12, 31) if name == "historical" else None
            contract = Contract(user=self.users[name], employer=entity, place_of_work=self.site,
                                contract_type=ContractType.secretary, job_title=f"Job {name}",
                                start_date=start, end_date=end, working_hours_per_week=40)
            db.session.add(contract)
            self.contracts[name] = contract
        db.session.commit()
        self.login("hr")

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.directory.cleanup()

    def login(self, name):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.users[name].get_id()
            session["_fresh"] = True
            session["locale"] = "en"
            session["worktime_csrf_token"] = "navigation-csrf"
        g.pop("_login_user", None)

    def path(self, endpoint):
        with self.app.test_request_context():
            return url_for(endpoint)

    def rule(self, key, role, allowed):
        row = db.session.get(PageRolePermission, (key, role))
        if row is None:
            row = PageRolePermission(page_key=key, role=role)
            db.session.add(row)
        row.allowed = allowed
        db.session.commit()
        invalidate_access_cache()

    def sidebar(self, path="/profile"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return SidebarLinks(response.get_data(as_text=True))

    def seed_register(self):
        _, fingerprint = build_payload(self.site.id, 2026, 10)
        schedule = WorkSchedule(place_of_work=self.site, year=2026, month=10, revision="a" * 32,
                                source_hash=fingerprint, status="draft", generated_at=datetime(2026, 10, 1))
        db.session.add(schedule)
        for name in ("hr", "employee"):
            db.session.add(WorkTimeEntry(
                schedule=schedule, contract=self.contracts[name], user=self.users[name],
                day=date(2026, 10, 5), start_minute=480, end_minute=980,
                break_start=720, break_minutes=20, work_minutes=480, teaching_minutes=0,
                shift="morning", note=f"PRIVATE RECORD FOR {name.upper()}",
            ))
        db.session.commit()
        return schedule

    def test_sidebar_moves_personal_records_under_workspace_and_keeps_top_level_destinations(self):
        nav = self.sidebar()
        workspace = [urlsplit(link["href"]).path for link in nav.links if "workspace-menu" in link["ancestors"]]
        self.assertEqual(workspace, [self.path(endpoint) for endpoint in (
            "dashboard", "edit_profile", "manage_dependents", "qualifications.index",
        )])
        for endpoint in ("leaves", "worktime.index"):
            links = nav.matching(self.path(endpoint))
            self.assertEqual(len(links), 1)
            self.assertTrue({"workspace-menu", "management-menu", "records-menu"}.isdisjoint(links[0]["ancestors"]))
        self.assertNotIn("records-menu", nav.ids)
        self.assertEqual(len(nav.matching(self.path("worktime.groups"))), 1)
        managed = nav.matching(self.path("worktime.management"))
        self.assertEqual(len(managed), 1)
        self.assertIn("management-menu", managed[0]["ancestors"])

    def test_any_contract_including_historical_or_future_exposes_own_register_for_every_role(self):
        for name in ("employee", "historical", "future", "hr", "ceo", "developer"):
            with self.subTest(account=name):
                self.login(name)
                nav = self.sidebar()
                self.assertEqual(len(nav.matching(self.path("worktime.index"))), 1)
                response = self.client.get("/worktime?year=2026&month=10")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(bool(nav.matching(self.path("worktime.management"))), name in {"hr", "ceo"})
        for name in ("uncontracted", "hr_no_contract"):
            with self.subTest(account=name):
                self.login(name)
                nav = self.sidebar()
                self.assertFalse(nav.matching(self.path("worktime.index")))
                self.assertEqual(self.client.get("/worktime?year=2026&month=10").status_code, 403)
                self.assertEqual(bool(nav.matching(self.path("worktime.management"))), name == "hr_no_contract")

    def test_any_contract_exposes_leave_calendar_but_does_not_override_denied_permission(self):
        for name in ("employee", "historical", "future", "hr", "ceo", "developer"):
            with self.subTest(account=name):
                self.login(name)
                self.assertEqual(len(self.sidebar().matching("/leaves")), 1)
                self.assertEqual(self.client.get("/leaves").status_code, 200)
                dashboard = self.client.get("/dashboard").get_data(as_text=True)
                self.assertIn("Your time away, at a glance.", dashboard)
        self.rule("leaves", "employee", False)
        for name in ("employee", "historical", "future"):
            with self.subTest(denied_account=name):
                self.login(name)
                self.assertFalse(self.sidebar().matching("/leaves"))
                self.assertEqual(self.client.get("/leaves").status_code, 403)
                self.assertEqual(self.client.post("/leaves").status_code, 403)

    def test_settings_menu_groups_only_allowed_settings_and_tracks_active_page(self):
        endpoints = ("manage_privileges", "page_access.settings", "mail_settings.settings")
        self.login("developer")
        for active in endpoints:
            response = self.client.get(self.path(active))
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            nav = SidebarLinks(html)
            self.assertNotIn("management-menu", nav.ids)
            self.assertIn('<span class="breadcrumb-root">Settings</span>', html)
            self.assertIn('<details class="nav-group has-current" open>\n          <summary class="nav-group-trigger" id="settings-trigger"', html)
            settings = [urlsplit(link["href"]).path for link in nav.links if "settings-menu" in link["ancestors"]]
            self.assertEqual(settings, [self.path(endpoint) for endpoint in endpoints])
            for endpoint in endpoints:
                links = nav.matching(self.path(endpoint))
                self.assertEqual(len(links), 1)
                self.assertEqual(links[0].get("aria-current") == "page", endpoint == active)
        self.login("hr")
        nav = self.sidebar()
        self.assertIn("settings-menu", nav.ids)
        self.assertIn("settings-menu", nav.matching(self.path("manage_privileges"))[0]["ancestors"])
        self.assertFalse(nav.matching(self.path("page_access.settings")))
        self.assertFalse(nav.matching(self.path("mail_settings.settings")))
        self.rule("manage_privileges", "hr", False)
        self.assertNotIn("settings-menu", self.sidebar().ids)

    def test_settings_menu_appears_for_custom_grant_and_disappears_after_revocation(self):
        self.login("employee")
        self.assertNotIn("settings-menu", self.sidebar().ids)
        self.rule("mail_settings.settings", "employee", True)
        nav = self.sidebar()
        self.assertNotIn("management-menu", nav.ids)
        self.assertEqual(len(nav.matching(self.path("mail_settings.settings"))), 1)
        self.assertIn("settings-menu", nav.matching(self.path("mail_settings.settings"))[0]["ancestors"])
        dashboard = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn('href="/settings"', dashboard)
        self.assertIn("Email settings", dashboard)
        self.rule("mail_settings.settings", "employee", False)
        self.assertNotIn("settings-menu", self.sidebar().ids)
        dashboard = self.client.get("/dashboard").get_data(as_text=True)
        self.assertNotIn('href="/settings"', dashboard)

    def test_hr_personal_page_renders_only_own_rows_and_no_management_forms(self):
        self.seed_register()
        response = self.client.get(f"/worktime?year=2026&month=10&place_id={self.site.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("PRIVATE RECORD FOR HR", html)
        self.assertNotIn("PRIVATE RECORD FOR EMPLOYEE", html)
        self.assertNotIn('id="worktime-user"', html)
        for action in ("/worktime/generate", "/worktime/confirm", "/worktime/entries/", "/worktime/merges"):
            self.assertNotIn(f'action="{action}', html)
        self.assertNotIn('href="/worktime/groups', html)

    def test_management_success_and_validation_error_return_to_management_with_employee_filter(self):
        schedule = self.seed_register()
        data = {"csrf_token": "navigation-csrf", "year": "2026", "month": "10", "place_id": str(self.site.id),
                "user_id": str(self.users["employee"].id), "revision": schedule.revision}
        for values in ({**data, "csrf_token": "invalid"}, data):
            with self.subTest(valid_csrf=values["csrf_token"] == "navigation-csrf"):
                response = self.client.post("/worktime/generate", data=values)
                self.assertEqual(response.status_code, 302)
                location = urlsplit(response.headers["Location"])
                self.assertEqual(location.path, self.path("worktime.management"))
                self.assertEqual(parse_qs(location.query)["user_id"], [str(self.users["employee"].id)])
                page = self.client.get(response.headers["Location"])
                self.assertEqual(page.status_code, 200)
                self.assertIn('id="worktime-user"', page.get_data(as_text=True))

    def test_groups_has_its_own_management_item_and_root_level_routes(self):
        self.assertEqual(self.path("worktime.groups"), "/groups")
        for path in (f"/groups?place_id={self.site.id}", f"/groups/new?place_id={self.site.id}"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            nav = SidebarLinks(html)
            group_links = nav.matching(self.path("worktime.groups"))
            self.assertEqual(len(group_links), 1)
            self.assertIn("management-menu", group_links[0]["ancestors"])
            self.assertEqual(group_links[0].get("aria-current"), "page")
            self.assertIsNone(nav.matching(self.path("worktime.management"))[0].get("aria-current"))
            self.assertNotIn('class="worktime-tabs"', html)
        managed = self.client.get(f"/worktime/manage?place_id={self.site.id}").get_data(as_text=True)
        self.assertIn('href="/groups?', managed)
        for name in ("employee", "developer"):
            self.login(name)
            for path in ("/worktime/manage", "/groups", "/groups/new", "/worktime/groups"):
                self.assertEqual(self.client.get(path).status_code, 403)


if __name__ == "__main__":
    unittest.main()
