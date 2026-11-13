"""Managers can inspect staff records without acquiring ownership or write access."""

import os
import tempfile
import unittest
from datetime import date
from html.parser import HTMLParser
from unittest.mock import patch

from flask import g, template_rendered

from app import create_app, db
from app.models import EducationalQualification, User, UserPrivilege, UserProfile
from app.qualification_models import QualificationRecord
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
            qualification = QualificationRecord(
                status="processed", completion_state="completed", kind="qualification",
                user=self.users[key], level_or_type="Certificate", qualification_name=f"Unique qualification {key}",
                institution_name=f"Institute {key}", degree_number=f"QUAL-{key}",
                year_obtained=2025, date_obtained=date(2025, 9 + index, 15), highest=True,
            )
            exam = QualificationRecord(
                status="processed", completion_state="completed", kind="exam",
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
        return [(row.id, row.user_id, row.kind, row.status, row.qualification_name, row.institution_name,
                 row.degree_number, row.date_obtained, row.year_obtained, row.highest)
                for row in QualificationRecord.query.order_by(QualificationRecord.id)]

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

    def test_report_permission_never_grants_employee_processing_access(self):
        self.matrix_save({(REPORT_PAGE, "employee"): True})
        before = self.record_snapshot()
        self.login("employee")
        html = self.client.get(self.detail_path()).get_data(as_text=True)
        edit_path = f"/qualifications/manage/{self.qualifications['other'].id}/edit"
        self.assertNotIn(edit_path, LinkParser(html).links)
        self.assertEqual(self.client.get(edit_path).status_code, 403)
        self.assertEqual(self.client.post(edit_path, data={"qualification_name": "Forbidden"}).status_code, 403)
        self.assertEqual(self.record_snapshot(), before)

    def test_manager_detail_links_to_processing_without_counting_legacy_records_twice(self):
        legacy = EducationalQualification(
            user=self.users["other"], level_or_type="Certificate", qualification_name="Archived only",
            institution_name="Old college", degree_number="OLD", year_obtained=2025,
        )
        db.session.add(legacy)
        db.session.commit()
        self.login("hr")
        html = self.client.get(self.detail_path()).get_data(as_text=True)
        self.assertIn(f"/qualifications/manage/{self.qualifications['other'].id}/edit", LinkParser(html).links)
        self.assertNotIn("Archived only", html)

    def test_directory_and_detail_include_uploads_and_courses_in_progress(self):
        course = QualificationRecord(user=self.users["other"], status="processed", kind="teacher_training",
            qualification_name="Ongoing training", completion_state="in_progress", study_start_date=date(2025, 9, 1))
        uploaded = QualificationRecord(user=self.users["other"], status="uploaded")
        db.session.add_all([course, uploaded])
        db.session.commit()
        self.login("hr")
        html = self.client.get(self.detail_path()).get_data(as_text=True)
        self.assertIn("Ongoing training", html)
        self.assertIn("In progress", html)
        self.assertIn("Uploaded document", html)
        directory = self.client.get(f"{REPORT_ROOT}/employees?q=other").get_data(as_text=True)
        self.assertIn("Courses", directory)
        report = self.client.get(REPORT_ROOT, query_string={"year": "all", "kind": "teacher_training"}).get_data(as_text=True)
        self.assertIn("No records match the selected filters.", report)

    def test_teacher_assessments_render_separately_in_reports_directory_and_detail(self):
        assessment = QualificationRecord(
            user=self.users["other"], status="processed", kind="teacher_assessment",
            qualification_name="Pedagógus I.", completion_state="completed",
            year_obtained=2026, date_obtained=date(2026, 2, 21),
        )
        db.session.add(assessment)
        db.session.commit()
        self.login("hr")
        contexts = []

        def capture(sender, template, context, **extra):
            contexts.append(context)

        with template_rendered.connected_to(capture, self.app):
            response = self.client.get(REPORT_ROOT, query_string={"year": "2025"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Teacher assessments", response.get_data(as_text=True))
            self.assertEqual(contexts[-1]["totals"]["teacher_assessments"], 1)
            self.assertEqual(contexts[-1]["totals"]["qualifications"], 2)
            self.assertEqual(contexts[-1]["totals"]["exams"], 2)
            self.assertEqual(contexts[-1]["totals"]["total"], 5)

            response = self.client.get(REPORT_ROOT, query_string={"year": "2025", "kind": "teacher_assessment"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([row["id"] for row in contexts[-1]["records"]], [assessment.id])
            self.assertIn('value="teacher_assessment" selected', response.get_data(as_text=True))

            response = self.client.get(f"{REPORT_ROOT}/employees", query_string={"q": "other"})
            self.assertEqual(response.status_code, 200)
            person = contexts[-1]["users"][0]
            self.assertEqual((person["qualification_count"], person["exam_count"],
                              person["training_count"], person["teacher_assessment_count"]), (1, 1, 0, 1))
            self.assertIn("Teacher assessments", response.get_data(as_text=True))

            response = self.client.get(self.detail_path())
            self.assertEqual(response.status_code, 200)
            self.assertIn("Pedagógus I.", response.get_data(as_text=True))
            self.assertIn("Teacher assessment", response.get_data(as_text=True))
            self.assertEqual(contexts[-1]["totals"]["teacher_assessments"], 1)

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
