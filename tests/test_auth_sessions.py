"""A password replacement revokes every previously authenticated browser."""

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import event
from werkzeug.security import generate_password_hash

from app import create_app, db
from app.models import User, UserProfile, load_user


class PasswordBoundSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(Path(self.directory.name) / "sessions.db"),
            "SECRET_KEY": "password-session-test-key",
            "EMAIL_WORKER_ENABLED": "false",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in ("INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_EMAIL", "INITIAL_ADMIN_PASSWORD"):
            os.environ.pop(key, None)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        with self.app.app_context():
            user = User(username="session-user", email="session-user@example.invalid")
            user.set_password("Original-test-password-123")
            user.profile = UserProfile(full_name="Session user")
            db.session.add(user)
            db.session.commit()
            self.user_id = user.id
            self.identity = user.get_id()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def login(self, client, password="Original-test-password-123"):
        response = client.post("/login", data={"login": "session-user", "password": password})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get("/dashboard").status_code, 200)

    def password_form(self, client):
        response = client.get("/password")
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as session:
            token = session["change_password_csrf_token"]
        self.assertTrue(token)
        self.assertIn(token.encode(), response.data)
        return {
            "csrf_token": token,
            "current_password": "Original-test-password-123",
            "new_password": "Changed-in-form-password-789",
            "new_password_confirm": "Changed-in-form-password-789",
        }

    def test_identity_round_trips_without_containing_the_password_hash(self):
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertIs(load_user(self.identity), user)
            self.assertEqual(self.identity, user.get_id())
            self.assertNotIn(user.password_hash, self.identity)
            self.assertNotIn("Original-test-password-123", self.identity)
            self.assertRegex(self.identity, rf"^{self.user_id}:[0-9a-f]{{64}}$")

    def test_normal_login_stores_the_password_bound_identity(self):
        client = self.app.test_client()
        self.login(client)
        with client.session_transaction() as session:
            self.assertEqual(session["_user_id"], self.identity)

    def test_replacing_password_revokes_independent_existing_browser_sessions(self):
        clients = [self.app.test_client(), self.app.test_client()]
        for client in clients:
            self.login(client)
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            user.set_password("Replacement-test-password-456")
            db.session.commit()
            self.assertIsNone(load_user(self.identity))
            self.assertNotEqual(user.get_id(), self.identity)
        for client in clients:
            response = client.get("/dashboard")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response.location)
        self.login(clients[0], "Replacement-test-password-456")

    def test_setting_the_same_password_also_revokes_old_sessions(self):
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            user.set_password("Original-test-password-123")
            db.session.commit()
            self.assertTrue(user.check_password("Original-test-password-123"))
            self.assertIsNone(load_user(self.identity))

    def test_legacy_numeric_session_requires_a_fresh_login(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True
        response = client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        self.login(client)

    def test_malformed_and_unknown_identities_are_rejected_without_errors(self):
        invalid = [
            None, 1, "", "not-a-user", "1", ":", self.identity + ":extra",
            "x:" + "0" * 64, "0:" + "0" * 64, "-1:" + "0" * 64,
            "01:" + "0" * 64, "١:" + "0" * 64, "1:" + "é" * 64,
            "1:" + "A" * 64, "1:" + "0" * 63,
            "999999:" + "0" * 64, "9223372036854775808:" + "0" * 64,
            "9" * 1000 + ":" + "0" * 64,
        ]
        with self.app.app_context():
            for identity in invalid:
                with self.subTest(identity=str(identity)[:90]):
                    self.assertIsNone(load_user(identity))
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = "not-a-user"
        self.assertEqual(client.get("/dashboard").status_code, 302)

    def test_fingerprint_cannot_be_reused_for_another_account(self):
        with self.app.app_context():
            original = db.session.get(User, self.user_id)
            other = User(username="other", email="other@example.invalid", password_hash=original.password_hash)
            db.session.add(other)
            db.session.commit()
            reused = f"{other.id}:{self.identity.split(':', 1)[1]}"
            self.assertIsNone(load_user(reused))
            self.assertIs(load_user(other.get_id()), other)

    def test_deleted_account_and_changed_server_key_reject_old_identity(self):
        with self.app.app_context():
            with patch.dict(self.app.config, SECRET_KEY="another-server-key"):
                self.assertIsNone(load_user(self.identity))
            user = db.session.get(User, self.user_id)
            db.session.delete(user)
            db.session.commit()
            self.assertIsNone(load_user(self.identity))

    def test_password_form_generates_a_session_csrf_token(self):
        client = self.app.test_client()
        self.login(client)
        with client.session_transaction() as session:
            self.assertNotIn("change_password_csrf_token", session)
        first_form = self.password_form(client)
        self.assertEqual(self.password_form(client)["csrf_token"], first_form["csrf_token"])

    def test_password_form_keeps_this_session_and_revokes_other_browsers(self):
        client, other_browser = self.app.test_client(), self.app.test_client()
        self.login(client)
        self.login(other_browser)
        form = self.password_form(client)
        response = client.post("/password", data=form)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get("/dashboard").status_code, 200)
        with client.session_transaction() as session:
            refreshed_identity = session["_user_id"]
        self.assertNotEqual(refreshed_identity, self.identity)
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertTrue(user.check_password(form["new_password"]))
            self.assertEqual(refreshed_identity, user.get_id())
            self.assertIsNone(load_user(self.identity))
        revoked = other_browser.get("/dashboard")
        self.assertEqual(revoked.status_code, 302)
        self.assertIn("/login", revoked.location)

    def test_password_form_rejects_missing_or_invalid_csrf_without_changing_password(self):
        client = self.app.test_client()
        self.login(client)
        form = self.password_form(client)
        for token in (None, "wrong-token"):
            with self.subTest(token=token):
                rejected_form = dict(form)
                if token is None:
                    rejected_form.pop("csrf_token")
                else:
                    rejected_form["csrf_token"] = token
                self.assertEqual(client.post("/password", data=rejected_form).status_code, 400)
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertTrue(user.check_password("Original-test-password-123"))
            self.assertEqual(user.get_id(), self.identity)

    def assert_password_race_preserves_winner(self, column, winning_value):
        """Commit a second connection's change immediately before the guarded UPDATE."""
        client = self.app.test_client()
        self.login(client)
        form = self.password_form(client)
        raced = []
        with self.app.app_context():
            engine = db.engine
            database_path = engine.url.database

        def commit_winner(connection, cursor, statement, parameters, context, executemany):
            if raced or not statement.lstrip().upper().startswith("UPDATE USERS "):
                return
            raced.append(True)
            # The SQL statement is deliberately limited to these two constant
            # account columns; this connection is independent of the request.
            self.assertIn(column, ("email", "password_hash"))
            with sqlite3.connect(database_path) as competitor:
                competitor.execute(f"UPDATE users SET {column} = ? WHERE id = ?", (winning_value, self.user_id))

        event.listen(engine, "before_cursor_execute", commit_winner)
        try:
            response = client.post("/password", data=form)
        finally:
            event.remove(engine, "before_cursor_execute", commit_winner)
        self.assertEqual(raced, [True], "The race must happen before the route's actual SQL update.")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        with client.session_transaction() as session:
            self.assertNotIn("_user_id", session)
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertEqual(getattr(user, column), winning_value)
            self.assertFalse(user.check_password(form["new_password"]))
            if column == "email":
                self.assertTrue(user.check_password("Original-test-password-123"))

    def test_password_form_cannot_overwrite_a_concurrent_password_reset(self):
        self.assert_password_race_preserves_winner("password_hash", generate_password_hash("Race-winning-password-456"))

    def test_password_form_cannot_replace_password_after_a_concurrent_email_change(self):
        self.assert_password_race_preserves_winner("email", "race-winner@example.invalid")


if __name__ == "__main__":
    unittest.main()
