"""Evidence ownership, manager processing and period-independent classifications."""

import os
import tempfile
import unittest
from datetime import date
from io import BytesIO
from unittest.mock import patch

from flask import g
from PIL import Image
from werkzeug.datastructures import FileStorage, MultiDict

from app import create_app, db
from app.models import User, UserPrivilege, UserProfile
from app.page_access import invalidate_access_cache
from app.page_access_models import PageRolePermission
from app.qualification_models import QualificationDocument, QualificationRecord
from app.qualifications import MAX_UPLOAD_BYTES, QualificationValidationError, parse_metadata, validate_document


PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


class QualificationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + os.path.join(self.directory.name, "qualifications.db"),
            "SECRET_KEY": "qualification-workflow-tests", "EMAIL_WORKER_ENABLED": "false",
        })
        self.environment.start()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for key in ("employee", "other", "hr", "ceo", "developer"):
            user = User(username=key, email=f"{key}@example.invalid", password_hash="unused",
                        privilege=UserPrivilege.employee if key == "other" else UserPrivilege(key))
            user.profile = UserProfile(full_name="Alpha Employee" if key == "employee" else f"Person {key}")
            self.users[key] = user
            db.session.add(user)
        db.session.commit()
        self.login("employee")

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
            session["qualification_csrf_token"] = "test-token"
        g.pop("_login_user", None)
        invalidate_access_cache()

    def grant(self, page, role, allowed=True):
        db.session.add(PageRolePermission(page_key=page, role=role, allowed=allowed))
        db.session.commit()
        invalidate_access_cache()

    def parse(self, form, **kwargs):
        with self.app.test_request_context():
            return parse_metadata(form, **kwargs)

    def validate(self, source):
        with self.app.test_request_context():
            return validate_document(source)

    def upload(self, *, payload=PDF, filename="Diploma.pdf", extra=None):
        return self.client.post("/qualifications", data={
            "csrf_token": "test-token", "document": (BytesIO(payload), filename), **(extra or {}),
        })

    def seed(self, *, user="employee", processed=False, highest=False):
        record = QualificationRecord(user_id=self.users[user].id, status="processed" if processed else "uploaded",
                                     kind="qualification", completion_state="completed", qualification_name="Existing course",
                                     year_obtained=2020, highest=highest)
        db.session.add(record)
        db.session.commit()
        return record

    def form(self, record=None, **overrides):
        return {"csrf_token": "test-token", "revision": str(record.revision) if record else "",
                "action": "process", "kind": "qualification", "completion_state": "completed",
                "qualification_name": "Preschool qualification", "year_obtained": "2024", **overrides}

    def save(self, record, **overrides):
        return self.client.post(f"/qualifications/manage/{record.id}/edit", data=self.form(record, **overrides))

    def test_employee_upload_ignores_metadata_and_other_owner(self):
        response = self.upload(extra={"user_id": str(self.users["other"].id), "qualification_name": "Forged", "status": "processed", "highest": "1"})
        self.assertEqual(response.status_code, 302)
        record = QualificationRecord.query.one()
        self.assertEqual(record.user_id, self.users["employee"].id)
        self.assertEqual(record.status, "uploaded")
        self.assertIsNone(record.qualification_name)
        self.assertFalse(record.highest)
        self.assertEqual(record.documents[0].data, PDF)

    def test_upload_requires_csrf_and_rejects_empty_bad_or_multiple_files(self):
        response = self.client.post("/qualifications", data={"document": (BytesIO(PDF), "a.pdf")})
        self.assertEqual(response.status_code, 400)
        for data, filename in ((b"", "a.pdf"), (b"<script></script>", "a.pdf"), (PDF, "a.html")):
            self.assertEqual(self.upload(payload=data, filename=filename).status_code, 302)
        response = self.client.post("/qualifications", data={"csrf_token": "test-token", "document": [(BytesIO(PDF), "a.pdf"), (BytesIO(PDF), "b.pdf")]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(QualificationRecord.query.count(), 0)

    def test_upload_accepts_real_images_and_sanitises_filename(self):
        data = BytesIO()
        Image.new("RGB", (8, 8), "blue").save(data, format="PNG")
        self.upload(payload=data.getvalue(), filename="../../my diploma.png")
        document = QualificationDocument.query.one()
        self.assertEqual(document.filename, "my_diploma.png")
        self.assertEqual(document.mime_type, "image/png")
        self.assertEqual(self.upload(payload=data.getvalue(), filename="mismatch.jpg").status_code, 302)
        self.assertEqual(QualificationDocument.query.count(), 1)

    def test_document_size_limit_and_bounded_read(self):
        source = FileStorage(stream=BytesIO(b"x" * (MAX_UPLOAD_BYTES + 100)), filename="big.pdf")
        with self.assertRaises(QualificationValidationError):
            self.validate(source)
        self.assertEqual(source.stream.tell(), MAX_UPLOAD_BYTES + 1)

    def test_private_download_only_owner_or_hr_director(self):
        self.upload()
        evidence = QualificationDocument.query.one()
        path = f"/qualifications/documents/{evidence.id}"
        response = self.client.get(path)
        self.assertEqual(response.data, PDF)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        for role in ("other", "developer"):
            self.login(role)
            self.assertEqual(self.client.get(path).status_code, 403)
        for role in ("hr", "ceo"):
            self.login(role)
            self.assertEqual(self.client.get(path).status_code, 200)
        self.grant("qualifications.manage", "ceo", False)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.login("employee")
        self.grant("add_qualification", "employee", False)
        self.assertEqual(self.client.get(path).status_code, 403)

    def test_employee_cannot_process_even_with_explicit_page_grant(self):
        record = self.seed()
        self.grant("qualifications.manage", "employee")
        for path in ("/qualifications/manage", "/qualifications/manage/new", f"/qualifications/manage/{record.id}/edit"):
            self.assertEqual(self.client.get(path).status_code, 403)
            if path != "/qualifications/manage":
                self.assertEqual(self.client.post(path, data=self.form(record)).status_code, 403)
        self.assertEqual(db.session.get(QualificationRecord, record.id).status, "uploaded")

    def test_manager_processing_and_completed_year_only(self):
        record = self.seed()
        self.login("hr")
        self.assertEqual(self.save(record).status_code, 302)
        db.session.expire_all()
        saved = db.session.get(QualificationRecord, record.id)
        self.assertEqual(saved.status, "processed")
        self.assertEqual(saved.qualification_name, "Preschool qualification")
        self.assertEqual(saved.year_obtained, 2024)
        self.assertIsNone(saved.date_obtained)
        self.assertEqual(saved.processed_by_id, self.users["hr"].id)
        self.assertEqual(saved.revision, 2)

    def test_manager_draft_is_not_processed_and_invalid_process_is_atomic(self):
        record = self.seed()
        self.login("ceo")
        self.assertEqual(self.save(record, action="save_draft", qualification_name="", year_obtained="").status_code, 302)
        db.session.expire_all()
        self.assertEqual(record.status, "uploaded")
        self.assertIsNone(record.qualification_name)
        response = self.save(record, qualification_name="Should not persist", year_obtained="")
        self.assertEqual(response.status_code, 400)
        db.session.expire_all()
        self.assertIsNone(record.qualification_name)
        self.assertEqual(record.status, "uploaded")

    def test_processed_record_cannot_be_downgraded_by_draft_action(self):
        record = self.seed(processed=True)
        self.login("hr")
        self.assertEqual(self.save(record, action="save_draft", qualification_name="Updated").status_code, 302)
        db.session.expire_all()
        self.assertEqual(record.status, "processed")
        self.assertEqual(record.qualification_name, "Updated")
        self.assertEqual(self.save(record, action="save_draft", year_obtained="").status_code, 400)
        db.session.expire_all()
        self.assertEqual(record.year_obtained, 2024)

    def test_manager_metadata_posts_require_csrf(self):
        record = self.seed()
        self.login("hr")
        for path in ("/qualifications/manage/new", f"/qualifications/manage/{record.id}/edit"):
            response = self.client.post(path, data=self.form(record, csrf_token="invalid", user_id=str(self.users["employee"].id)))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(QualificationRecord.query.count(), 1)
        self.assertEqual(record.status, "uploaded")

    def test_manager_can_create_multiple_exams_without_document(self):
        self.login("hr")
        for index in range(2):
            response = self.client.post("/qualifications/manage/new", data=self.form(
                user_id=str(self.users["employee"].id), kind="exam", qualification_name=f"Exam {index}",
            ))
            self.assertEqual(response.status_code, 302)
        records = QualificationRecord.query.all()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(row.kind == "exam" and row.status == "processed" and not row.documents for row in records))

    def test_stale_revision_and_reassignment_cannot_overwrite(self):
        record = self.seed()
        self.login("hr")
        old_revision = record.revision
        self.assertEqual(self.save(record, qualification_name="Latest").status_code, 302)
        db.session.expire_all()
        self.assertEqual(self.save(record, revision=str(old_revision), qualification_name="Stale").status_code, 409)
        db.session.expire_all()
        self.assertEqual(record.qualification_name, "Latest")
        self.assertEqual(self.save(record, user_id=str(self.users["other"].id)).status_code, 400)
        db.session.expire_all()
        self.assertEqual(record.user_id, self.users["employee"].id)

    def test_highest_changes_only_after_processing_and_increments_old_revision(self):
        first = self.seed(processed=True, highest=True)
        second = self.seed()
        self.login("hr")
        self.assertEqual(self.save(second, highest="1", action="save_draft").status_code, 302)
        db.session.expire_all()
        self.assertTrue(first.highest)
        self.assertEqual(self.save(second, highest="1").status_code, 302)
        db.session.expire_all()
        self.assertFalse(first.highest)
        self.assertTrue(second.highest)
        self.assertEqual(first.revision, 2)

    def test_filters_and_own_list_do_not_leak_other_records(self):
        own = self.seed()
        other = self.seed(user="other", processed=True)
        other.qualification_name = "Unrelated private qualification"
        db.session.commit()
        own_html = self.client.get("/qualifications").get_data(as_text=True)
        self.assertNotIn(other.qualification_name, own_html)
        self.login("hr")
        default_html = self.client.get("/qualifications/manage").get_data(as_text=True)
        self.assertNotIn(other.qualification_name, default_html)
        processed = self.client.get("/qualifications/manage?status=processed").get_data(as_text=True)
        self.assertIn(other.qualification_name, processed)
        filtered = self.client.get("/qualifications/manage?status=all&q=Alpha").get_data(as_text=True)
        self.assertNotIn(other.qualification_name, filtered)
        self.assertEqual(self.client.get("/qualifications/manage?status=bogus").status_code, 400)
        self.assertEqual(self.client.get("/qualifications/manage?user_id=bogus").status_code, 400)

    def test_validation_exact_date_implies_year_and_classification_parent_categories(self):
        values, errors = self.parse(MultiDict(self.form(
            year_obtained="", date_obtained="2024-09-01", award_categories=["ecdl", "leadership_college"],
        )), today=date(2026, 1, 1))
        self.assertFalse(errors)
        self.assertEqual(values["year_obtained"], 2024)
        self.assertEqual(set(values["award_categories"]), {"ecdl", "it", "leadership_college", "leadership"})

    def test_validation_rejects_invalid_unknown_duplicate_and_future_values(self):
        invalid = [
            {"date_obtained": "2027-01-01"}, {"date_obtained": "2024-02-30"},
            {"year_obtained": "2020", "study_start_date": "2024-01-01", "study_end_date": "2024-06-01"},
            {"date_obtained": "2025-01-01", "year_obtained": "2024"},
            {"kind": "invented"}, {"study_categories": ["invented"]},
            {"award_categories": ["invented"]}, {"duration_hours": "NaN"},
            {"credits": "-1"}, {"duration_hours": "0"}, {"credits": "0.123"},
            {"year_obtained": "１８９９"}, {"qualification_name": "x" * 256},
            {"highest": "false"}, {"qualification_name": ["one", "two"]},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                _, errors = self.parse(MultiDict(self.form(**overrides)), today=date(2026, 1, 1))
                self.assertTrue(errors)

    def test_ongoing_studies_require_period_and_cannot_be_counted_as_awards(self):
        base = self.form(completion_state="in_progress", year_obtained="", study_start_date="2024-09-01", study_categories=["bachelor"])
        values, errors = self.parse(MultiDict(base))
        self.assertFalse(errors)
        self.assertIsNone(values["year_obtained"])
        for extra in ({"year_obtained": "2025"}, {"study_start_date": ""}, {"award_categories": ["bachelor"]}, {"study_end_date": "2025-01-01"}):
            _, errors = self.parse(MultiDict({**base, **extra}))
            self.assertTrue(errors)

    def test_arbitrary_hours_and_credits_and_completed_study_period(self):
        values, errors = self.parse(MultiDict(self.form(
            study_start_date="2023-09-01", study_end_date="2024-05-01", study_categories=["master"],
            duration_hours="18,5", credits="2.25", funding_type="partial", attendance_mode="blended",
        )))
        self.assertFalse(errors)
        self.assertEqual(str(values["duration_hours"]), "18.5")
        self.assertEqual(str(values["credits"]), "2.25")


if __name__ == "__main__":
    unittest.main()
