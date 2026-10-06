"""Developer-only SMTP configuration and mocked, private SMTP delivery."""

import os
import smtplib
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flask import g
from sqlalchemy import event

from app import create_app, db
from app.mail_settings import MailDeliveryError, _cipher, get_mail_settings, is_mail_enabled, send_email
from app.mail_settings_models import MailServerSettings
from app.mail_key import MailKeyError
from app.models import User, UserPrivilege


class MailSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + os.path.join(self.directory.name, "mail.db"),
            "SECRET_KEY": "stable-mail-test-key", "EMAIL_WORKER_ENABLED": "false",
            "EMAIL_SECRET_KEY": "",
        })
        self.environment.start()
        self.app = create_app()
        self.app.instance_path = os.path.join(self.directory.name, "instance")
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for privilege in UserPrivilege:
            user = User(username=privilege.value, email=f"{privilege.value}@example.invalid", password_hash="unused", privilege=privilege)
            db.session.add(user)
            self.users[privilege.value] = user
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.environment.stop()
        self.directory.cleanup()

    def login(self, role="developer"):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.users[role].get_id()
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)

    def form(self, **changes):
        self.client.get("/settings")
        with self.client.session_transaction() as session:
            token = session["mail_settings_csrf_token"]
        data = {
            "csrf_token": token, "revision": get_mail_settings().revision,
            "host": "smtp.example.invalid", "port": "587", "security": "starttls",
            "username": "service@example.invalid", "password": "test-secret-password",
            "sender_email": "portal@example.invalid", "sender_name": "CSUPOR",
            "base_url": "https://csupor.example.invalid", "enabled": "1",
        }
        data.update(changes)
        return data

    def save(self, **changes):
        with patch("app.notification_delivery.wake_notifications"):
            return self.client.post("/settings", data=self.form(**changes))

    def saved(self):
        db.session.expire_all()
        return db.session.get(MailServerSettings, 1)

    def test_only_developer_can_view_or_change_settings(self):
        self.assertEqual(self.client.get("/settings").status_code, 302)
        for role in ("employee", "hr", "ceo"):
            self.login(role)
            self.assertEqual(self.client.get("/settings").status_code, 403)
            self.assertEqual(self.client.post("/settings").status_code, 403)
            self.assertEqual(self.client.post("/settings/email-key").status_code, 403)
        self.login()
        self.assertEqual(self.client.get("/settings").status_code, 200)

    def test_reading_disabled_defaults_does_not_write_or_commit(self):
        self.login()
        self.assertEqual(self.client.get("/settings").status_code, 200)
        self.assertEqual(MailServerSettings.query.count(), 0)
        self.assertFalse(get_mail_settings().enabled)
        self.assertFalse(is_mail_enabled())
        self.assertEqual(len(db.session.new), 0)

    def test_saves_encrypted_password_without_returning_it(self):
        self.login()
        self.assertEqual(self.save().status_code, 302)
        saved = self.saved()
        self.assertTrue(saved.enabled)
        self.assertEqual(saved.port, 587)
        self.assertNotIn("test-secret-password", saved.encrypted_password)
        self.assertEqual(_cipher().decrypt(saved.encrypted_password.encode()).decode(), "test-secret-password")
        self.assertEqual(saved.updated_by_id, self.users["developer"].id)
        self.assertIsNotNone(saved.updated_at)
        self.assertEqual(len(saved.revision), 32)
        page = self.client.get("/settings")
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertNotIn(b"test-secret-password", page.data)
        self.assertNotIn(saved.encrypted_password.encode(), page.data)
        self.assertIn(b"A password is saved", page.data)

    def test_blank_password_preserves_and_explicit_clear_removes(self):
        self.login()
        self.save()
        original = self.saved().encrypted_password
        self.save(password="", sender_name="Updated name")
        self.assertEqual(self.saved().encrypted_password, original)
        self.save(password="", clear_password="1", username="")
        self.assertEqual(self.saved().encrypted_password, "")

    def test_password_is_not_reflected_when_validation_fails(self):
        self.login()
        response = self.save(host="invalid/hostname", password="NeverEchoThisPassword")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"NeverEchoThisPassword", response.data)
        self.assertEqual(MailServerSettings.query.count(), 0)

    def test_csrf_is_required_and_checked_before_any_write(self):
        self.login()
        for token in ("", "invalid", "árvíz"):
            response = self.save(csrf_token=token)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(MailServerSettings.query.count(), 0)

    def test_stale_form_does_not_overwrite_newer_settings(self):
        self.login()
        self.save()
        old = self.form(password="", sender_name="Outdated")
        self.save(password="", sender_name="Newer")
        response = self.client.post("/settings", data=old, follow_redirects=True)
        self.assertIn(b"changed in another session", response.data)
        self.assertEqual(self.saved().sender_name, "Newer")

    def test_optimistic_version_rejects_race_after_validation(self):
        self.login()
        self.save()
        original_revision = self.saved().revision
        data = self.form(password="", sender_name="Outdated race")

        def competing_update(session, flush_context, instances):
            with db.engine.begin() as connection:
                connection.execute(MailServerSettings.__table__.update().values(sender_name="Concurrent", revision="f" * 32))

        event.listen(db.session(), "before_flush", competing_update, once=True)
        with patch("app.notification_delivery.wake_notifications"):
            response = self.client.post("/settings", data=data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.saved().sender_name, "Concurrent")
        self.assertNotEqual(self.saved().revision, original_revision)

    def test_validation_rejects_bad_ports_security_headers_and_urls(self):
        self.login()
        invalid = [
            {"port": "0"}, {"port": "65536"}, {"port": "9" * 5000}, {"port": "-1"}, {"port": "abc"},
            {"security": "unsafe"}, {"host": "smtp.example.invalid:25"}, {"host": "https://smtp.example.invalid"},
            {"sender_email": "bad@@example.invalid"}, {"sender_email": "Name <mail@example.invalid>"},
            {"sender_email": "ok@example.invalid\r\nBcc: other@example.invalid"},
            {"sender_name": "CSUPOR\nBcc: other@example.invalid"}, {"username": "account\n"},
            {"base_url": "javascript:alert(1)"}, {"base_url": "https://user:password@example.invalid"},
            {"base_url": "https://example.invalid/path?secret=bad"}, {"base_url": "https://example.invalid/#frag"},
            {"base_url": "https://example.invalid:70000"}, {"base_url": "https://example.invalid\\evil"},
            {"host": ""}, {"sender_email": ""}, {"base_url": ""},
            {"password": "new-password", "clear_password": "1"},
        ]
        for changes in invalid:
            with self.subTest(changes=list(changes.keys())):
                self.assertEqual(self.save(**changes).status_code, 400)
                self.assertEqual(MailServerSettings.query.count(), 0)

    def test_disabled_configuration_can_be_saved_without_server_or_credentials(self):
        self.login()
        response = self.save(enabled="", host="", sender_email="", base_url="", username="", password="")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.saved().enabled)

    def test_default_secret_cannot_store_password_or_enable_email(self):
        self.app.config["SECRET_KEY"] = "dev-secret-key-change-me"
        self.login()
        for changes in ({"enabled": "", "password": "new-secret"}, {"password": "", "username": ""}):
            self.assertEqual(self.save(**changes).status_code, 400)
        self.assertEqual(MailServerSettings.query.count(), 0)

    def prepare_key(self, **changes):
        self.client.get("/settings")
        with self.client.session_transaction() as session:
            token = session["mail_settings_csrf_token"]
        return self.client.post("/settings/email-key", data={"csrf_token": token, **changes}, follow_redirects=True)

    def test_browser_key_setup_enables_password_save_without_restart_or_key_disclosure(self):
        self.app.config["SECRET_KEY"] = "dev-secret-key-change-me"
        self.login()
        page = self.client.get("/settings")
        self.assertIn(b'id="create-mail-key"', page.data)
        self.assertFalse(os.path.exists(self.app.instance_path))
        response = self.prepare_key()
        self.assertIn(b"Encryption key created", response.data)
        self.assertNotIn(b'id="create-mail-key"', response.data)
        key_path = os.path.join(self.app.instance_path, "email-secret.key")
        with open(key_path, "rb") as source:
            secret = source.read()
        self.assertNotIn(secret.strip(), response.data)
        self.assertEqual(MailServerSettings.query.count(), 0)
        self.assertEqual(self.save().status_code, 302)
        encrypted = self.saved().encrypted_password
        self.assertEqual(_cipher().decrypt(encrypted.encode()).decode(), "test-secret-password")
        # A separately created application sees the persisted key immediately.
        second = create_app()
        second.instance_path = self.app.instance_path
        second.config["SECRET_KEY"] = "dev-secret-key-change-me"
        with second.app_context():
            self.assertEqual(_cipher().decrypt(encrypted.encode()).decode(), "test-secret-password")
            db.session.remove()
            db.engine.dispose()
        self.prepare_key()
        with open(key_path, "rb") as source:
            self.assertEqual(source.read(), secret)

    def test_key_setup_requires_csrf_and_post(self):
        self.app.config["SECRET_KEY"] = "dev-secret-key-change-me"
        self.login()
        self.assertEqual(self.client.get("/settings/email-key").status_code, 405)
        self.client.get("/settings")
        for token in ("", "wrong", "árvíz"):
            self.assertEqual(self.client.post("/settings/email-key", data={"csrf_token": token}).status_code, 400)
        self.assertFalse(os.path.exists(self.app.instance_path))

    def test_existing_configured_key_is_preserved_by_repeated_setup(self):
        self.login()
        self.save()
        encrypted = self.saved().encrypted_password
        response = self.prepare_key()
        self.assertIn(b"existing key was kept", response.data)
        self.assertFalse(os.path.exists(self.app.instance_path))
        self.assertEqual(self.saved().encrypted_password, encrypted)
        self.assertEqual(_cipher().decrypt(encrypted.encode()).decode(), "test-secret-password")

    def test_key_setup_failure_is_actionable_without_exposing_server_details(self):
        self.app.config["SECRET_KEY"] = "dev-secret-key-change-me"
        self.login()
        with patch("app.mail_settings.initialise_mail_secret", side_effect=MailKeyError("storage")):
            response = self.prepare_key()
        self.assertIn(b"hosting provider", response.data)
        self.assertNotIn(self.app.instance_path.encode(), response.data)
        self.assertEqual(MailServerSettings.query.count(), 0)

    def test_missing_key_does_not_replace_existing_password_without_explicit_removal(self):
        self.login()
        self.save()
        encrypted = self.saved().encrypted_password
        self.app.config["SECRET_KEY"] = "dev-secret-key-change-me"
        self.login()
        response = self.prepare_key()
        self.assertIn(b"remove the saved password", response.data)
        self.assertEqual(self.saved().encrypted_password, encrypted)
        self.assertFalse(os.path.exists(self.app.instance_path))
        failed_save = self.save(password="")
        self.assertEqual(failed_save.status_code, 400)
        self.assertIn(b"remove the saved password", failed_save.data)
        self.assertNotIn(b"First use the Create encryption key button", failed_save.data)
        self.assertEqual(self.save(enabled="", password="", clear_password="1").status_code, 302)
        self.assertIn(b"Encryption key created", self.prepare_key().data)

    def test_explicit_email_key_survives_flask_key_change(self):
        self.login()
        self.app.config["EMAIL_SECRET_KEY"] = "stable-explicit-email-key"
        self.save()
        encrypted = self.saved().encrypted_password
        self.app.config["SECRET_KEY"] = "different-login-session-key"
        self.assertEqual(_cipher().decrypt(encrypted.encode()).decode(), "test-secret-password")

    def test_key_change_requires_password_reentry_before_enabling(self):
        self.login()
        self.save()
        self.app.config["EMAIL_SECRET_KEY"] = "new-email-key"
        response = self.save(password="")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"enter the password again", response.data)
        self.assertEqual(self.save(password="new-server-password").status_code, 302)

    def _smtp_mock(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        return connection

    def test_starttls_verifies_certificates_and_sends_private_multipart(self):
        self.login()
        self.save()
        smtp = self._smtp_mock()
        with patch("app.mail_settings.smtplib.SMTP", return_value=smtp) as constructor:
            send_email("employee@example.invalid", "Távolléti értesítő", "Plain text", "<p>HTML text</p>", message_id="<stable@example.invalid>")
        self.assertEqual(constructor.call_args.kwargs["timeout"], 12)
        self.assertEqual(smtp.starttls.call_count, 1)
        context = smtp.starttls.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        smtp.login.assert_called_once_with("service@example.invalid", "test-secret-password")
        self.assertEqual(smtp.ehlo.call_count, 2)
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "employee@example.invalid")
        self.assertEqual(message["Message-ID"], "<stable@example.invalid>")
        self.assertNotIn("Bcc", message)
        self.assertNotIn("Cc", message)
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertEqual([part.get_content_type() for part in message.iter_parts()], ["text/plain", "text/html"])
        self.assertEqual(smtp.send_message.call_args.kwargs["to_addrs"], ["employee@example.invalid"])

    def test_ssl_uses_verified_context_and_no_auth_without_username(self):
        self.login()
        self.save(security="ssl", port="465", username="", password="")
        smtp = self._smtp_mock()
        with patch("app.mail_settings.smtplib.SMTP_SSL", return_value=smtp) as constructor, patch("app.mail_settings.smtplib.SMTP") as plain:
            send_email("employee@example.invalid", "Subject", "Text", "<p>HTML</p>")
        plain.assert_not_called()
        self.assertTrue(constructor.call_args.kwargs["context"].check_hostname)
        smtp.starttls.assert_not_called()
        smtp.login.assert_not_called()

    def test_plain_connection_is_explicit_and_starttls_is_never_downgraded(self):
        self.login()
        self.save(security="none", port="25", username="", password="")
        smtp = self._smtp_mock()
        with patch("app.mail_settings.smtplib.SMTP", return_value=smtp):
            send_email("employee@example.invalid", "Subject", "Text", "<p>HTML</p>")
        smtp.starttls.assert_not_called()
        self.save(security="starttls", password="")
        smtp = self._smtp_mock()
        smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("raw provider detail")
        with patch("app.mail_settings.smtplib.SMTP", return_value=smtp), self.assertRaises(MailDeliveryError):
            send_email("employee@example.invalid", "Subject", "Text", "<p>HTML</p>")
        smtp.login.assert_not_called()
        smtp.send_message.assert_not_called()

    def test_disabled_transport_never_connects(self):
        with patch("app.mail_settings.smtplib.SMTP") as constructor, self.assertRaises(MailDeliveryError) as failure:
            send_email("employee@example.invalid", "Subject", "Text", "<p>HTML</p>")
        constructor.assert_not_called()
        self.assertEqual(failure.exception.code, "disabled")

    def test_header_injection_and_multiple_recipients_are_rejected_before_connecting(self):
        self.login()
        self.save()
        for recipient, subject, message_id in (
            ("one@example.invalid,two@example.invalid", "Subject", None),
            ("one@example.invalid", "Subject\r\nBcc: bad@example.invalid", None),
            ("one@example.invalid", "Subject", "<ok@example.invalid>\nBcc: bad@example.invalid"),
        ):
            with patch("app.mail_settings.smtplib.SMTP") as constructor, self.assertRaises(MailDeliveryError):
                send_email(recipient, subject, "Text", "<p>HTML</p>", message_id=message_id)
            constructor.assert_not_called()

    def test_authentication_failure_has_safe_code_and_no_provider_message(self):
        self.login()
        self.save()
        smtp = self._smtp_mock()
        smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"PRIVATE_PROVIDER_RESPONSE secret=abc")
        with patch("app.mail_settings.smtplib.SMTP", return_value=smtp), self.assertRaises(MailDeliveryError) as failure:
            send_email("employee@example.invalid", "Subject", "Text", "<p>HTML</p>")
        self.assertEqual(failure.exception.code, "authentication")
        self.assertNotIn("PRIVATE", str(failure.exception))
        self.assertNotIn("abc", str(failure.exception))
        smtp.send_message.assert_not_called()

    def test_wake_occurs_only_after_enabled_configuration_is_committed(self):
        self.login()
        checks = []

        def wake(app=None):
            # The worker also gets a general after-request wake. Verify this
            # route's explicit no-argument call occurs only after commit.
            if app is not None:
                return
            checks.append(bool(db.session.get(MailServerSettings, 1).enabled))
            self.assertEqual(len(db.session.dirty), 0)

        with patch("app.notification_delivery.wake_notifications", side_effect=wake):
            self.client.post("/settings", data=self.form())
        self.assertEqual(checks, [True])
        with patch("app.notification_delivery.wake_notifications") as no_wake:
            self.client.post("/settings", data=self.form(enabled="", password=""))
        self.assertTrue(all(call.args for call in no_wake.call_args_list))


if __name__ == "__main__":
    unittest.main()
