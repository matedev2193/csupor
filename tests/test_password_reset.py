"""Isolated recovery lifecycle, delivery and shared abuse limits."""

import importlib
import os
from pathlib import Path
import re
import tempfile
import unittest
from datetime import timedelta
from hashlib import sha256
from unittest.mock import patch

from sqlalchemy import event, inspect
from werkzeug.security import generate_password_hash

from app import create_app, db
from app.mail_settings import MailDeliveryError
from app.mail_settings_models import MailServerSettings
from app.models import User
from app.password_reset_models import PasswordResetToken, PasswordResetThrottle

recovery = importlib.import_module("app.password_reset")


class PasswordResetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(Path(self.directory.name) / "recovery.db"),
            "SECRET_KEY": "isolated-reset-test-secret", "EMAIL_WORKER_ENABLED": "false",
        })
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_EMAIL", "INITIAL_ADMIN_PASSWORD"):
            os.environ.pop(key, None)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        with self.app.app_context():
            user = User(username="recover", email="recover@example.invalid")
            user.set_password("Original-password-123")
            db.session.add(user)
            db.session.add(MailServerSettings(
                id=1, enabled=True, host="smtp.example.invalid", port=587,
                sender_email="csupor@example.invalid", base_url="https://portal.example.invalid",
            ))
            db.session.commit()
            self.user_id = user.id
        self.sender = patch.object(recovery, "send_email")
        self.send = self.sender.start()
        self.addCleanup(self.sender.stop)
        # HTTP tests never start threads or contact an external mail server.
        dispatch = patch.object(recovery, "_dispatch_reset")
        self.dispatch = dispatch.start()
        self.addCleanup(dispatch.stop)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def csrf(self, client=None):
        client = client or self.client
        self.assertEqual(client.get("/forgot-password").status_code, 200)
        with client.session_transaction() as session:
            return session["password_reset_csrf"]

    def request_link(self, email="recover@example.invalid", client=None, **kwargs):
        client = client or self.client
        return client.post("/forgot-password", data={"email": email, "csrf_token": self.csrf(client)}, **kwargs)

    def delivered_token(self, email="recover@example.invalid", locale="en"):
        self.send.reset_mock()
        recovery._deliver_reset(self.app, email, locale)
        self.send.assert_called_once()
        return re.search(r"/reset-password#([A-Za-z0-9_-]{43})", self.send.call_args.args[2]).group(1)

    def reset(self, token, password="New-recovered-password-456", **extra):
        data = {"token": token, "new_password": password, "new_password_confirm": password, "csrf_token": self.csrf()}
        data.update(extra)
        return self.client.post("/reset-password", data=data)

    def test_login_button_public_forms_and_private_headers(self):
        self.assertIn(b'href="/forgot-password"', self.client.get("/login").data)
        for path in ("/forgot-password", "/reset-password"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_request_response_does_not_disclose_account_or_delivery_configuration(self):
        bodies = []
        for address in ("recover@example.invalid", "unknown@example.invalid", "invalid"):
            response = self.request_link(address, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            bodies.append(response.get_data(as_text=True))
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[1], bodies[2])
        self.assertEqual(self.dispatch.call_count, 2)
        self.send.assert_not_called()

    def test_email_is_normalised_before_background_dispatch(self):
        self.request_link("  ReCover@Example.Invalid  ")
        self.assertEqual(self.dispatch.call_args.args[1], "recover@example.invalid")

    def test_both_forms_reject_missing_or_wrong_csrf(self):
        for path in ("/forgot-password", "/reset-password"):
            for supplied in (None, "wrong", "árvíz"):
                data = {"email": "recover@example.invalid"}
                if supplied is not None:
                    data["csrf_token"] = supplied
                self.assertEqual(self.client.post(path, data=data).status_code, 400)
        self.dispatch.assert_not_called()

    def test_delivery_uses_trusted_origin_and_only_stores_token_hash(self):
        token = self.delivered_token()
        recipient, subject, text, html = self.send.call_args.args
        self.assertEqual(recipient, "recover@example.invalid")
        self.assertIn("https://portal.example.invalid/reset-password#" + token, text)
        self.assertIn("30 minutes", text)
        self.assertNotIn("Original-password-123", text + html)
        with self.app.app_context():
            saved = db.session.get(PasswordResetToken, sha256(token.encode()).hexdigest())
            self.assertIsNotNone(saved)
            self.assertEqual(saved.expires_at - saved.created_at, timedelta(minutes=30))
            self.assertNotIn(token, str(saved.__dict__))
        with self.app.test_request_context(headers={"Host": "attacker.invalid", "X-Forwarded-Host": "attacker.invalid"}):
            self.delivered_token()
            self.assertNotIn("attacker.invalid", self.send.call_args.args[2])

    def test_unknown_and_disabled_accounts_do_not_send_or_create_tokens(self):
        recovery._deliver_reset(self.app, "missing@example.invalid", "en")
        with self.app.app_context():
            db.session.get(MailServerSettings, 1).enabled = False
            db.session.commit()
        recovery._deliver_reset(self.app, "recover@example.invalid", "en")
        self.send.assert_not_called()
        with self.app.app_context():
            self.assertEqual(PasswordResetToken.query.count(), 0)

    def test_mail_failure_removes_token_without_logging_sensitive_details(self):
        self.send.side_effect = MailDeliveryError("authentication")
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            recovery._deliver_reset(self.app, "recover@example.invalid", "en")
        self.assertNotIn("recover@example.invalid", " ".join(logs.output))
        with self.app.app_context():
            self.assertEqual(PasswordResetToken.query.count(), 0)

    def test_single_use_reset_revokes_old_login_and_requires_new_login(self):
        old_client = self.app.test_client()
        self.assertEqual(old_client.post("/login", data={"login": "recover", "password": "Original-password-123"}).status_code, 302)
        token = self.delivered_token()
        response = self.reset(token)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/login")
        with self.client.session_transaction() as session:
            self.assertNotIn("_user_id", session)
        self.assertEqual(old_client.get("/dashboard").status_code, 302)
        self.assertEqual(self.reset(token).status_code, 400)
        self.assertEqual(self.client.post("/login", data={"login": "recover", "password": "Original-password-123"}).status_code, 200)
        self.assertEqual(self.client.post("/login", data={"login": "recover", "password": "New-recovered-password-456"}).status_code, 302)

    def test_expired_and_malformed_tokens_cannot_change_password(self):
        token = self.delivered_token()
        with self.app.app_context():
            PasswordResetToken.query.update({"expires_at": recovery._now() - timedelta(seconds=1)})
            db.session.commit()
        for invalid in (token, "", "x" * 43, "é" * 43, "x" * 10000):
            self.assertEqual(self.reset(invalid).status_code, 400)
        with self.app.app_context():
            self.assertTrue(db.session.get(User, self.user_id).check_password("Original-password-123"))

    def test_password_validation_does_not_consume_link_or_echo_password(self):
        token = self.delivered_token()
        for password, confirmation in (("short", "short"), ("x" * 1025, "x" * 1025), ("Valid-long-password", "other")):
            response = self.reset(token, password=password, new_password_confirm=confirmation)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('value="' + password + '"', response.get_data(as_text=True))
        self.assertEqual(self.reset(token).status_code, 302)

    def test_email_or_password_changes_invalidate_existing_link(self):
        token = self.delivered_token()
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            user.email = "new@example.invalid"
            db.session.commit()
        self.assertEqual(self.reset(token).status_code, 400)
        token = self.delivered_token("new@example.invalid")
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            user.set_password("Changed-password-123")
            db.session.commit()
        self.assertEqual(self.reset(token).status_code, 400)

    def test_another_request_preserves_first_link_until_one_is_used(self):
        first = self.delivered_token()
        second = self.delivered_token()
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.get("/reset-password").status_code, 200)
        self.assertEqual(self.reset(first).status_code, 302)
        self.assertEqual(self.reset(second).status_code, 400)

    def test_shared_email_and_requester_limits_survive_new_app_instance(self):
        with self.app.app_context():
            now = recovery._now()
            with patch.object(recovery, "_now", return_value=now):
                self.assertTrue(recovery._allow_request("recover@example.invalid", "ip-one"))
                self.assertFalse(recovery._allow_request("recover@example.invalid", "ip-two"))
            with patch.object(recovery, "_now", return_value=now + timedelta(minutes=2)):
                self.assertTrue(recovery._allow_request("recover@example.invalid", "ip-two"))
            with patch.object(recovery, "_now", return_value=now + timedelta(minutes=4)):
                # The repeated minute-blocked attempt also spent hourly capacity.
                self.assertFalse(recovery._allow_request("recover@example.invalid", "ip-three"))
            keys = [row.key_hash for row in PasswordResetThrottle.query.all()]
            self.assertTrue(all(re.fullmatch("[a-f0-9]{64}", key) for key in keys))
        restarted = create_app()
        with restarted.app_context():
            with patch.object(recovery, "_now", return_value=now + timedelta(hours=2)):
                for index in range(20):
                    self.assertTrue(recovery._allow_request(f"other{index}@example.invalid", "shared-ip"))
                self.assertFalse(recovery._allow_request("over-limit@example.invalid", "shared-ip"))
            db.session.remove()
            db.engine.dispose()

    def test_concurrent_credential_change_wins_over_recovery(self):
        token = self.delivered_token()
        winner = generate_password_hash("Concurrent-winner-password")
        raced = False
        with self.app.app_context():
            def change_before_claim(conn, cursor, statement, parameters, context, executemany):
                nonlocal raced
                if not raced and statement.startswith("UPDATE password_reset_tokens SET"):
                    raced = True
                    with db.engine.begin() as other:
                        other.execute(db.update(User).where(User.id == self.user_id).values(password_hash=winner))
            event.listen(db.engine, "before_cursor_execute", change_before_claim)
            try:
                self.assertFalse(recovery._consume_token(token, "Losing-password-123"))
            finally:
                event.remove(db.engine, "before_cursor_execute", change_before_claim)
            self.assertTrue(raced)
            self.assertEqual(db.session.get(User, self.user_id).password_hash, winner)
            self.assertIsNone(PasswordResetToken.query.one().consumed_at)

    def test_new_tables_and_cascade_are_created_automatically(self):
        with self.app.app_context():
            schema = inspect(db.engine)
            self.assertIn("password_reset_throttles", schema.get_table_names())
            fk = schema.get_foreign_keys("password_reset_tokens")[0]
            self.assertEqual(fk["referred_table"], "users")
            self.assertEqual(fk["options"]["ondelete"], "CASCADE")


if __name__ == "__main__":
    unittest.main()
