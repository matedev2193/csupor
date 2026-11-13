"""Credential lifecycle, recovery and cross-browser revocation checks."""

from hashlib import sha256
from pathlib import Path
from time import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from flask import Flask
from sqlalchemy import func
from werkzeug.security import generate_password_hash

from accounts.models import Account, AccessToken, Client, Membership, PasswordReset, db
from accounts.views import web


class AccountPageTests(unittest.TestCase):
    def setUp(self):
        package = Path(__file__).resolve().parents[1]
        self.app = Flask(__name__, template_folder=str(package / "templates"))
        self.app.config.update(
            TESTING=True, SECRET_KEY="account-lifecycle-test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite://", SQLALCHEMY_TRACK_MODIFICATIONS=False,
            ACCOUNTS_ISSUER="https://accounts.example.invalid",
        )
        db.init_app(self.app)
        self.app.register_blueprint(web)
        self.subject = str(uuid4())
        with self.app.app_context():
            db.create_all()
            account = Account(subject=self.subject, username="mate", email="mate@example.invalid",
                              password_hash=generate_password_hash("Original-password-123"), version=1)
            clients = [Client(client_id=client_id, name=client_id.upper(), secret_hash="unused",
                              redirect_uris=["https://example.invalid/callback"], allow_registration=allowed)
                       for client_id, allowed in (("csupor", True), ("safety", False))]
            db.session.add_all([account] + clients)
            db.session.add(Membership(account_subject=self.subject, client_id="csupor"))
            db.session.commit()
        self.browser = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def csrf(self, browser, path="/login"):
        self.assertEqual(browser.get(path).status_code, 200)
        with browser.session_transaction() as session:
            return session["csrf_token"]

    def login(self, browser=None, password="Original-password-123", next_url="/account"):
        browser = browser or self.browser
        response = browser.post("/login", data={
            "csrf_token": self.csrf(browser), "login": "mate", "password": password, "next": next_url,
        })
        self.assertEqual(response.status_code, 302)
        return response

    def reset_token(self, version=1):
        raw = "a-random-password-reset-token-for-testing"
        with self.app.app_context():
            db.session.add(PasswordReset(token_hash=sha256(raw.encode()).hexdigest(), account_subject=self.subject,
                                         version=version, expires_at=int(time()) + 300))
            db.session.commit()
        return raw

    def test_login_requires_csrf_and_rejects_external_redirects(self):
        self.assertEqual(self.browser.post("/login", data={"login": "mate", "password": "Original-password-123"}).status_code, 400)
        response = self.login(next_url="https://evil.example.invalid/")
        self.assertEqual(response.location, "/account")
        with self.browser.session_transaction() as session:
            self.assertEqual(session["account_subject"], self.subject)
            self.assertNotIn("Original-password-123", str(dict(session)))

    def test_oidc_login_ends_form_chain_before_cross_origin_authorization(self):
        target = '/authorize?client_id=csupor&state=single-browser-flow'
        response = self.login(next_url=target)
        self.assertEqual(urlsplit(response.location).path, '/login/continue')
        with self.browser.session_transaction() as session:
            self.assertEqual(session['login_continuation']['next'], target)
            self.assertEqual(session['login_continuation']['nonce'],
                             parse_qs(urlsplit(response.location).query)['flow'][0])
        handoff = self.browser.get(response.location)
        self.assertEqual(handoff.status_code, 200)
        self.assertNotIn('Location', handoff.headers)
        self.assertIn(b'id="login-continuation"', handoff.data)
        self.assertIn(b'/authorize?client_id=csupor&amp;state=single-browser-flow', handoff.data)
        self.assertIn(b'login_continue.js', handoff.data)
        with self.browser.session_transaction() as session:
            self.assertNotIn('login_continuation', session)
        self.assertEqual(self.browser.get(response.location).location, '/account')

    def test_login_handoff_is_session_bound_and_does_not_accept_query_destination(self):
        response = self.login(next_url='/authorize?client_id=csupor&state=private-flow')
        other = self.app.test_client()
        self.assertEqual(other.get(response.location).location, '/login')
        wrong = self.browser.get('/login/continue?flow=wrong&next=https://evil.example.invalid')
        self.assertEqual(wrong.location, '/account')
        with self.browser.session_transaction() as session:
            self.assertIn('login_continuation', session)
        handoff = self.browser.get(response.location + '&next=https://evil.example.invalid')
        self.assertEqual(handoff.status_code, 200)
        self.assertNotIn(b'evil.example.invalid', handoff.data)
        self.assertIn(b'state=private-flow', handoff.data)

    def test_expired_handoff_does_not_repeat_the_authorization_flow(self):
        response = self.login(next_url='/authorize?client_id=csupor&state=old-flow')
        with self.browser.session_transaction() as session:
            pending = dict(session['login_continuation'])
            pending['issued_at'] = int(time()) - 301
            session['login_continuation'] = pending
        self.assertEqual(self.browser.get(response.location).location, '/account')

    def test_reauthentication_marker_survives_login_handoff(self):
        token = self.csrf(self.browser)
        with self.browser.session_transaction() as session:
            session['reauth_request'] = 'bound-reauthentication-marker'
        response = self.browser.post('/login', data={
            'csrf_token': token, 'login': 'mate', 'password': 'Original-password-123',
            'next': '/authorize?client_id=csupor&prompt=login&max_age=0',
        })
        self.assertEqual(self.browser.get(response.location).status_code, 200)
        with self.browser.session_transaction() as session:
            self.assertEqual(session['reauth_completed'], 'bound-reauthentication-marker')

    def test_unknown_and_ambiguous_identifiers_never_authenticate(self):
        token = self.csrf(self.browser)
        response = self.browser.post("/login", data={"csrf_token": token, "login": "unknown", "password": "Original-password-123"})
        self.assertIn("Hibás bejelentkezési".encode(), response.data)
        with self.app.app_context():
            db.session.add(Account(username="mate@example.invalid", email="other@example.invalid", password_hash=generate_password_hash("Original-password-123")))
            db.session.commit()
        response = self.browser.post("/login", data={"csrf_token": token, "login": "mate@example.invalid", "password": "Original-password-123"})
        self.assertIn("Hibás bejelentkezési".encode(), response.data)

    def test_registration_grants_only_the_requested_permitted_client(self):
        self.assertEqual(self.browser.get("/register?client_id=safety").status_code, 403)
        token = self.csrf(self.browser, "/register?client_id=csupor")
        response = self.browser.post("/register", data={
            "csrf_token": token, "client_id": "csupor", "username": "new-user", "email": "new@example.invalid",
            "password": "Strong-new-password-123", "password_confirm": "Strong-new-password-123",
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = db.session.execute(db.select(Account).where(Account.username == "new-user")).scalar_one()
            self.assertFalse(user.email_verified)
            self.assertTrue(user.check_password("Strong-new-password-123"))
            memberships = db.session.execute(db.select(Membership).where(Membership.account_subject == user.subject)).scalars().all()
            self.assertEqual([m.client_id for m in memberships], ["csupor"])

    def test_registration_uses_same_origin_handoff_for_oidc_authorization(self):
        token = self.csrf(self.browser, '/register?client_id=csupor')
        response = self.browser.post('/register', data={
            'csrf_token': token, 'client_id': 'csupor', 'username': 'new-worker',
            'email': 'new-worker@example.invalid', 'password': 'Strong-new-password-123',
            'password_confirm': 'Strong-new-password-123',
            'next': '/authorize?client_id=csupor&state=register-flow',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response.location).path, '/login/continue')
        handoff = self.browser.get(response.location)
        self.assertEqual(handoff.status_code, 200)
        self.assertIn(b'state=register-flow', handoff.data)

    def test_registration_rejects_email_used_as_another_legacy_username(self):
        with self.app.app_context():
            db.session.add(Account(username="legacy@example.invalid", email="other@example.invalid",
                                   password_hash=generate_password_hash("Legacy-password-123")))
            db.session.commit()
        token = self.csrf(self.browser, "/register?client_id=csupor")
        response = self.browser.post("/register", data={
            "csrf_token": token, "client_id": "csupor", "username": "new-user",
            "email": "LEGACY@example.invalid", "password": "Strong-new-password-123",
            "password_confirm": "Strong-new-password-123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("nem hozható létre új fiók".encode(), response.data)
        with self.app.app_context():
            self.assertEqual(Account.query.count(), 2)
            self.assertIsNone(Account.query.filter_by(username="new-user").first())
            self.assertEqual(Account.query.filter_by(username="legacy@example.invalid").one().email,
                             "other@example.invalid")

    def test_email_change_rejects_other_legacy_username_but_allows_own(self):
        with self.app.app_context():
            db.session.add(Account(username="legacy@example.invalid", email="other@example.invalid",
                                   password_hash=generate_password_hash("Legacy-password-123")))
            db.session.commit()
        self.login()
        token = self.csrf(self.browser, "/account")
        response = self.browser.post("/account", data={
            "csrf_token": token, "email": "LEGACY@example.invalid",
            "current_password": "Original-password-123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("e-mail-cím nem használható".encode(), response.data)
        with self.app.app_context():
            account = db.session.get(Account, self.subject)
            self.assertEqual(account.email, "mate@example.invalid")
            self.assertEqual(account.version, 1)
            # Imported accounts may legitimately use their own email as username.
            account.username = "own-login@example.invalid"
            db.session.commit()
        response = self.browser.post("/account", data={
            "csrf_token": token, "email": "own-login@example.invalid",
            "current_password": "Original-password-123",
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(db.session.get(Account, self.subject).email, "own-login@example.invalid")

    def test_password_change_revokes_other_browser_and_old_recovery_token(self):
        other = self.app.test_client()
        self.login()
        self.login(other)
        recovery = self.reset_token()
        token = self.csrf(self.browser, "/password")
        response = self.browser.post("/password", data={
            "csrf_token": token, "current_password": "Original-password-123",
            "new_password": "Changed-password-456", "new_password_confirm": "Changed-password-456",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.browser.get("/account").status_code, 200)
        self.assertEqual(other.get("/account").status_code, 302)
        self.assertEqual(other.get("/reset-password/" + recovery).status_code, 400)

    def test_email_change_requires_password_and_invalidates_old_sessions(self):
        other = self.app.test_client()
        self.login()
        self.login(other)
        token = self.csrf(self.browser, "/account")
        failed = self.browser.post("/account", data={"csrf_token": token, "email": "updated@example.invalid", "current_password": "bad"})
        self.assertIn("jelszó nem megfelelő".encode(), failed.data)
        changed = self.browser.post("/account", data={"csrf_token": token, "email": "updated@example.invalid", "current_password": "Original-password-123"})
        self.assertEqual(changed.status_code, 302)
        self.assertEqual(other.get("/account").status_code, 302)
        with self.app.app_context():
            account = db.session.get(Account, self.subject)
            self.assertEqual(account.email, "updated@example.invalid")
            self.assertFalse(account.email_verified)
            self.assertEqual(account.version, 2)

    def test_recovery_response_is_generic_and_stores_only_hashed_tokens(self):
        token = self.csrf(self.browser, "/forgot-password")
        with patch("accounts.views.send_reset_email") as send:
            existing = self.browser.post("/forgot-password", data={"csrf_token": token, "email": "mate@example.invalid"}, follow_redirects=True)
            missing = self.browser.post("/forgot-password", data={"csrf_token": token, "email": "missing@example.invalid"}, follow_redirects=True)
        self.assertEqual(existing.data, missing.data)
        send.assert_called_once()
        recipient, raw = send.call_args.args
        self.assertEqual(recipient, "mate@example.invalid")
        with self.app.app_context():
            reset = db.session.execute(db.select(PasswordReset)).scalar_one()
            self.assertEqual(reset.token_hash, sha256(raw.encode()).hexdigest())
            self.assertNotEqual(reset.token_hash, raw)

    def test_reset_is_single_use_revokes_old_sessions_and_rejects_weak_password(self):
        other = self.app.test_client()
        self.login(other)
        raw = self.reset_token()
        path = "/reset-password/" + raw
        token = self.csrf(self.browser, path)
        weak = self.browser.post(path, data={"csrf_token": token, "new_password": "short", "new_password_confirm": "short"})
        self.assertIn("12–1024".encode(), weak.data)
        response = self.browser.post(path, data={"csrf_token": token, "new_password": "Reset-password-789", "new_password_confirm": "Reset-password-789"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.browser.get(path).status_code, 400)
        self.assertEqual(other.get("/account").status_code, 302)
        self.login(password="Reset-password-789")

    def test_logout_get_is_safe_post_revokes_other_browsers_and_api_tokens(self):
        other = self.app.test_client()
        self.login()
        self.login(other)
        with self.app.app_context():
            token = AccessToken(token_hash="0" * 64, account_subject=self.subject, client_id="csupor", version=1, scope="openid", expires_at=int(time()) + 300)
            db.session.add(token)
            db.session.commit()
            self.assertFalse(token.is_revoked())
        csrf = self.csrf(self.browser, "/logout")
        self.assertEqual(other.get("/account").status_code, 200)
        self.assertEqual(self.browser.post("/logout", data={"csrf_token": csrf}).status_code, 302)
        self.assertEqual(other.get("/account").status_code, 302)
        with self.app.app_context():
            self.assertTrue(db.session.get(AccessToken, "0" * 64).is_revoked())

    def test_failed_mail_does_not_expose_transport_or_leave_usable_token(self):
        csrf = self.csrf(self.browser, "/forgot-password")
        with patch("accounts.views.send_reset_email", side_effect=RuntimeError("private-smtp-password")):
            response = self.browser.post("/forgot-password", data={"csrf_token": csrf, "email": "mate@example.invalid"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"private-smtp-password", response.data)
        with self.app.app_context():
            self.assertEqual(db.session.execute(db.select(func.count()).select_from(PasswordReset)).scalar_one(), 0)


if __name__ == "__main__":
    unittest.main()
