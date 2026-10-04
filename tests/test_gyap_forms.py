"""Annual GYAP upload/download permissions, validation and durable storage."""

import os
import tempfile
import unittest
from datetime import date
from io import BytesIO
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from flask import g, template_rendered
from flask_login import login_user
from sqlalchemy import event, inspect

from app import create_app, db
from app.gyap_forms import MAX_UPLOAD_BYTES, inject_gyap_forms
from app.models import (
    Contract, ContractType, Dependent, DependentType, GyapForm, LegalEntity,
    LeaveRequest, LeaveRequestCategory, LeaveYear, PlaceOfWork, User, UserPrivilege,
)


PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def docx_bytes(document=None, content_types=None):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types or (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'
        ))
        package.writestr("word/document.xml", document or (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>'
        ))
    return output.getvalue()


def corrupt_docx_bytes():
    data = bytearray(docx_bytes())
    with ZipFile(BytesIO(data)) as package:
        info = package.getinfo("word/document.xml")
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    # Reserved DEFLATE block type: ZipFile.read raises zlib.error.
    data[offset] = (data[offset] & 0xF8) | 0x07
    return bytes(data)


class GyapFormTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_url = "sqlite:///" + os.path.join(self.directory.name, "forms.db")
        with patch.dict(os.environ, {"DATABASE_URL": self.database_url, "SECRET_KEY": "gyap-test-only"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for privilege in UserPrivilege:
            user = User(username=privilege.value, email=f"{privilege.value}@example.invalid", password_hash="unused", privilege=privilege)
            db.session.add(user)
            self.users[privilege.value] = user
        entity = LegalEntity(name="Example", address="Example", om_id="000001", tax_number="00000000001")
        place = PlaceOfWork(legal_entity=entity, address="Example")
        self.contract = Contract(
            user=self.users["employee"], employer=entity, place_of_work=place,
            contract_type=ContractType.teacher, start_date=date(2020, 1, 1),
            job_title="Teacher", working_hours_per_week=40,
        )
        db.session.add(self.contract)
        db.session.add(Dependent(
            user=self.users["employee"], name="Child", dependent_type=DependentType.child,
            date_of_birth=date(2020, 1, 1), social_security_number="123456789", dependency_start=date(2020, 1, 1),
        ))
        db.session.add(LeaveYear(year=2026, is_open=True))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.directory.cleanup()

    def login(self, name):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.users[name].id)
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)

    def token(self):
        self.assertEqual(self.client.get("/gyap-forms").status_code, 200)
        with self.client.session_transaction() as session:
            return session["gyap_forms_csrf_token"]

    def upload(self, year=2026, filename="gyap.pdf", data=PDF, csrf=None):
        response = self.client.post("/gyap-forms", data={
            "calendar_year": str(year), "csrf_token": self.token() if csrf is None else csrf,
            "document": (BytesIO(data), filename, "text/html"),
        })
        self.addCleanup(response.request.environ["wsgi.input"].close)
        return response

    def test_upload_management_is_hr_and_ceo_only(self):
        for name in self.users:
            with self.subTest(role=name):
                self.login(name)
                allowed = name in {"hr", "ceo"}
                self.assertEqual(self.client.get("/gyap-forms").status_code, 200 if allowed else 403)
                if allowed:
                    self.assertEqual(self.upload().status_code, 302)
                    self.assertEqual(db.session.get(GyapForm, 2026).uploaded_by_id, self.users[name].id)
                else:
                    self.assertEqual(self.upload(csrf="invalid").status_code, 403)
        self.client.get("/logout")
        g.pop("_login_user", None)
        self.assertEqual(self.client.get("/gyap-forms").status_code, 302)
        self.assertEqual(self.upload(csrf="invalid").status_code, 302)
        self.assertEqual(self.client.get("/gyap-forms/2026/download").status_code, 302)

    def test_exact_safe_download_for_all_authenticated_roles(self):
        self.login("hr")
        response = self.upload(filename="../../gyermekapolas.pdf")
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        form = db.session.get(GyapForm, 2026)
        self.assertEqual(form.filename, "gyermekapolas.pdf")
        self.assertEqual(form.size_bytes, len(PDF))
        self.assertEqual(form.mime_type, "application/pdf")
        self.assertIsNotNone(form.uploaded_at)
        for name in self.users:
            self.login(name)
            download = self.client.get("/gyap-forms/2026/download")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.data, PDF)
            self.assertEqual(download.mimetype, "application/pdf")
            self.assertIn('attachment; filename=gyermekapolas.pdf', download.headers["Content-Disposition"])
            self.assertEqual(download.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(download.headers["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.get("/gyap-forms/2027/download").status_code, 404)

    def test_upload_all_formats_and_replace_only_matching_year(self):
        self.login("hr")
        self.upload()
        docx = docx_bytes()
        self.assertEqual(self.upload(2027, "new.docx", docx).status_code, 302)
        self.assertEqual(self.client.get("/gyap-forms/2027/download").data, docx)
        # Annual forms are independent of which leave years HR has opened.
        self.assertIsNone(db.session.get(LeaveYear, 2027))
        doc = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 512 + "WordDocument".encode("utf-16-le")
        self.login("ceo")
        self.assertEqual(self.upload(2026, "replacement.DOC", doc).status_code, 302)
        db.session.expire_all()
        self.assertEqual(GyapForm.query.count(), 2)
        form = db.session.get(GyapForm, 2026)
        self.assertEqual(form.data, doc)
        self.assertEqual(form.mime_type, "application/msword")
        self.assertEqual(form.uploaded_by_id, self.users["ceo"].id)
        self.assertEqual(self.client.get("/gyap-forms/2027/download").data, docx)

    def test_invalid_uploads_leave_previous_form_unchanged(self):
        self.login("hr")
        self.upload()
        invalid_cases = [
            (2026, "", PDF), (2026, "file.exe", PDF), (2026, "file.pdf", b""),
            (2026, "file.pdf", b"<script>bad()</script>"), (2026, "file.pdf", b"%PDF-1.4 truncated"),
            (2026, "file.doc", PDF), (2026, "file.docx", PDF),
            (2026, "file.docx", docx_bytes(document="<notword/>")),
            (2026, "file.docx", docx_bytes(content_types="<Types/>")),
            (2026, "file.docx", docx_bytes(document="<broken")),
            (2026, "file.docx", corrupt_docx_bytes()),
            (2026, "file.docx", docx_bytes(document="x" * (2 * 1024 * 1024 + 1))),
            (1969, "file.pdf", PDF), (2101, "file.pdf", PDF), ("2026.0", "file.pdf", PDF),
            ("x", "file.pdf", PDF), (2026, "file.pdf", PDF + b"x" * MAX_UPLOAD_BYTES),
        ]
        for year, filename, payload in invalid_cases:
            with self.subTest(year=year, filename=filename, size=len(payload)):
                self.assertEqual(self.upload(year, filename, payload).status_code, 302)
                db.session.expire_all()
                self.assertEqual(db.session.get(GyapForm, 2026).data, PDF)
                self.assertEqual(GyapForm.query.count(), 1)
        response = self.client.post("/gyap-forms", data={"calendar_year": "2026", "csrf_token": self.token()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/gyap-forms/2026/download").data, PDF)

    def test_missing_or_invalid_csrf_cannot_replace_form(self):
        self.login("hr")
        self.upload()
        for token in ("", "invalid", "árvíz"):
            self.assertEqual(self.upload(data=PDF.replace(b"1.4", b"1.7"), csrf=token).status_code, 400)
        self.assertEqual(self.client.get("/gyap-forms/2026/download").data, PDF)

    def test_upload_size_limit_includes_exact_boundary(self):
        self.login("hr")
        exact = b"%PDF-1.4\n" + b" " * (MAX_UPLOAD_BYTES - 15) + b"%%EOF\n"
        self.assertEqual(len(exact), MAX_UPLOAD_BYTES)
        self.assertEqual(self.upload(data=exact).status_code, 302)
        self.assertEqual(self.client.get("/gyap-forms/2026/download").data, exact)
        # Covers the early multipart-size guard as well as stream read limiting.
        self.assertEqual(self.upload(data=exact + b" " * (128 * 1024)).status_code, 302)
        self.assertEqual(self.client.get("/gyap-forms/2026/download").data, exact)

    def test_leave_metadata_does_not_load_document_bytes_and_covers_all_years(self):
        self.login("hr")
        self.upload(2026)
        self.upload(2028)
        db.session.expire_all()
        self.assertIn("data", inspect(GyapForm.query.first()).unloaded)
        statements = []

        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", collect)
        try:
            with self.app.test_request_context("/leaves"):
                login_user(self.users["employee"])
                context = inject_gyap_forms()
                self.assertEqual(set(context["gyap_forms_by_year"]), {2026, 2028})
                rows = context["gyap_forms_for_period"](date(2026, 12, 31), date(2028, 1, 1))
                self.assertEqual([row["year"] for row in rows], [2026, 2027, 2028])
                self.assertEqual(rows[0]["url"], "/gyap-forms/2026/download")
                self.assertIsNone(rows[1]["url"])
                self.assertEqual(rows[2]["url"], "/gyap-forms/2028/download")
                self.assertEqual(len(context["gyap_forms_for_period"](date(2026, 1, 1))), 1)
                self.assertEqual(context["gyap_forms_for_period"](None), [])
        finally:
            event.remove(db.engine, "before_cursor_execute", collect)
        self.assertTrue(any("gyap_forms" in sql for sql in statements))
        self.assertFalse(any("gyap_forms.data" in sql for sql in statements))

    def test_employee_leave_page_and_saved_cross_year_request_have_annual_links(self):
        self.login("hr")
        self.upload(2026)
        self.upload(2027)
        db.session.add(LeaveRequest(
            user=self.users["employee"], contract=self.contract,
            category=LeaveRequestCategory.childcare_sickness_benefit,
            start_date=date(2026, 12, 31), end_date=date(2027, 1, 2),
        ))
        db.session.commit()
        self.login("employee")
        response = self.client.get("/leaves?year=2026&month=12")
        self.assertEqual(response.status_code, 200)
        # Isolate the saved request, excluding JSON metadata and the new form.
        request_card = response.data.split(b'<article class="request-item">', 1)[1].split(b"</article>", 1)[0]
        self.assertIn(b"gyap-request-downloads", request_card)
        self.assertIn(b'href="/gyap-forms/2026/download"', request_card)
        self.assertIn(b'href="/gyap-forms/2027/download"', request_card)

    def test_management_lists_metadata_newest_first_without_fetching_binary(self):
        self.login("hr")
        self.upload(2026)
        self.upload(2027)
        db.session.expire_all()
        contexts = []
        statements = []

        def capture(sender, template, context, **extra):
            contexts.append(context)

        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        template_rendered.connect(capture, self.app)
        event.listen(db.engine, "before_cursor_execute", collect)
        try:
            self.assertEqual(self.client.get("/gyap-forms?year=2028").status_code, 200)
        finally:
            template_rendered.disconnect(capture, self.app)
            event.remove(db.engine, "before_cursor_execute", collect)
        self.assertEqual([form.year for form in contexts[-1]["forms"]], [2027, 2026])
        self.assertEqual(contexts[-1]["selected_year"], 2028)
        self.assertFalse(any("gyap_forms.data" in sql for sql in statements))

    def test_form_survives_application_restart(self):
        self.login("hr")
        self.upload(2027)
        employee_id = self.users["employee"].id
        with patch.dict(os.environ, {"DATABASE_URL": self.database_url, "SECRET_KEY": "gyap-test-only"}):
            restarted = create_app()
        restarted.config.update(TESTING=True)
        try:
            with restarted.app_context():
                new_client = restarted.test_client()
                with new_client.session_transaction() as session:
                    session["_user_id"] = str(employee_id)
                    session["_fresh"] = True
                response = new_client.get("/gyap-forms/2027/download")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, PDF)
                self.assertEqual(db.session.get(GyapForm, 2027).filename, "gyap.pdf")
                db.session.remove()
                db.engine.dispose()
        finally:
            g.pop("_login_user", None)


if __name__ == "__main__":
    unittest.main()
