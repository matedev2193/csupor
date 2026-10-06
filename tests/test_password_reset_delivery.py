"""Private recovery diagnostics preserve secrets and caller transactions."""

from datetime import datetime, timedelta
import os
import tempfile
import unittest
from unittest.mock import patch

from flask import g
from sqlalchemy.exc import OperationalError

from app import create_app, db
from app.models import User, UserPrivilege
from app.password_reset_delivery import (
    record_reset_delivery, recent_reset_deliveries, update_reset_delivery,
)
from app.password_reset_delivery_models import PasswordResetDelivery


class PasswordResetDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + os.path.join(self.directory.name, "delivery.db"),
            "SECRET_KEY": "diagnostic-test-secret", "EMAIL_WORKER_ENABLED": "false",
            "EMAIL_SECRET_KEY": "",
        })
        self.environment.start()
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for role in (UserPrivilege.developer, UserPrivilege.employee):
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

    def login(self, role):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.users[role].get_id()
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)

    def test_records_then_updates_only_fixed_metadata(self):
        delivery_id = record_reset_delivery("Person@Example.invalid")
        self.assertEqual(len(delivery_id), 32)
        self.assertTrue(update_reset_delivery(delivery_id, "failed", "authentication", attempts=1))
        row = recent_reset_deliveries()[0]
        self.assertEqual(row.recipient_email, "person@example.invalid")
        self.assertEqual((row.status, row.safe_error_code, row.attempts), ("failed", "authentication", 1))
        self.assertTrue(update_reset_delivery(delivery_id, "sent", attempts=2))
        row = recent_reset_deliveries()[0]
        self.assertEqual((row.status, row.safe_error_code, row.attempts), ("sent", None, 2))
        self.assertEqual(set(row.__table__.columns.keys()), {
            "id", "created_at", "updated_at", "recipient_email", "status", "safe_error_code", "attempts",
        })

    def test_rejects_invalid_addresses_states_and_attempts(self):
        for address in ("bad", "Person <person@example.invalid>", "person@example.invalid\r\nBcc:x@example.invalid", "x" * 121, None):
            self.assertIsNone(record_reset_delivery(address))
        self.assertIsNone(record_reset_delivery("person@example.invalid", "raw SMTP response"))
        delivery_id = record_reset_delivery("person@example.invalid")
        for attempts in (-1, 101, "1", True):
            self.assertFalse(update_reset_delivery(delivery_id, "failed", attempts=attempts))
        self.assertFalse(update_reset_delivery(None, "sent"))
        self.assertFalse(update_reset_delivery("missing", "sent"))
        self.assertFalse(update_reset_delivery(delivery_id, "invalid"))
        self.assertEqual(recent_reset_deliveries()[0].status, "queued")

    def test_never_retains_unrecognised_error_text(self):
        secret = "smtp-password-and-token-must-not-be-retained"
        delivery_id = record_reset_delivery("person@example.invalid", "failed", secret)
        self.assertEqual(recent_reset_deliveries()[0].safe_error_code, "internal")
        self.assertTrue(update_reset_delivery(delivery_id, "failed", {"smtp": secret}))
        self.assertEqual(recent_reset_deliveries()[0].safe_error_code, "internal")
        self.login("developer")
        response = self.client.get("/settings")
        self.assertNotIn(secret.encode(), response.data)
        self.assertIn(b"An unexpected error prevented email delivery.", response.data)

    def test_helper_transactions_do_not_commit_caller_changes(self):
        self.users["employee"].username = "unsaved-change"
        delivery_id = record_reset_delivery("person@example.invalid")
        self.assertTrue(update_reset_delivery(delivery_id, "sent", attempts=1))
        db.session.rollback()
        self.assertEqual(self.users["employee"].username, "employee")
        self.assertEqual(recent_reset_deliveries()[0].status, "sent")

    def test_database_failure_never_leaks_provider_details_or_rolls_back_caller(self):
        self.users["employee"].username = "pending-change"
        error = OperationalError("SELECT token-secret", {"email": "private@example.invalid"}, Exception("provider-secret"))
        with patch("app.password_reset_delivery.Session", side_effect=error):
            with self.assertLogs(self.app.logger, level="WARNING") as captured:
                self.assertIsNone(record_reset_delivery("person@example.invalid"))
                self.assertFalse(update_reset_delivery("a" * 32, "failed"))
                self.assertEqual(recent_reset_deliveries(), [])
        self.assertEqual(self.users["employee"].username, "pending-change")
        self.assertIn(self.users["employee"], db.session.dirty)
        logs = " ".join(captured.output)
        self.assertNotIn("token-secret", logs)
        self.assertNotIn("private@example.invalid", logs)
        self.assertNotIn("provider-secret", logs)

    def test_retention_and_bounded_history(self):
        now = datetime(2026, 10, 6, 21, 0)
        with patch("app.password_reset_delivery._now", return_value=now - timedelta(days=8)):
            record_reset_delivery("old@example.invalid")
        with patch("app.password_reset_delivery._now", return_value=now):
            self.assertEqual(recent_reset_deliveries(), [])
            with patch("app.password_reset_delivery.MAX_RECORDS", 3):
                for number in range(5):
                    with patch("app.password_reset_delivery._now", return_value=now + timedelta(seconds=number)):
                        record_reset_delivery(f"person{number}@example.invalid")
            self.assertEqual([row.recipient_email for row in recent_reset_deliveries()], [
                "person4@example.invalid", "person3@example.invalid", "person2@example.invalid",
            ])
            self.assertEqual(len(recent_reset_deliveries(limit=2)), 2)
        self.assertEqual(PasswordResetDelivery.query.count(), 3)

    def test_diagnostics_only_appear_for_authorised_settings_viewers(self):
        record_reset_delivery("private@example.invalid", "not_found", "account_missing")
        self.assertEqual(self.client.get("/settings").status_code, 302)
        self.login("employee")
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b"private@example.invalid", response.data)
        self.login("developer")
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(b"private@example.invalid", response.data)
        self.assertIn(b"No account has this email address.", response.data)
        self.assertIn(b"does not confirm arrival in the inbox", response.data)

    def test_settings_survives_unavailable_diagnostics(self):
        self.login("developer")
        with patch("app.password_reset_delivery.Session", side_effect=RuntimeError("private error")):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"private error", response.data)

    def test_local_timestamp_uses_budapest_summer_and_winter_offsets(self):
        row = PasswordResetDelivery(created_at=datetime(2026, 10, 6, 20, 0))
        self.assertEqual(row.local_created_at.isoformat(), "2026-10-06T22:00:00+02:00")
        row.created_at = datetime(2026, 12, 6, 20, 0)
        self.assertEqual(row.local_created_at.isoformat(), "2026-12-06T21:00:00+01:00")


if __name__ == "__main__":
    unittest.main()
