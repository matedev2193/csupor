"""Durable key creation and safe selection without a database or SMTP server."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from app.mail_key import MailKeyError, get_mail_secret, initialise_mail_secret, key_status


class MailKeyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.instance = Path(self.directory.name) / "private-instance"
        self.app = Flask(__name__, instance_path=str(self.instance))
        self.app.config["SECRET_KEY"] = "dev-secret-key-change-me"
        self.context = self.app.app_context()
        self.context.push()
        self.environment = patch.dict(os.environ, {"EMAIL_SECRET_KEY": ""})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.context.pop()
        self.directory.cleanup()

    @property
    def path(self):
        return self.instance / "email-secret.key"

    def write_key(self, content="a" * 64, mode=0o600):
        self.instance.mkdir(mode=0o700, exist_ok=True)
        self.path.write_text(content, encoding="ascii")
        self.path.chmod(mode)

    def test_missing_status_does_not_create_files(self):
        self.assertEqual(key_status(), "missing")
        self.assertFalse(self.instance.exists())
        with self.assertRaises(MailKeyError) as error:
            get_mail_secret()
        self.assertEqual(error.exception.code, "missing")

    def test_known_defaults_are_not_keys(self):
        for default in (None, "", "dev-secret-key-change-me", "change-me", "changeme", "secret"):
            with self.subTest(default=default):
                self.app.config.update(EMAIL_SECRET_KEY=default, SECRET_KEY=default)
                self.assertEqual(key_status(), "missing")

    def test_explicit_config_and_environment_precede_file_and_app_key(self):
        self.write_key()
        self.app.config.update(EMAIL_SECRET_KEY="explicit", SECRET_KEY="old-stable")
        os.environ["EMAIL_SECRET_KEY"] = "environment"
        self.assertEqual(get_mail_secret(), "explicit")
        self.app.config["EMAIL_SECRET_KEY"] = ""
        self.assertEqual(get_mail_secret(), "environment")
        os.environ["EMAIL_SECRET_KEY"] = ""
        self.assertEqual(get_mail_secret(), "a" * 64)
        self.path.unlink()
        self.assertEqual(get_mail_secret(), "old-stable")

    def test_existing_stable_app_key_is_preserved_without_file_creation(self):
        for stable in ("existing-app-secret", b"existing-app-secret"):
            with self.subTest(stable=stable):
                self.app.config["SECRET_KEY"] = stable
                self.assertFalse(initialise_mail_secret())
                self.assertEqual(get_mail_secret(), stable)
                self.assertFalse(self.instance.exists())

    def test_existing_explicit_key_is_preserved_even_if_local_file_is_invalid(self):
        self.write_key("invalid")
        self.app.config["EMAIL_SECRET_KEY"] = "explicit-stable"
        self.assertFalse(initialise_mail_secret())
        self.assertEqual(get_mail_secret(), "explicit-stable")
        self.assertEqual(self.path.read_text(), "invalid")

    def test_initialise_creates_persistent_private_key_only_once(self):
        self.assertTrue(initialise_mail_secret())
        secret = get_mail_secret()
        self.assertRegex(secret, r"^[0-9a-f]{64}$")
        self.assertEqual(key_status(), "ready")
        self.assertFalse(initialise_mail_secret())
        self.assertEqual(get_mail_secret(), secret)
        self.assertEqual(list(self.instance.iterdir()), [self.path])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.instance.stat().st_mode), 0o700)
        restarted = Flask("restarted", instance_path=str(self.instance))
        restarted.config["SECRET_KEY"] = "different-app-key"
        with restarted.app_context():
            self.assertEqual(get_mail_secret(), secret)

    def test_concurrent_workers_share_one_complete_key(self):
        def create(_):
            with self.app.app_context():
                created = initialise_mail_secret()
                return created, get_mail_secret()
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(create, range(36)))
        self.assertEqual(sum(created for created, _ in results), 1)
        self.assertEqual(len({secret for _, secret in results}), 1)
        self.assertEqual(list(self.instance.iterdir()), [self.path])

    def test_invalid_file_is_not_replaced_or_ignored_for_fallback(self):
        for invalid in ("", "short", "A" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n"):
            with self.subTest(invalid=invalid):
                self.write_key(invalid)
                self.app.config["SECRET_KEY"] = "old-stable"
                self.assertEqual(key_status(), "error")
                with self.assertRaises(MailKeyError) as error:
                    initialise_mail_secret()
                self.assertEqual(error.exception.code, "invalid")
                self.assertEqual(self.path.read_text(), invalid)

    @unittest.skipUnless(hasattr(os, "symlink"), "Symlinks are unavailable")
    def test_symlinks_are_rejected_without_changing_target(self):
        self.instance.mkdir(mode=0o700)
        target = Path(self.directory.name) / "external.key"
        target.write_text("b" * 64)
        target.chmod(0o600)
        self.path.symlink_to(target)
        self.assertEqual(key_status(), "error")
        with self.assertRaises(MailKeyError) as error:
            initialise_mail_secret()
        self.assertEqual(error.exception.code, "invalid")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), "b" * 64)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions required")
    def test_group_or_world_readable_file_is_rejected_without_chmod(self):
        self.write_key(mode=0o644)
        self.assertEqual(key_status(), "error")
        with self.assertRaises(MailKeyError) as error:
            initialise_mail_secret()
        self.assertEqual(error.exception.code, "invalid")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_read_failure_is_sanitised_and_never_overwrites_file(self):
        self.write_key()
        with patch("app.mail_key.os.open", side_effect=PermissionError("private path and credentials")):
            self.assertEqual(key_status(), "error")
            with self.assertRaises(MailKeyError) as error:
                initialise_mail_secret()
        self.assertEqual(error.exception.code, "storage")
        self.assertNotIn("private path", str(error.exception))
        self.assertEqual(self.path.read_text(), "a" * 64)

    def test_creation_failure_is_sanitised_and_leaves_no_key_or_temporary(self):
        with patch("app.mail_key.os.link", side_effect=OSError("private credentials")):
            with self.assertRaises(MailKeyError) as error:
                initialise_mail_secret()
        self.assertEqual(error.exception.code, "storage")
        self.assertNotIn("private credentials", str(error.exception))
        self.assertEqual(list(self.instance.iterdir()), [])

    def test_directory_failure_is_sanitised(self):
        with patch("app.mail_key.Path.mkdir", side_effect=PermissionError("server details")):
            with self.assertRaises(MailKeyError) as error:
                initialise_mail_secret()
        self.assertEqual(error.exception.code, "storage")
        self.assertNotIn("server details", str(error.exception))

    def test_concurrent_invalid_file_is_never_replaced(self):
        original_link = os.link
        def link_after_invalid_file(source, target, **kwargs):
            self.write_key("do-not-replace")
            return original_link(source, target, **kwargs)
        with patch("app.mail_key.os.link", side_effect=link_after_invalid_file):
            with self.assertRaises(MailKeyError) as error:
                initialise_mail_secret()
        self.assertEqual(error.exception.code, "invalid")
        self.assertEqual(self.path.read_text(), "do-not-replace")
        self.assertEqual(list(self.instance.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
