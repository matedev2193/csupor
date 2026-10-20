"""Credential migration preserves local ownership and refuses unsafe retries."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import create_app, db
from app.models import User, UserPrivilege, UserProfile
from app.shared_auth_models import ExternalIdentity


class AccountMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(self.root / "csupor.db"),
            "SECRET_KEY": "migration-test-secret", "AUTH_MODE": "local",
            "EMAIL_WORKER_ENABLED": "false",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_EMAIL", "INITIAL_ADMIN_PASSWORD"):
            os.environ.pop(name, None)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.runner = self.app.test_cli_runner()
        with self.app.app_context():
            user = User(username="existing", email="existing@example.test", privilege=UserPrivilege.ceo)
            user.set_password("Existing-password-123")
            user.profile = UserProfile(full_name="Private HR name", bank_account_number="private-bank-data")
            db.session.add(user)
            db.session.commit()
            self.user_id, self.password_hash = user.id, user.password_hash

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def export(self, name="migration.accounts-export.json"):
        target = self.root / name
        result = self.runner.invoke(args=["accounts", "export", "--issuer", "https://accounts.example.test", "--output", str(target)])
        return result, target

    def test_export_preserves_credentials_ids_and_privileges_but_excludes_personnel(self):
        result, path = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        document = json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(document), {"version", "source", "issuer", "users"})
        row = document["users"][0]
        self.assertEqual(set(row), {"subject", "username", "email", "password_hash"})
        self.assertEqual(row["password_hash"], self.password_hash)
        self.assertNotIn("Private HR", path.read_text())
        self.assertNotIn("Existing-password", path.read_text())
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertEqual(user.privilege, UserPrivilege.ceo)
            self.assertEqual(user.password_hash, self.password_hash)
            self.assertEqual(user.profile.bank_account_number, "private-bank-data")
            link = ExternalIdentity.query.filter_by(user_id=user.id).one()
            self.assertEqual(link.subject, row["subject"])
        again, second = self.export("retry.accounts-export.json")
        self.assertEqual(again.exit_code, 0, again.output)
        self.assertEqual(json.loads(second.read_text()), document)

    def test_existing_output_or_symlink_is_not_overwritten(self):
        result, path = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        original = path.read_bytes()
        retry, _ = self.export()
        self.assertNotEqual(retry.exit_code, 0)
        self.assertEqual(path.read_bytes(), original)
        (self.root / "link.accounts-export.json").symlink_to(path)
        # O_EXCL refuses a symlink instead of following its target.
        symlink, _ = self.export("link.accounts-export.json")
        self.assertNotEqual(symlink.exit_code, 0)
        self.assertEqual(path.read_bytes(), original)

    def test_conflicting_identity_rolls_back_new_links_and_creates_no_file(self):
        with self.app.app_context():
            second = User(username="second", email="second@example.test")
            second.set_password("Second-password-123")
            db.session.add(second)
            db.session.flush()
            db.session.add(ExternalIdentity(issuer="https://other.example.test", subject="other", user_id=second.id))
            db.session.commit()
        result, path = self.export()
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(path.exists())
        with self.app.app_context():
            self.assertIsNone(ExternalIdentity.query.filter_by(user_id=self.user_id).first())

    def test_export_is_refused_after_cutover(self):
        self.app.config["AUTH_MODE"] = "oidc"
        result, path = self.export()
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(path.exists())

    def test_retirement_requires_confirmation_and_all_users_linked(self):
        self.app.config.update(AUTH_MODE="oidc", OIDC_ISSUER="https://accounts.example.test")
        command = ["accounts", "retire-local-passwords"]
        self.assertNotEqual(self.runner.invoke(args=command).exit_code, 0)
        command.append("--confirm-imported-and-tested")
        self.assertNotEqual(self.runner.invoke(args=command).exit_code, 0)
        with self.app.app_context():
            self.assertEqual(db.session.get(User, self.user_id).password_hash, self.password_hash)

    def test_retirement_removes_password_copy_without_affecting_hr(self):
        result, _ = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.app.config.update(AUTH_MODE="oidc", OIDC_ISSUER="https://accounts.example.test")
        retired = self.runner.invoke(args=["accounts", "retire-local-passwords", "--confirm-imported-and-tested"])
        self.assertEqual(retired.exit_code, 0, retired.output)
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertTrue(user.password_hash.startswith("!central-account-only!"))
            self.assertEqual(user.privilege, UserPrivilege.ceo)
            self.assertEqual(user.profile.bank_account_number, "private-bank-data")
