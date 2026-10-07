"""Managers can inspect staff records without acquiring ownership or write access."""

import os
import tempfile
import unittest
from datetime import date
from html.parser import HTMLParser
from unittest.mock import patch

from flask import g

from app import create_app, db
from app.models import EducationalQualification, ProfessionalExam, User, UserPrivilege, UserProfile
from app.page_access import ALL_ROLES, PAGE_DEFINITIONS, invalidate_access_cache
from app.page_access_models import PageAccessSettings, PageRolePermission


REPORT_PAGE = "qualification_reports.index"
REPORT_ROOT = "/qualification-reports"


class LinkParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href", ""))


class QualificationReportRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + os.path.join(self.directory.name, "reports.db"),
            "SECRET_KEY": "qualification-report-route-tests-only",
            "EMAIL_WORKER_ENABLED": "false",
        })
        self.environment.start()
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for key in ("employee", "other", "hr", "ceo", "developer"):
            role = UserPrivilege.employee if key == "other" else UserPrivilege(key)
            user = User(username=key, email=f"{key}@example.invalid", password_hash="unused", privilege=role)
            user.profile = UserProfile(full_name=f"Report Person {key}")
            self.users[key] = user
            db.session.add(user)
        db.session.flush()
        self.qualifications = {}
        self.exams = {}
        for index, key in enumerate(("employee", "other")):
            qualification = EducationalQualification(
                user=self.users[key], level_or_type="Certificate", qualification_name=f"Unique qualification {key}",
                institution_name=f"Institute {key}", degree_number=f"QUAL-{key}",
                year_obtained=2025, date_obtained=date(2025, 9 + index, 15), highest=True,
            )
            exam = ProfessionalExam(
                user=self.users[key], qualification_name=f"Unique exam {key}",
                degree_number=f"EXAM-{key}", year_obtained=2026, date_obtained=date(2026, 2 + index, 20),
            )
            db.session.add_all([qualification, exam])
            self.qualifications[key] = qualification
            self.exams[key] = exam
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.environment.stop()
        self.directory.cleanup()

    def login(self, key):
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = self.users[key].get_id()
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)
        invalidate_access_cache()

    def detail_path(self, key="other"):
        return f"{REPORT_ROOT}/employees/{self.users[key].id}"

    def paths(self):
        return (REPORT_ROOT, f"{REPORT_ROOT}/employees", self.detail_path())

    def matrix_save(self, changes):
        """Exercise the actual developer matrix, including persisted overrides."""
        self.login("developer")
        self.assertEqual(self.client.get("/page-access").status_code, 200)
        with self.client.session_transaction() as session:
            token = session["page_access_csrf_token"]
        rules = {(row.page_key, row.role): row.allowed for row in PageRolePermission.query.all()}
        rules.update(changes)
        selected = [f"{page['key']}:{role}" for page in PAGE_DEFINITIONS for role in ALL_ROLES
                    if rules.get((page["key"], role), role in page["default_roles"])]
        with patch("app.notification_delivery.wake_notifications"):
            response = self.client.post("/page-access", data={
                "csrf_token": token, "revision": db.session.get(PageAccessSettings, 1).revision,
                "permissions": selected,
            })
        self.assertEqual(response.status_code, 302)

    def links(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return LinkParser(response.get_data(as_text=True)).links

    def record_snapshot(self):
        db.session.expire_all()
        return (
            [(row.id, row.user_id, row.qualification_name, row.institution_name, row.degree_number,
              row.date_obtained, row.year_obtained, row.highest)
             for row in EducationalQualification.query.order_by(EducationalQualification.id)],
            [(row.id, row.user_id, row.qualification_name, row.degree_number, row.date_obtained, row.year_obtained)
             for row in ProfessionalExam.query.order_by(ProfessionalExam.id)],
        )

    def test_anonymous_visitors_must_sign_in_for_every_report_route(self):
        for path in self.paths():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])
                self.assertNotIn(b"Unique qualification", response.data)

    def test_hr_and_ceo_can_read_another_users_qualifications_and_exam(self):
        for role in ("hr", "ceo"):
            self.login(role)
            for path in self.paths():
                with self.subTest(role=role, path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.cache_control.no_store)
            detail = self.client.get(self.detail_path()).get_data(as_text=True)
            for value in ("Report Person other", "Unique qualification other", "Institute other",
                          "QUAL-other", "Unique exam other", "EXAM-other"):
                self.assertIn(value, detail)
            self.assertNotIn("Unique qualification employee", detail)
            self.assertNotIn("Unique exam employee", detail)

    def test_reports_are_hidden_and_direct_urls_denied_for_default_employee_and_developer(self):
        for role in ("employee", "developer"):
            self.login(role)
            for path in self.paths():
                with self.subTest(role=role, path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn(b"Unique qualification", response.data)
                    self.assertTrue(response.cache_control.no_store)
            for page in ("/dashboard", "/profile"):
                self.assertFalse(any(link.startswith(REPORT_ROOT) for link in self.links(page)))

    def test_report_is_available_in_developer_matrix_with_manager_defaults(self):
        self.login("developer")
        response = self.client.get("/page-access")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for role in ("hr", "ceo"):
            self.assertIn(f'value="{REPORT_PAGE}:{role}" checked', html)
        for role in ("employee", "developer"):
            self.assertIn(f'value="{REPORT_PAGE}:{role}">', html)
            self.assertNotIn(f'value="{REPORT_PAGE}:{role}" checked', html)

    def test_matrix_grant_opens_parent_and_children_without_profile_management(self):
        self.matrix_save({(REPORT_PAGE, "employee"): True})
        self.login("employee")
        for path in self.paths():
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertGreaterEqual(self.links("/dashboard").count(REPORT_ROOT), 2)
        self.assertIn(REPORT_ROOT, self.links("/profile"))
        self.assertEqual(self.client.get("/users/profiles").status_code, 403)
        self.assertEqual(self.client.get(f"/users/{self.users['other'].id}/profile").status_code, 403)

    def test_matrix_can_grant_read_access_to_developer_without_changing_locked_matrix_access(self):
        self.matrix_save({(REPORT_PAGE, "developer"): True})
        self.login("developer")
        for path in self.paths():
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/page-access").status_code, 200)

    def test_revocation_hides_navigation_dashboard_and_individual_profile_links(self):
        self.login("hr")
        self.assertGreaterEqual(self.links("/dashboard").count(REPORT_ROOT), 2)
        self.assertIn(self.detail_path(), self.links("/users/profiles?status=all"))
        self.matrix_save({(REPORT_PAGE, "hr"): False})
        self.login("hr")
        for path in self.paths():
            self.assertEqual(self.client.get(path).status_code, 403)
        for path in ("/dashboard", "/profile", "/users/profiles?status=all"):
            self.assertFalse(any(link.startswith(REPORT_ROOT) for link in self.links(path)))
        # Removing report access does not remove unrelated HR profile access.
        self.assertEqual(self.client.get(f"/users/{self.users['other'].id}/profile").status_code, 200)

    def test_unknown_employee_returns_404_for_authorised_managers(self):
        self.login("hr")
        response = self.client.get(f"{REPORT_ROOT}/employees/999999")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.cache_control.no_store)

    def test_read_only_routes_reject_mutations_and_leave_all_records_unchanged(self):
        self.login("hr")
        before = self.record_snapshot()
        for path in self.paths():
            for method in ("post", "put", "patch", "delete"):
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)(path, data={
                        "user_id": self.users["other"].id,
                        "qualification_name": "Unauthorised replacement",
                        "degree_number": "Changed",
                    })
                    self.assertEqual(response.status_code, 405)
        self.assertEqual(self.record_snapshot(), before)

    def test_report_permission_never_allows_editing_another_users_qualification(self):
        self.matrix_save({(REPORT_PAGE, "employee"): True})
        before = self.record_snapshot()
        for role in ("employee", "hr", "ceo"):
            self.login(role)
            self.assertEqual(self.client.get(self.detail_path()).status_code, 200)
            self.assertEqual(self.client.get("/qualifications/add").status_code, 200)
            with self.client.session_transaction() as session:
                token = session["qualification_records_csrf_token"]
            path = f"/qualifications/{self.qualifications['other'].id}/edit"
            self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.post(path, data={
                "csrf_token": token, "level_or_type": "Degree", "qualification_name": "Forbidden",
                "institution_name": "Changed", "degree_number": "Changed", "date_obtained": "2025-01-01",
            }).status_code, 404)
        self.assertEqual(self.record_snapshot(), before)

    def test_uploaded_record_text_is_escaped_in_manager_detail(self):
        malicious = '<script>alert("record")</script>'
        self.qualifications["other"].qualification_name = malicious
        self.exams["other"].qualification_name = malicious
        db.session.commit()
        self.login("hr")
        response = self.client.get(self.detail_path())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(malicious, response.get_data(as_text=True))
        self.assertIn("&lt;script&gt;", response.get_data(as_text=True))

    def test_malformed_or_duplicated_report_filters_are_rejected(self):
        self.login("hr")
        invalid_queries = (
            {"year": "2025/2026"}, {"year": "2025-09-01"}, {"year": "9999"},
            {"year": "２０２５"}, {"year": ["2025", "2026"]},
            {"user_id": "999999"}, {"user_id": "-1"}, {"user_id": "1 OR 1=1"},
            {"user_id": [str(self.users["other"].id), str(self.users["employee"].id)]},
            {"kind": "delete"}, {"kind": ["qualification", "exam"]},
            {"type": "unrecognised"}, {"type": ["exam", "exam"]},
            {"q": "x" * 201}, {"q": "bad\x00query"}, {"q": ["other", "employee"]},
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                response = self.client.get(REPORT_ROOT, query_string=query)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.cache_control.no_store)
        for query in ({"q": "x" * 201}, {"q": "bad\x00query"}, {"q": ["other", "employee"]}):
            self.assertEqual(self.client.get(f"{REPORT_ROOT}/employees", query_string=query).status_code, 400)

    def test_directory_search_and_detail_links_preserve_person_identity(self):
        self.login("hr")
        path = f"{REPORT_ROOT}/employees?q=other"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Report Person other", response.get_data(as_text=True))
        links = LinkParser(response.get_data(as_text=True)).links
        self.assertIn(self.detail_path("other"), links)
        self.assertNotIn(self.detail_path("employee"), links)
        # A forged query parameter cannot replace the person selected in the URL.
        response = self.client.get(self.detail_path("other"), query_string={"user_id": self.users["employee"].id})
        self.assertIn(b"Unique qualification other", response.data)
        self.assertNotIn(b"Unique qualification employee", response.data)


if __name__ == "__main__":
    unittest.main()
