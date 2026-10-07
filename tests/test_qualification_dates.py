"""Old qualification URLs cannot bypass the new HR-only metadata workflow."""

import os
import unittest
from datetime import date
from unittest.mock import patch

from flask import g

from app import create_app, db
from app.models import EducationalQualification, ProfessionalExam, User, UserPrivilege
from app.page_access import invalidate_access_cache
from app.page_access_models import PageRolePermission
from app.qualification_migration import migrate_legacy_qualifications
from app.qualification_models import QualificationRecord


class QualificationDateTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {
            "DATABASE_URL": "sqlite://", "SECRET_KEY": "qualification-date-test-only",
            "EMAIL_WORKER_ENABLED": "false",
        }):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for role in UserPrivilege:
            user = User(username=role.value, email=f"{role.value}@example.invalid",
                        password_hash="unused", privilege=role)
            self.users[role.value] = user
            db.session.add(user)
        db.session.commit()
        self.qualification = EducationalQualification(
            user=self.users["employee"], level_or_type="Degree", qualification_name="Original qualification",
            institution_name="Original university", degree_number="OLD-123", year_obtained=2019,
            date_obtained=None, highest=True,
        )
        self.exam = ProfessionalExam(user=self.users["employee"], qualification_name="Original exam",
                                     degree_number="OLD-EXAM", year_obtained=2024, date_obtained=date(2024, 2, 29))
        db.session.add_all([self.qualification, self.exam])
        db.session.commit()
        migrate_legacy_qualifications(db.engine)
        self.paths = ("/qualifications/add", f"/qualifications/{self.qualification.id}/edit", "/professional-exam")
        self.login()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def login(self, key="employee"):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.users[key].get_id()
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)

    def assert_retained(self):
        db.session.expire_all()
        self.assertEqual(EducationalQualification.query.count(), 1)
        self.assertEqual(ProfessionalExam.query.count(), 1)
        self.assertEqual(self.qualification.qualification_name, "Original qualification")
        self.assertEqual(self.qualification.degree_number, "OLD-123")
        self.assertEqual(self.qualification.year_obtained, 2019)
        self.assertIsNone(self.qualification.date_obtained)
        self.assertTrue(self.qualification.highest)
        self.assertEqual(self.exam.qualification_name, "Original exam")
        self.assertEqual(self.exam.degree_number, "OLD-EXAM")
        self.assertEqual(self.exam.year_obtained, 2024)
        self.assertEqual(self.exam.date_obtained, date(2024, 2, 29))
        self.assertEqual(QualificationRecord.query.count(), 2)

    def test_legacy_gets_redirect_to_unified_page_without_exposing_old_editors(self):
        for role in self.users:
            self.login(role)
            for path in self.paths:
                with self.subTest(role=role, path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.location, "/qualifications")
                    self.assertNotIn(b'name="date_obtained"', response.data)
        self.assert_retained()

    def test_old_forms_cannot_create_update_or_clear_records_for_any_role(self):
        data_sets = (
            {},
            {"qualification_name": "", "degree_number": "", "date_obtained": ""},
            {"level_or_type": "Changed", "qualification_name": "Changed", "institution_name": "Changed",
             "degree_number": "CHANGED", "date_obtained": "2026-01-01", "highest": "on"},
        )
        with self.client.session_transaction() as session:
            session["qualification_records_csrf_token"] = "previously-valid-token"
        for role in self.users:
            self.login(role)
            for path in self.paths:
                for data in data_sets:
                    with self.subTest(role=role, path=path, fields=data):
                        response = self.client.post(path, data={**data, "csrf_token": "previously-valid-token"})
                        self.assertEqual(response.status_code, 403)
        self.assert_retained()

    def test_legacy_edit_ids_never_disclose_another_employee_record(self):
        self.login("hr")
        for identifier in (self.qualification.id, 999999):
            path = f"/qualifications/{identifier}/edit"
            self.assertEqual(self.client.get(path).location, "/qualifications")
            self.assertEqual(self.client.post(path, data={"qualification_name": "Blocked"}).status_code, 403)
        self.assert_retained()

    def test_existing_own_page_denial_also_blocks_merged_exam_and_upload_page(self):
        db.session.add(PageRolePermission(page_key="add_qualification", role="employee", allowed=False))
        # A now-obsolete separate exam grant must not revive the unified page.
        db.session.add(PageRolePermission(page_key="professional_exam", role="employee", allowed=True))
        db.session.commit()
        invalidate_access_cache()
        for path in (*self.paths, "/qualifications"):
            for method in ("get", "post"):
                self.assertEqual(getattr(self.client, method)(path).status_code, 403)
        self.assert_retained()

    def test_legacy_year_and_exact_leap_date_are_visible_in_unified_history(self):
        response = self.client.get("/qualifications")
        self.assertEqual(response.status_code, 200)
        for content in (b"Original qualification", b"Original exam", b"2019", b"2024"):
            self.assertIn(content, response.data)
        self.assertNotIn(b"2019-01-01", response.data)
        records = {record.kind: record for record in QualificationRecord.query.all()}
        self.assertIsNone(records["qualification"].date_obtained)
        self.assertEqual(records["exam"].date_obtained, date(2024, 2, 29))
        self.assert_retained()

    def test_legacy_urls_require_authentication(self):
        self.client.get("/logout")
        g.pop("_login_user", None)
        for path in self.paths:
            for method in ("get", "post"):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.location)
        self.assert_retained()


if __name__ == "__main__":
    unittest.main()
