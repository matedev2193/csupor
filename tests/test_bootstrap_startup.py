"""A new host can serve CSUPOR without first importing the SQL installation file."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import inspect

from app import create_app, db
from app.mail_settings import get_mail_settings, is_mail_enabled
from app.mail_settings_models import MailServerSettings
from app.models import LeaveApprovalPolicy, LeaveApprovalSettings, User, UserPrivilege
from app.notification_models import LeaveNotification, MailBatch
from app.page_access_models import PageAccessSettings, PageRolePermission


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLATION_TABLES = set(re.findall(
    r"^CREATE TABLE IF NOT EXISTS ([a-z_]+)\s*\(",
    (REPOSITORY_ROOT / "sql" / "schema.sql").read_text(),
    flags=re.MULTILINE,
))


class BootstrapStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "brand-new-host.db"
        self.environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(self.database_path),
            "SECRET_KEY": "bootstrap-integration-test-key",
            "EMAIL_WORKER_ENABLED": "false",
            "EMAIL_SECRET_KEY": "",
        })
        self.environment.start()
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        self.environment.stop()
        self.directory.cleanup()

    def start_app(self):
        app = create_app()
        app.config.update(TESTING=True)
        self.apps.append(app)
        return app

    def seed_configuration(self, app):
        with app.app_context():
            user = User(
                username="existing-developer", email="existing@example.invalid",
                privilege=UserPrivilege.developer, password_hash="preserved-password-hash",
            )
            db.session.add(user)
            db.session.flush()
            approval = db.session.get(LeaveApprovalSettings, 1)
            approval.policy = LeaveApprovalPolicy.either
            approval.updated_by_id = user.id
            page_access = db.session.get(PageAccessSettings, 1)
            page_access.updated_by_id = user.id
            db.session.add_all([
                PageRolePermission(page_key="dashboard", role="employee", allowed=False),
                PageRolePermission(page_key="worktime.groups", role="employee", allowed=True),
                MailServerSettings(
                    id=1, enabled=False, host="saved.smtp.example.invalid", port=465,
                    security="ssl", sender_name="Saved nursery", updated_by_id=user.id,
                ),
            ])
            db.session.commit()
            return self.saved_rows()

    def saved_rows(self):
        return {
            model.__tablename__: [dict(row) for row in db.session.execute(
                model.__table__.select().order_by(*model.__table__.primary_key.columns)
            ).mappings()]
            for model in (
                User, LeaveApprovalSettings, PageAccessSettings,
                PageRolePermission, MailServerSettings,
            )
        }

    def test_fresh_process_registers_and_creates_every_installation_table(self):
        # A separate interpreter proves startup does not depend on model imports
        # performed by another test module in the test runner.
        self.assertFalse(self.database_path.exists())
        code = """
import json
from sqlalchemy import inspect
from app import create_app, db
app = create_app()
with app.app_context():
    print(json.dumps(inspect(db.engine).get_table_names()))
    db.session.remove()
    db.engine.dispose()
"""
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=REPOSITORY_ROOT,
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.database_path.is_file())
        created_tables = set(json.loads(result.stdout))
        self.assertEqual(len(INSTALLATION_TABLES), 29)
        self.assertEqual(created_tables, INSTALLATION_TABLES)

    def test_first_startup_creates_constraints_foreign_keys_and_dispatch_indexes(self):
        app = self.start_app()
        with app.app_context():
            schema = inspect(db.engine)
            self.assertEqual(set(schema.get_table_names()), INSTALLATION_TABLES)
            access_fk = schema.get_foreign_keys("page_access_settings")
            self.assertEqual(len(access_fk), 1)
            self.assertEqual(access_fk[0]["constrained_columns"], ["updated_by_id"])
            self.assertEqual(access_fk[0]["referred_table"], "users")
            self.assertEqual(access_fk[0]["referred_columns"], ["id"])
            self.assertEqual(access_fk[0]["options"]["ondelete"], "SET NULL")
            self.assertEqual(
                schema.get_pk_constraint("page_role_permissions")["constrained_columns"],
                ["page_key", "role"],
            )
            for table, expected_name in (
                ("page_access_settings", "single_page_access_settings"),
                ("mail_server_settings", "single_mail_server_settings"),
                ("leave_approval_settings", "single_leave_approval_settings"),
                ("work_assignments", "work_assignment_phase"),
            ):
                self.assertIn(expected_name, {
                    constraint["name"] for constraint in schema.get_check_constraints(table)
                })
            for table, expected_index in (
                ("mail_batches", "ix_mail_batches_dispatch"),
                ("leave_notifications", "ix_leave_notifications_dispatch"),
            ):
                self.assertIn(expected_index, {
                    index["name"] for index in schema.get_indexes(table)
                })
            for table in ("educational_qualifications", "professional_exams"):
                self.assertIn("date_obtained", {
                    column["name"] for column in schema.get_columns(table)
                })
            self.assertIn("flexible_shift", {
                column["name"] for column in schema.get_columns("work_assignments")
            })

    def test_defaults_are_ready_without_creating_users_or_enabling_email(self):
        app = self.start_app()
        with app.app_context():
            self.assertEqual(LeaveApprovalSettings.query.count(), 1)
            self.assertEqual(db.session.get(LeaveApprovalSettings, 1).policy, LeaveApprovalPolicy.both)
            self.assertEqual(PageAccessSettings.query.count(), 1)
            self.assertEqual(len(db.session.get(PageAccessSettings, 1).revision), 32)
            self.assertEqual(PageRolePermission.query.count(), 0)
            self.assertEqual(User.query.count(), 0)
            self.assertEqual(MailServerSettings.query.count(), 0)
            self.assertFalse(get_mail_settings().enabled)
            self.assertFalse(is_mail_enabled())
            self.assertEqual(LeaveNotification.query.count(), 0)
            self.assertEqual(MailBatch.query.count(), 0)

    def test_first_requests_render_and_registration_works_without_manual_sql(self):
        app = self.start_app()
        client = app.test_client()
        for path in ("/", "/login", "/register"):
            with self.subTest(path=path):
                response = client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"<form", response.data)
        response = client.post("/register", data={
            "username": "first-user", "email": "first@example.invalid",
            "password": "first-registration-test-password",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            user = User.query.one()
            self.assertEqual(user.username, "first-user")
            self.assertEqual(user.privilege, UserPrivilege.employee)
            self.assertTrue(user.check_password("first-registration-test-password"))
            self.assertIsNotNone(user.profile)

    def test_repeated_startup_preserves_users_settings_and_explicit_permissions(self):
        before = self.seed_configuration(self.start_app())
        for _ in range(2):
            app = self.start_app()
            with app.app_context():
                self.assertEqual(self.saved_rows(), before)
                self.assertEqual(set(inspect(db.engine).get_table_names()), INSTALLATION_TABLES)

    def test_restart_recreates_a_missing_table_without_resetting_existing_records(self):
        first_app = self.start_app()
        before = self.seed_configuration(first_app)
        with first_app.app_context():
            # This table has no incoming references and no saved records.
            db.metadata.tables["gyap_forms"].drop(db.engine)
            self.assertNotIn("gyap_forms", inspect(db.engine).get_table_names())
        restarted = self.start_app()
        with restarted.app_context():
            self.assertEqual(set(inspect(db.engine).get_table_names()), INSTALLATION_TABLES)
            self.assertEqual(self.saved_rows(), before)

    def test_notification_worker_is_initialised_only_after_schema_and_defaults_exist(self):
        observed = []

        def inspect_before_worker(app):
            with app.app_context():
                observed.append(set(inspect(db.engine).get_table_names()))
                self.assertEqual(LeaveApprovalSettings.query.count(), 1)
                self.assertEqual(PageAccessSettings.query.count(), 1)
                self.assertEqual(LeaveNotification.query.count(), 0)
                self.assertEqual(MailBatch.query.count(), 0)

        with patch("app.notification_delivery.init_notifications", side_effect=inspect_before_worker) as init:
            app = self.start_app()
        init.assert_called_once_with(app)
        self.assertEqual(observed, [INSTALLATION_TABLES])


if __name__ == "__main__":
    unittest.main()
