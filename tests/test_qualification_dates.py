"""Exact qualification dates, legacy completion and mutation safety."""

import os
import unittest
from datetime import date
from unittest.mock import patch

from flask import g, template_rendered

from app import create_app, db
from app.models import EducationalQualification, ProfessionalExam, User, UserPrivilege


class QualificationDateTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "SECRET_KEY": "qualification-date-test-only"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.today = date(2026, 10, 4)
        self.date_patch = patch("app.routes.local_today", return_value=self.today)
        self.date_patch.start()
        self.users = {}
        for key in ("employee", "other", "hr"):
            user = User(username=key, email=f"{key}@example.invalid", password_hash="unused",
                        privilege=UserPrivilege.hr if key == "hr" else UserPrivilege.employee)
            self.users[key] = user
            db.session.add(user)
        db.session.commit()
        self.login()

    def tearDown(self):
        self.date_patch.stop()
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def login(self, key="employee"):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.users[key].get_id()
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)
        g.pop("leave_approval_policy", None)

    def token(self, path="/qualifications/add"):
        self.assertEqual(self.client.get(path).status_code, 200)
        with self.client.session_transaction() as session:
            return session["qualification_records_csrf_token"]

    def captured(self, method, path, **kwargs):
        contexts = []
        def record(sender, template, context, **extra):
            contexts.append(context)
        with template_rendered.connected_to(record, self.app):
            response = getattr(self.client, method)(path, **kwargs)
        return response, contexts[-1] if contexts else None

    def qualification_data(self, **changes):
        data = dict(level_or_type="Degree", qualification_name="Teacher", institution_name="University",
                    degree_number="CERT-123", date_obtained="2024-02-29")
        data.update(changes)
        return data

    def exam_data(self, **changes):
        data = dict(qualification_name="Professional qualification", degree_number="EXAM-123", date_obtained="2024-02-29")
        data.update(changes)
        return data

    def post(self, path, data, csrf=None):
        values = dict(data)
        values["csrf_token"] = self.token(path) if csrf is None else csrf
        return self.captured("post", path, data=values)

    def qualification(self, key="employee", **changes):
        values = dict(user=self.users[key], level_or_type="Degree", qualification_name="Original qualification",
                      institution_name="Original university", degree_number="OLD-123", year_obtained=2019,
                      highest=False)
        values.update(changes)
        record = EducationalQualification(**values)
        db.session.add(record)
        db.session.commit()
        return record

    def exam(self, **changes):
        values = dict(user=self.users["employee"], qualification_name="Original exam", degree_number="OLD-EXAM", year_obtained=2018)
        values.update(changes)
        record = ProfessionalExam(**values)
        db.session.add(record)
        db.session.commit()
        return record

    def test_new_qualification_and_exam_store_full_leap_date_and_derive_legacy_year(self):
        for path, data, model in (
            ("/qualifications/add", self.qualification_data(year_obtained="1999"), EducationalQualification),
            ("/professional-exam", self.exam_data(year_obtained="1999"), ProfessionalExam),
        ):
            with self.subTest(path=path):
                response, _ = self.post(path, data)
                self.assertEqual(response.status_code, 302)
                record = model.query.one()
                self.assertEqual(record.date_obtained, date(2024, 2, 29))
                self.assertEqual(record.year_obtained, 2024)

    def test_missing_year_only_malformed_future_and_pre1900_dates_never_create_records(self):
        invalid_dates = (
            None, "", "2024", "20240229", "2024-W09-4", "2024-2-29", "2023-02-29",
            "2024-04-31", "2024-13-01", "2024-02-29T00:00:00", "２０２４-02-29",
            "1899-12-31", "2026-10-05",
        )
        for path, factory, model in (
            ("/qualifications/add", self.qualification_data, EducationalQualification),
            ("/professional-exam", self.exam_data, ProfessionalExam),
        ):
            for invalid in invalid_dates:
                with self.subTest(path=path, date=invalid):
                    data = factory(date_obtained=invalid, year_obtained="2024")
                    if invalid is None:
                        data.pop("date_obtained")
                    response, context = self.post(path, data)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(model.query.count(), 0)
                    self.assertEqual(context["form_values"]["date_obtained"], invalid or "")

    def test_lower_bound_and_budapest_today_are_inclusive(self):
        for value in ("1900-01-01", "2026-10-04"):
            for path, factory, model in (
                ("/qualifications/add", self.qualification_data, EducationalQualification),
                ("/professional-exam", self.exam_data, ProfessionalExam),
            ):
                with self.subTest(path=path, date=value):
                    response, _ = self.post(path, factory(date_obtained=value))
                    self.assertEqual(response.status_code, 302)
                    record = model.query.order_by(model.id.desc()).first()
                    self.assertEqual(record.date_obtained.isoformat(), value)
                    self.assertEqual(record.year_obtained, int(value[:4]))

    def test_both_forms_emit_csrf_and_calendar_boundaries_with_shared_token(self):
        qualification_token = self.token()
        exam_token = self.token("/professional-exam")
        self.assertEqual(qualification_token, exam_token)
        for path in ("/qualifications/add", "/professional-exam"):
            response = self.client.get(path)
            self.assertIn(b'name="csrf_token"', response.data)
            self.assertIn(b'name="date_obtained"', response.data)
            self.assertIn(b'min="1900-01-01"', response.data)
            self.assertIn(b'max="2026-10-04"', response.data)
            self.assertNotIn(b'name="year_obtained"', response.data)

    def test_legacy_gets_preserve_year_without_inventing_date(self):
        qualification = self.qualification()
        exam = self.exam()
        for path, model_key, record in (
            (f"/qualifications/{qualification.id}/edit", "qualification", qualification),
            ("/professional-exam", "exam", exam),
        ):
            response, context = self.captured("get", path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(context["form_values"]["date_obtained"], "")
            self.assertIsNone(context[model_key].date_obtained)
            self.assertIn(str(record.year_obtained).encode(), response.data)
            self.assertIn(b"Exact date not recorded", response.data)
            self.assertNotIn(f'value="{record.year_obtained}-01-01"'.encode(), response.data)
        db.session.expire_all()
        self.assertEqual(qualification.year_obtained, 2019)
        self.assertEqual(exam.year_obtained, 2018)

    def test_own_legacy_qualification_can_be_edited_to_complete_date(self):
        record = self.qualification()
        record_id = record.id
        response, _ = self.post(f"/qualifications/{record.id}/edit", self.qualification_data(date_obtained="2019-06-20"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EducationalQualification.query.count(), 1)
        db.session.expire_all()
        record = db.session.get(EducationalQualification, record_id)
        self.assertEqual(record.date_obtained, date(2019, 6, 20))
        self.assertEqual(record.year_obtained, 2019)
        self.assertEqual(record.qualification_name, "Teacher")

    def test_other_accounts_cannot_view_or_edit_qualification_even_as_hr(self):
        record = self.qualification()
        path = f"/qualifications/{record.id}/edit"
        for key in ("other", "hr"):
            self.login(key)
            token = self.token()
            self.assertEqual(self.client.get(path).status_code, 404)
            response = self.client.post(path, data={**self.qualification_data(), "csrf_token": token})
            self.assertEqual(response.status_code, 404)
        db.session.expire_all()
        self.assertEqual(record.qualification_name, "Original qualification")
        self.assertIsNone(record.date_obtained)

    def test_invalid_create_or_edit_does_not_change_highest_or_existing_data(self):
        highest = self.qualification(highest=True)
        other = self.qualification(qualification_name="Other qualification")
        for path in ("/qualifications/add", f"/qualifications/{other.id}/edit"):
            response, context = self.post(path, self.qualification_data(date_obtained="2024", highest="on"))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(context["form_values"]["highest"])
            db.session.expire_all()
            self.assertTrue(highest.highest)
            self.assertFalse(other.highest)
            self.assertEqual(other.qualification_name, "Other qualification")
            self.assertIsNone(other.date_obtained)
            self.assertEqual(EducationalQualification.query.count(), 2)
        response, _ = self.post(f"/qualifications/{other.id}/edit", self.qualification_data(highest="on"))
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertFalse(highest.highest)
        self.assertTrue(other.highest)
        self.assertEqual(EducationalQualification.query.filter_by(highest=True).count(), 1)

    def test_required_qualification_text_error_preserves_highest_before_any_mutation(self):
        highest = self.qualification(highest=True)
        response, context = self.post("/qualifications/add", self.qualification_data(institution_name="   ", highest="on"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(context["form_values"]["institution_name"], "   ")
        db.session.expire_all()
        self.assertTrue(highest.highest)
        self.assertEqual(EducationalQualification.query.count(), 1)

    def test_invalid_forms_keep_every_submitted_field_for_correction(self):
        for path, data in (
            ("/qualifications/add", self.qualification_data(level_or_type="Submitted level", qualification_name="Submitted name",
                institution_name="Submitted institution", degree_number="SUBMITTED-123", date_obtained="2023-02-29", highest="on")),
            ("/professional-exam", self.exam_data(qualification_name="Submitted exam", degree_number="SUBMITTED-EXAM", date_obtained="2023-02-29")),
        ):
            response, context = self.post(path, data)
            self.assertEqual(response.status_code, 200)
            for key, value in data.items():
                self.assertEqual(context["form_values"][key], value if key != "highest" else True)
                if key != "highest":
                    self.assertIn(f'value="{value}"'.encode(), response.data)

    def test_exam_invalid_update_preserves_existing_then_valid_update_sets_exact_date(self):
        exam = self.exam()
        for invalid in ("", "2023-02-29", "2026-10-05"):
            response, _ = self.post("/professional-exam", self.exam_data(date_obtained=invalid))
            self.assertEqual(response.status_code, 200)
            db.session.expire_all()
            self.assertEqual(exam.qualification_name, "Original exam")
            self.assertEqual(exam.degree_number, "OLD-EXAM")
            self.assertEqual(exam.year_obtained, 2018)
            self.assertIsNone(exam.date_obtained)
        response, _ = self.post("/professional-exam", self.exam_data(date_obtained="2018-06-12"))
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertEqual(exam.date_obtained, date(2018, 6, 12))
        self.assertEqual(exam.year_obtained, 2018)

    def test_stale_year_only_or_incomplete_exam_post_never_removes_legacy_record(self):
        exam = self.exam()
        exam_id = exam.id
        malformed_forms = (
            {}, {"year_obtained": "2018"}, {"year_obtained": ""},
            {"qualification_name": "", "degree_number": "", "year_obtained": ""},
            {"qualification_name": "", "degree_number": "", "date_obtained": "", "year_obtained": "2018"},
            {"qualification_name": "New name", "degree_number": "New number", "year_obtained": "2019"},
        )
        for data in malformed_forms:
            with self.subTest(fields=data):
                response, _ = self.post("/professional-exam", data)
                self.assertEqual(response.status_code, 200)
                db.session.expire_all()
                saved = db.session.get(ProfessionalExam, exam_id)
                self.assertIsNotNone(saved)
                self.assertEqual(saved.qualification_name, "Original exam")
                self.assertEqual(saved.year_obtained, 2018)
                self.assertIsNone(saved.date_obtained)

    def test_explicit_blank_current_exam_form_removes_legacy_or_exact_record(self):
        for obtained in (None, date(2018, 6, 12)):
            self.exam(date_obtained=obtained)
            response, _ = self.post("/professional-exam", dict(qualification_name=" ", degree_number="", date_obtained=""))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(ProfessionalExam.query.count(), 0)
            # Long-lived test app contexts need the deleted relationship reloaded.
            db.session.expire(self.users["employee"], ["professional_exam"])

    def test_invalid_or_missing_csrf_prevents_all_writes_and_exam_removal(self):
        qualification = self.qualification(highest=True)
        exam = self.exam()
        cases = (
            ("/qualifications/add", self.qualification_data(highest="on")),
            (f"/qualifications/{qualification.id}/edit", self.qualification_data()),
            ("/professional-exam", self.exam_data()),
            ("/professional-exam", dict(qualification_name="", degree_number="", date_obtained="")),
        )
        for path, data in cases:
            for token in ("", "invalid", "árvíz"):
                with self.subTest(path=path, token=token):
                    response, _ = self.post(path, data, csrf=token)
                    self.assertEqual(response.status_code, 400)
        db.session.expire_all()
        self.assertEqual(EducationalQualification.query.count(), 1)
        self.assertTrue(qualification.highest)
        self.assertIsNone(qualification.date_obtained)
        self.assertEqual(exam.qualification_name, "Original exam")
        self.assertIsNone(exam.date_obtained)


if __name__ == "__main__":
    unittest.main()
