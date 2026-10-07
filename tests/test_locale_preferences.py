"""New sessions start in Hungarian while explicit choices stay in effect."""

import os
import unittest
from unittest.mock import patch

from app import create_app, db
from app.models import User


class LocalePreferenceTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {
            "DATABASE_URL": "sqlite://",
            "SECRET_KEY": "locale-test-only",
            "EMAIL_WORKER_ENABLED": "false",
        }):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        with self.app.app_context():
            user = User(username="locale-user", email="locale@example.invalid")
            user.set_password("Locale-test-password-123")
            db.session.add(user)
            db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def assert_page_locale(self, response, locale):
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'<html lang="{locale}">', response.get_data(as_text=True))

    def login(self):
        response = self.client.post("/login", data={
            "login": "locale-user", "password": "Locale-test-password-123",
        }, follow_redirects=True, headers={"Accept-Language": "en-GB,en;q=0.9"})
        self.assertEqual(response.status_code, 200)
        return response

    def test_new_browser_uses_hungarian_regardless_of_language_header(self):
        self.assertEqual(self.app.config["BABEL_DEFAULT_LOCALE"], "hu")
        for header in (None, "en-GB,en;q=0.9", "de-DE,de;q=0.9,en;q=0.8", "hu-HU"):
            with self.subTest(header=header):
                client = self.app.test_client()
                headers = {"Accept-Language": header} if header else {}
                response = client.get("/login", headers=headers)
                self.assert_page_locale(response, "hu")
                self.assertIn("Bejelentkezés", response.get_data(as_text=True))

    def test_login_without_a_language_choice_stays_hungarian(self):
        self.assert_page_locale(self.login(), "hu")
        self.assert_page_locale(self.client.get("/logout", follow_redirects=True), "hu")

    def test_registration_without_a_language_choice_stays_hungarian(self):
        response = self.client.post("/register", data={
            "username": "new-locale-user", "email": "new-locale@example.invalid",
            "password": "New-locale-password-123",
        }, follow_redirects=True, headers={"Accept-Language": "en-GB"})
        self.assert_page_locale(response, "hu")

    def test_explicit_english_choice_survives_login_and_logout(self):
        response = self.client.post("/language", data={
            "locale": "en", "next": "/login",
        }, follow_redirects=True, headers={"Accept-Language": "hu-HU"})
        self.assert_page_locale(response, "en")
        self.assert_page_locale(self.login(), "en")
        self.assert_page_locale(self.client.get("/logout", follow_redirects=True), "en")
        # Another browser has no copy of this session's explicit preference.
        self.assert_page_locale(self.app.test_client().get("/login"), "hu")

    def test_switching_back_to_hungarian_overrides_browser_english(self):
        with self.client.session_transaction() as session:
            session["locale"] = "en"
        response = self.client.post("/language", data={
            "locale": "hu", "next": "/login",
        }, follow_redirects=True, headers={"Accept-Language": "en-GB"})
        self.assert_page_locale(response, "hu")

    def test_unsupported_saved_locale_falls_back_to_hungarian(self):
        with self.client.session_transaction() as session:
            session["locale"] = "de"
        self.assert_page_locale(self.client.get("/login", headers={
            "Accept-Language": "en-GB",
        }), "hu")

    def test_unsupported_selection_preserves_explicit_language(self):
        with self.client.session_transaction() as session:
            session["locale"] = "en"
        response = self.client.post("/language", data={
            "locale": "de", "next": "/login",
        }, follow_redirects=True)
        self.assert_page_locale(response, "en")
        with self.client.session_transaction() as session:
            self.assertEqual(session["locale"], "en")


if __name__ == "__main__":
    unittest.main()
