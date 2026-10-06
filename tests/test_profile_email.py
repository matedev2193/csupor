"""Authenticated account email changes preserve personal records and ownership."""

import os
import tempfile
import unittest
from unittest.mock import patch

from flask import g
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app import create_app, db
from app.models import User, UserPrivilege, UserProfile
from app.page_access import invalidate_access_cache
from app.page_access_models import PageRolePermission


class ProfileEmailTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + os.path.join(self.directory.name, "profile-email.db"),
            "SECRET_KEY": "profile-email-test-secret", "EMAIL_WORKER_ENABLED": "false",
        }):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        self.ids = {}
        for name, role in (("employee", UserPrivilege.employee), ("hr", UserPrivilege.hr)):
            user = User(username=name, email=f"{name}@example.invalid", privilege=role)
            user.set_password("current-password-123")
            user.profile = UserProfile(full_name=f"Saved {name}", phone_number="saved-phone")
            db.session.add(user)
            self.users[name] = user
        db.session.commit()
        self.ids = {name: user.id for name, user in self.users.items()}

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.directory.cleanup()

    def login(self, name="employee"):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.users[name].get_id()
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)

    def token(self):
        response = self.client.get("/profile")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session["profile_email_csrf_token"]

    def change(self, email="new@example.invalid", password="current-password-123", **extra):
        form = {"email": email, "current_password": password, "csrf_token": self.token()}
        form.update(extra)
        return self.client.post("/profile/email", data=form)

    def account(self, name="employee"):
        db.session.expire_all()
        return db.session.get(User, self.ids[name])

    def flashes(self):
        with self.client.session_transaction() as session:
            return " ".join(message for category, message in session.get("_flashes", []))

    def test_email_change_requires_login_and_does_not_accept_get(self):
        response = self.client.post("/profile/email", data={"email": "new@example.invalid"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        self.assertEqual(self.account().email, "employee@example.invalid")
        self.login()
        self.assertEqual(self.client.get("/profile/email").status_code, 405)

    def test_own_profile_displays_a_separate_current_email_and_password_form(self):
        self.login()
        response = self.client.get("/profile")
        html = response.get_data(as_text=True)
        self.assertIn('id="account-email-form" action="/profile/email" method="post"', html)
        self.assertIn('value="employee@example.invalid"', html)
        self.assertIn('id="account-email-password" type="password"', html)
        self.assertIn('maxlength="120"', html)
        self.assertIn(self.token(), html)

    def test_normalised_email_persists_without_changing_password_or_profile(self):
        self.login()
        old_hash = self.account().password_hash
        with patch("app.mail_settings.send_email") as send:
            response = self.change("  NEW.Address+test@Example.Invalid  ")
        send.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/profile")
        db.session.remove()
        saved = db.session.get(User, self.ids["employee"])
        self.assertEqual(saved.email, "new.address+test@example.invalid")
        self.assertEqual(saved.password_hash, old_hash)
        self.assertEqual(saved.profile.full_name, "Saved employee")
        self.assertEqual(saved.profile.phone_number, "saved-phone")
        self.assertIn("Future emails will be sent to this address.", self.flashes())

    def test_form_identity_and_profile_tampering_cannot_edit_another_account(self):
        self.login()
        response = self.change(
            user_id=str(self.ids["hr"]), username="changed", privilege="developer",
            full_name="Changed personal name", target_user_id=str(self.ids["hr"]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.account().email, "new@example.invalid")
        self.assertEqual(self.account().username, "employee")
        self.assertEqual(self.account().privilege, UserPrivilege.employee)
        self.assertEqual(self.account().profile.full_name, "Saved employee")
        self.assertEqual(self.account("hr").email, "hr@example.invalid")

    def test_manager_profile_editor_does_not_offer_account_email_changes(self):
        self.login("hr")
        response = self.client.get(f"/users/{self.ids['employee']}/profile")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="account-email-form"', response.get_data(as_text=True))
        response = self.client.post(f"/users/{self.ids['employee']}/profile", data={
            "email": "manager-selected@example.invalid", "full_name": "Updated personal details",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.account().email, "employee@example.invalid")

    def test_current_password_must_match(self):
        self.login()
        for password in ("", "wrong-password"):
            with self.subTest(password=password):
                self.assertEqual(self.change(password=password).status_code, 302)
                self.assertEqual(self.account().email, "employee@example.invalid")
                self.assertIn("Current password is incorrect.", self.flashes())

    def test_missing_invalid_or_non_ascii_csrf_is_rejected(self):
        self.login()
        for csrf in ("", "forged-token", "árvíztűrő"):
            with self.subTest(csrf=csrf):
                response = self.change(csrf_token=csrf)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.account().email, "employee@example.invalid")
        with self.client.session_transaction() as session:
            session.pop("profile_email_csrf_token", None)
        response = self.client.post("/profile/email", data={
            "csrf_token": "forged-token", "email": "new@example.invalid", "current_password": "current-password-123",
        })
        self.assertEqual(response.status_code, 400)

    def test_invalid_oversized_and_header_injection_addresses_preserve_saved_email(self):
        self.login()
        for email in (
            "", "not-an-email", "@example.invalid", "name@", "a..b@example.invalid",
            "A Person <person@example.invalid>", "one@example.invalid,two@example.invalid",
            "name@example.invalid\r\nBcc: other@example.invalid", "name@example.invalid\n",
            "\tname@example.invalid", "na\x00me@example.invalid", "name\x7f@example.invalid",
            "máté@example.invalid", "a" * 65 + "@example.invalid",
            "a" * 64 + "@" + "b" * 50 + ".invalid",
        ):
            with self.subTest(email=email):
                self.assertEqual(self.change(email).status_code, 302)
                self.assertEqual(self.account().email, "employee@example.invalid")
                self.assertIn("Enter a valid email address of at most 120 characters.", self.flashes())

    def test_duplicate_check_is_case_insensitive_even_for_legacy_addresses(self):
        self.users["hr"].email = "Existing@Example.Invalid"
        db.session.commit()
        self.login()
        for email in ("Existing@Example.Invalid", "existing@example.invalid", " EXISTING@EXAMPLE.INVALID "):
            with self.subTest(email=email):
                self.assertEqual(self.change(email).status_code, 302)
                self.assertEqual(self.account().email, "employee@example.invalid")
                self.assertIn("already used by another account", self.flashes())

    def test_own_current_email_is_not_a_duplicate(self):
        self.login()
        response = self.change(" EMPLOYEE@EXAMPLE.INVALID ")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.account().email, "employee@example.invalid")
        self.assertIn("Account email address updated.", self.flashes())

    def test_racing_unique_collision_rolls_back_and_gives_safe_feedback(self):
        self.login()
        token = self.token()
        with patch.object(db.session, "commit", side_effect=IntegrityError(
            "UPDATE users SET email=:email", {"email": "sensitive@example.invalid"}, Exception("duplicate"),
        )):
            response = self.client.post("/profile/email", data={
                "csrf_token": token, "email": "new@example.invalid", "current_password": "current-password-123",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.account().email, "employee@example.invalid")
        self.assertIn("already used by another account", self.flashes())
        self.assertNotIn("sensitive@example.invalid", self.flashes())
        self.assertEqual(self.client.get("/profile").status_code, 200)

    def _change_after_concurrent_account_update(self, values):
        self.login()
        token = self.token()
        raced = False

        def update_before_email_write(conn, cursor, statement, parameters, context, executemany):
            nonlocal raced
            if not raced and statement.startswith("UPDATE users SET email="):
                raced = True
                # A separate committed connection represents another request
                # completing after our password check but before our update.
                with db.engine.begin() as other_connection:
                    other_connection.execute(db.update(User).where(User.id == self.ids["employee"]).values(**values))

        event.listen(db.engine, "before_cursor_execute", update_before_email_write)
        try:
            response = self.client.post("/profile/email", data={
                "csrf_token": token, "email": "stale-change@example.invalid", "current_password": "current-password-123",
            })
        finally:
            event.remove(db.engine, "before_cursor_execute", update_before_email_write)
        self.assertTrue(raced)
        self.assertEqual(response.status_code, 302)
        self.assertIn("Your account changed while saving.", self.flashes())
        self.assertNotEqual(self.account().email, "stale-change@example.invalid")

    def test_concurrent_password_reset_prevents_email_change_using_old_password(self):
        new_hash = generate_password_hash("secure-reset-password")
        self._change_after_concurrent_account_update({"password_hash": new_hash})
        self.assertEqual(self.account().password_hash, new_hash)
        self.assertEqual(self.account().email, "employee@example.invalid")

    def test_concurrent_email_change_is_not_overwritten(self):
        self._change_after_concurrent_account_update({"email": "already-changed@example.invalid"})
        self.assertEqual(self.account().email, "already-changed@example.invalid")

    def test_revoking_own_profile_permission_also_blocks_email_post(self):
        self.login()
        token = self.token()
        db.session.add(PageRolePermission(page_key="edit_profile", role="employee", allowed=False))
        db.session.commit()
        invalidate_access_cache()
        response = self.client.post("/profile/email", data={
            "csrf_token": token, "email": "new@example.invalid", "current_password": "current-password-123",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.account().email, "employee@example.invalid")

    def test_updated_email_can_be_used_to_log_in(self):
        self.login()
        self.assertEqual(self.change().status_code, 302)
        self.client.get("/logout")
        g.pop("_login_user", None)
        response = self.client.post("/login", data={"login": "new@example.invalid", "password": "current-password-123"})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session["_user_id"], self.account().get_id())


if __name__ == "__main__":
    unittest.main()
