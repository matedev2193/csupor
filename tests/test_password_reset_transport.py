"""Exercise recovery through the real MIME, encryption and background code."""

import importlib
import os
from pathlib import Path
import re
import smtplib
import tempfile
import threading
import unittest
from hashlib import sha256
from unittest.mock import patch

from app import create_app, db
from app.mail_settings import _cipher
from app.mail_settings_models import MailServerSettings
from app.models import User
from app.password_reset_models import PasswordResetToken
from app.password_reset_delivery import record_reset_delivery, recent_reset_deliveries


recovery = importlib.import_module("app.password_reset")
transport = importlib.import_module("app.mail_settings")


class PasswordResetTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(Path(self.directory.name) / "transport.db"),
            "SECRET_KEY": "isolated-reset-transport-secret",
            "EMAIL_SECRET_KEY": "isolated-smtp-encryption-secret",
            "EMAIL_WORKER_ENABLED": "false",
        })
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_EMAIL", "INITIAL_ADMIN_PASSWORD"):
            os.environ.pop(key, None)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.addCleanup(self.close_database)
        with self.app.app_context():
            user = User(username="recover", email="recover@example.invalid")
            user.set_password("Original-password-123")
            db.session.add(user)
            encrypted = _cipher().encrypt(b"private-smtp-password").decode("ascii")
            db.session.add(MailServerSettings(
                id=1, enabled=True, host="smtp.example.invalid", port=587,
                security="starttls", username="smtp-account",
                encrypted_password=encrypted, sender_email="csupor@example.invalid",
                sender_name="CSUPOR", base_url="https://portal.example.invalid",
            ))
            db.session.commit()
        # Only network connections are replaced: all encryption, MIME rendering,
        # locale selection, database work and delivery dispatch remain genuine.
        connection = patch.object(transport.smtplib, "SMTP")
        self.connection = connection.start()
        self.addCleanup(connection.stop)
        self.smtp = self.connection.return_value.__enter__.return_value
        ssl_connection = patch.object(transport.smtplib, "SMTP_SSL")
        self.ssl_connection = ssl_connection.start()
        self.addCleanup(ssl_connection.stop)

    def close_database(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def assert_delivered(self, locale):
        self.connection.assert_called_once_with("smtp.example.invalid", 587, timeout=transport.SMTP_TIMEOUT)
        self.ssl_connection.assert_not_called()
        self.assertEqual(self.smtp.ehlo.call_count, 2)
        self.smtp.starttls.assert_called_once()
        self.smtp.login.assert_called_once_with("smtp-account", "private-smtp-password")
        self.smtp.send_message.assert_called_once()
        message = self.smtp.send_message.call_args.args[0]
        self.assertEqual(self.smtp.send_message.call_args.kwargs, {
            "from_addr": "csupor@example.invalid", "to_addrs": ["recover@example.invalid"],
        })
        self.assertEqual(str(message["To"]), "recover@example.invalid")
        self.assertEqual(str(message["Subject"]), {
            "en": "Reset your CSUPOR password", "hu": "CSUPOR-jelszó visszaállítása",
        }[locale])
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        text = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        match = re.search(r"https://portal\.example\.invalid/reset-password#([A-Za-z0-9_-]{43})", text)
        self.assertIsNotNone(match)
        self.assertIn(match.group(0), html)
        self.assertIn("30", text)
        self.assertNotIn("Original-password-123", text + html)
        self.assertNotIn("private-smtp-password", text + html)
        self.assertNotIn("attacker.invalid", text + html)
        # Serialise as the SMTP implementation would; malformed MIME headers or
        # non-ASCII Hungarian content must not be hidden by a mocked sender.
        self.assertIn(b"Content-Type: multipart/alternative", message.as_bytes())
        with self.app.app_context():
            saved = PasswordResetToken.query.one()
            self.assertEqual(saved.token_hash, sha256(match.group(1).encode("ascii")).hexdigest())

    def test_english_delivery_with_real_encryption_and_mime(self):
        recovery._deliver_reset(self.app, "recover@example.invalid", "en")
        self.assert_delivered("en")

    def test_hungarian_delivery_without_request_context(self):
        recovery._deliver_reset(self.app, "recover@example.invalid", "hu")
        self.assert_delivered("hu")

    def request_in_background(self, locale):
        self.client.get("/forgot-password")
        with self.client.session_transaction() as session:
            csrf = session["password_reset_csrf"]
            session["locale"] = locale
        done = threading.Event()
        failures, delivery_threads = [], []
        original = recovery._deliver_reset

        def observe_delivery(*args, **kwargs):
            delivery_threads.append(threading.get_ident())
            try:
                return original(*args, **kwargs)
            except Exception as error:
                failures.append(error)
                raise
            finally:
                done.set()

        # The wrapper observes completion, including failures, without replacing
        # dispatch, the actual delivery function or its background executor.
        with patch.object(recovery, "_deliver_reset", side_effect=observe_delivery):
            response = self.client.post("/forgot-password", data={
                "csrf_token": csrf, "email": "  ReCover@Example.Invalid  ",
            }, headers={"X-Forwarded-Host": "attacker.invalid"})
            self.assertTrue(done.wait(timeout=10), "The recovery worker did not finish.")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(failures, [])
        self.assertEqual(len(delivery_threads), 1)
        self.assertNotEqual(delivery_threads[0], threading.get_ident())
        self.assert_delivered(locale)
        with self.app.app_context():
            rows = recent_reset_deliveries()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].status, rows[0].attempts), ("sent", 1))

    def test_english_public_request_reaches_smtp_from_real_worker(self):
        self.request_in_background("en")

    def test_hungarian_public_request_reaches_smtp_from_real_worker(self):
        self.request_in_background("hu")

    def test_real_smtp_authentication_failure_is_sanitised_and_removes_token(self):
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(
            535, b"private-smtp-password recover@example.invalid server-details",
        )
        with self.app.app_context():
            delivery_id = record_reset_delivery("recover@example.invalid")
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            recovery._deliver_reset(self.app, "recover@example.invalid", "hu", delivery_id)
        self.smtp.send_message.assert_not_called()
        recorded = " ".join(logs.output)
        for private in ("private-smtp-password", "recover@example.invalid", "server-details"):
            self.assertNotIn(private, recorded)
        with self.app.app_context():
            self.assertEqual(PasswordResetToken.query.count(), 0)
            row = recent_reset_deliveries()[0]
            self.assertEqual((row.status, row.safe_error_code, row.attempts), ("failed", "authentication", 1))

    def test_unexpected_rendering_failure_is_logged_without_private_details(self):
        with patch.object(self.app.jinja_env, "get_template", side_effect=RuntimeError(
            "private-smtp-password recover@example.invalid template-details",
        )):
            with self.assertLogs(self.app.logger, level="ERROR") as logs:
                recovery._deliver_reset(self.app, "recover@example.invalid", "hu")
        self.connection.assert_not_called()
        recorded = " ".join(logs.output)
        for private in ("private-smtp-password", "recover@example.invalid", "template-details"):
            self.assertNotIn(private, recorded)
        with self.app.app_context():
            self.assertEqual(PasswordResetToken.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
