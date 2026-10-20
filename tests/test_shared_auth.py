"""The CSUPOR boundary cannot fall back to legacy account credentials in SSO mode."""

import os
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
import tempfile
import unittest
from unittest.mock import patch

from app import create_app, db
from app.models import User, UserPrivilege, UserProfile, load_user
from app.shared_auth_models import ExternalIdentity


class SharedAuthenticationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(Path(self.directory.name) / "accounts-boundary.db"),
            "SECRET_KEY": "shared-auth-boundary-secret-" + "x" * 32,
            "EMAIL_WORKER_ENABLED": "false", "AUTH_MODE": "oidc",
            "OIDC_ISSUER": "https://accounts.example.invalid",
            "OIDC_CLIENT_ID": "csupor", "OIDC_CLIENT_SECRET": "test-client-secret-" + "x" * 32,
            "OIDC_REDIRECT_URI": "https://csupor.example.invalid/auth/callback",
        }):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        with self.app.app_context():
            user = User(username="existing-worker", email="worker@example.invalid",
                        privilege=UserPrivilege.hr)
            user.set_password("Original-test-password-123")
            user.profile = UserProfile(full_name="Existing worker")
            db.session.add(user)
            db.session.flush()
            self.user_id = user.id
            self.hash_before = user.password_hash
            db.session.add(ExternalIdentity(issuer="https://accounts.example.invalid",
                                           subject="d96ab414-a302-47b7-821d-129732c671c3",
                                           user_id=user.id))
            db.session.commit()
            with patch.dict(self.app.config, AUTH_MODE="local"):
                self.legacy_identity = user.get_id()
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_legacy_password_bound_browser_session_cannot_bypass_sso(self):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.legacy_identity
            session["_fresh"] = True
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        with self.app.app_context():
            self.assertIsNone(load_user(self.legacy_identity))

    def test_local_account_forms_cannot_create_users_or_change_passwords(self):
        # These routes must leave the local database untouched even when a
        # caller bypasses the UI and submits the historical forms directly.
        with patch("app.routes.begin_login", return_value=("Central sign-in", 302)):
            response = self.client.post("/register", data={
                "username": "unexpected-local", "email": "unexpected@example.invalid",
                "password": "Should-never-be-used-123",
            })
            self.assertIn(response.status_code, (302, 303, 400, 403, 405))
            response = self.client.post("/login", data={
                "login": "existing-worker", "password": "Original-test-password-123",
            })
            self.assertIn(response.status_code, (302, 303, 400, 403, 405))
        with self.client.session_transaction() as session:
            self.assertNotIn("_user_id", session)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 1)
            self.assertEqual(db.session.get(User, self.user_id).password_hash, self.hash_before)

    def test_outstanding_local_reset_link_cannot_mutate_password(self):
        from app.password_reset import _consume_token, _credentials_hash
        from app.password_reset_models import PasswordResetToken
        token = token_urlsafe(32)
        with self.app.app_context():
            db.session.add(PasswordResetToken(
                token_hash=sha256(token.encode()).hexdigest(), user_id=self.user_id,
                credentials_hash=_credentials_hash(db.session.get(User, self.user_id)),
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10),
            ))
            db.session.commit()
            self.assertFalse(_consume_token(token, "New-local-password-123"))
        response = self.client.post("/reset-password", data={
            "token": token, "new_password": "New-local-password-123",
            "new_password_confirm": "New-local-password-123",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "https://accounts.example.invalid/forgot-password")
        with self.app.app_context():
            self.assertEqual(db.session.get(User, self.user_id).password_hash, self.hash_before)

    def test_unavailable_provider_does_not_fall_back_to_local_password_login(self):
        with patch("app.shared_auth._client", side_effect=ConnectionError("simulated unavailable provider")):
            response = self.client.post("/login", data={
                "login": "existing-worker", "password": "Original-test-password-123",
            })
        self.assertEqual(response.status_code, 503)
        with self.client.session_transaction() as session:
            self.assertNotIn("_user_id", session)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 1)

    def test_non_https_provider_is_rejected_unless_loopback_development_is_explicit(self):
        with patch.dict(os.environ, {
            "DATABASE_URL": "sqlite://", "AUTH_MODE": "oidc",
            "SECRET_KEY": "shared-auth-boundary-secret-" + "x" * 32,
            "OIDC_ISSUER": "http://accounts.example.com",
            "OIDC_CLIENT_ID": "csupor", "OIDC_CLIENT_SECRET": "secret-" + "x" * 32,
            "OIDC_REDIRECT_URI": "https://csupor.example.com/auth/callback",
            "AUTH_ALLOW_HTTP": "true", "EMAIL_WORKER_ENABLED": "false",
        }):
            with self.assertRaises((RuntimeError, ValueError)):
                create_app()


if __name__ == "__main__":
    unittest.main()
