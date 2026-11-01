"""Real HTTP OIDC round trips: independent databases, signatures and client grants."""

import hashlib
import base64
from html.parser import HTMLParser
import os
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
import requests
from sqlalchemy import event
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server, WSGIRequestHandler

from accounts import create_app as create_accounts_app
from accounts.models import Account, Client, Membership, db as accounts_db
from app import create_app, db
from app.models import User, UserPrivilege, UserProfile
from app.shared_auth_models import ExternalIdentity, ExternalLoginSession


class _HiddenInputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.continuation = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
            self.values[attrs["name"]] = attrs.get("value", "")
        if tag == "a" and attrs.get("id") == "login-continuation":
            self.continuation = attrs["href"]


class _QuietHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        pass


class SharedAccountsEndToEndTests(unittest.TestCase):
    """Use production Authlib paths; never replace JWT or callback validation."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {
            "AUTHLIB_INSECURE_TRANSPORT": "1",
            "EMAIL_WORKER_ENABLED": "false",
            "INITIAL_ADMIN_USERNAME": "",
            "INITIAL_ADMIN_EMAIL": "",
            "INITIAL_ADMIN_PASSWORD": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ("INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_EMAIL", "INITIAL_ADMIN_PASSWORD"):
            os.environ.pop(name, None)

        self.provider_server = make_server("127.0.0.1", 0, Flask("pending-provider"), threaded=True,
                                           request_handler=_QuietHandler)
        self.rp_server = make_server("127.0.0.1", 0, Flask("pending-rp"), threaded=True,
                                     request_handler=_QuietHandler)
        self.issuer = f"http://127.0.0.1:{self.provider_server.server_port}"
        self.rp_url = f"http://127.0.0.1:{self.rp_server.server_port}"
        self.callback = self.rp_url + "/auth/callback"
        self.other_callback = self.rp_url + "/other-system/callback"
        self.subject = str(uuid4())
        self.password = "Existing-worker-password-123"
        self.password_hash = generate_password_hash(self.password, method="pbkdf2:sha256:1000")

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path = Path(self.directory.name) / "signing.pem"
        key_path.write_bytes(private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        self.provider = create_accounts_app({
            "TESTING": True,
            "ACCOUNTS_DATABASE_URL": "sqlite:///" + str(Path(self.directory.name) / "accounts.db"),
            "ACCOUNTS_SECRET_KEY": "accounts-test-secret-" + "a" * 40,
            "ACCOUNTS_ISSUER": self.issuer,
            "ACCOUNTS_SIGNING_KEY_FILE": str(key_path),
            "ACCOUNTS_ALLOW_HTTP": True,
        })
        with self.provider.app_context():
            self._enable_foreign_keys(accounts_db.engine)
            accounts_db.create_all()
            account = Account(subject=self.subject, username="existing-worker",
                              email="worker@example.invalid", password_hash=self.password_hash)
            csupor = Client(client_id="csupor", name="CSUPOR", redirect_uris=[self.callback])
            csupor.set_secret("csupor-client-secret-" + "a" * 32)
            other = Client(client_id="work-safety", name="Munkavédelem", redirect_uris=[self.other_callback])
            other.set_secret("other-client-secret-" + "b" * 32)
            accounts_db.session.add_all([account, csupor, other])
            accounts_db.session.flush()
            accounts_db.session.add_all([
                Membership(account_subject=self.subject, client_id="csupor"),
                Membership(account_subject=self.subject, client_id="work-safety"),
            ])
            accounts_db.session.commit()

        with patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(Path(self.directory.name) / "csupor.db"),
            "SECRET_KEY": "csupor-test-secret-" + "b" * 40,
            "AUTH_MODE": "oidc",
            "OIDC_ISSUER": self.issuer,
            "OIDC_CLIENT_ID": "csupor",
            "OIDC_CLIENT_SECRET": "csupor-client-secret-" + "a" * 32,
            "OIDC_REDIRECT_URI": self.callback,
            "AUTH_ALLOW_HTTP": "true",
        }):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        with self.app.app_context():
            self._enable_foreign_keys(db.engine)
            worker = User(username="existing-worker", email="worker@example.invalid",
                          password_hash=self.password_hash, privilege=UserPrivilege.hr)
            worker.profile = UserProfile(full_name="Meglévő Munkatárs")
            db.session.add(worker)
            db.session.flush()
            self.user_id = worker.id
            db.session.add(ExternalIdentity(issuer=self.issuer, subject=self.subject, user_id=worker.id))
            db.session.commit()

        for server, app in ((self.provider_server, self.provider), (self.rp_server, self.app)):
            server.app = app
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(self._stop_server, server, thread)
        self.browser = requests.Session()
        self.addCleanup(self.browser.close)
        self.addCleanup(self._dispose_databases)

    @staticmethod
    def _stop_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    @staticmethod
    def _enable_foreign_keys(engine):
        def configure(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
        event.listen(engine, "connect", configure)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    def _dispose_databases(self):
        for app, database in ((self.app, db), (self.provider, accounts_db)):
            with app.app_context():
                database.session.remove()
                database.engine.dispose()

    def request(self, method, url, **kwargs):
        return self.browser.request(method, url, timeout=10, **kwargs)

    def sign_in(self, *, finish=True):
        response = self.request("GET", self.rp_url + "/login")
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(urlsplit(response.url).path, "/login")
        self.assertEqual(urlsplit(response.url).netloc, urlsplit(self.issuer).netloc)
        form = _HiddenInputs(response.text).values
        self.assertIn("csrf_token", form)
        form.update(login="existing-worker", password=self.password)
        response = self.request("POST", response.url, data=form, allow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text[:500])
        response = self.continue_sign_in(response, finish=finish)
        if not finish:
            return response
        self.assertEqual(response.status_code, 200, response.text[:700])
        self.assertEqual(urlsplit(response.url).netloc, urlsplit(self.rp_url).netloc)
        return response

    def continue_sign_in(self, response, *, finish=True):
        # Simulate the same-origin continuation page's normal JS navigation.
        # Real Chromium coverage separately verifies its CSP/browser behaviour.
        # Follow the central /authorize step but leave the RP callback untouched
        # when a test needs to alter its state or replay the returned code.
        for _ in range(12):
            target = response.headers["Location"] if response.is_redirect else _HiddenInputs(response.text).continuation
            if not target:
                return response
            destination = urljoin(response.url, target)
            if destination.startswith(self.callback) and not finish:
                return destination
            response = self.request("GET", destination, allow_redirects=False)
        self.fail("Sign-in redirect loop")

    def other_authorization(self, *, challenge_verifier=None, **overrides):
        verifier = challenge_verifier or secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {
            "response_type": "code", "client_id": "work-safety", "redirect_uri": self.other_callback,
            "scope": "openid email profile", "state": secrets.token_urlsafe(24),
            "nonce": secrets.token_urlsafe(24), "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        params.update(overrides)
        response = self.request("GET", self.issuer + "/authorize", params=params, allow_redirects=False)
        return response, verifier, params

    def exchange_other_code(self, code, verifier):
        return self.request("POST", self.issuer + "/token", auth=("work-safety", "other-client-secret-" + "b" * 32),
                            data={"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": self.other_callback, "code_verifier": verifier})

    def test_existing_password_and_identity_work_in_two_separate_systems(self):
        self.sign_in()
        cookie_names = {cookie.name for cookie in self.browser.cookies}
        self.assertIn("csupor_session", cookie_names)
        self.assertIn("micimacko_accounts", cookie_names)
        rp_cookie = next(cookie for cookie in self.browser.cookies if cookie.name == "csupor_session")
        browser_values = self.app.session_interface.get_signing_serializer(self.app).loads(rp_cookie.value)
        for secret_field in ("access_token", "id_token", "refresh_token", "password", "password_hash"):
            self.assertNotIn(secret_field, repr(browser_values))
        with self.app.app_context():
            worker = db.session.get(User, self.user_id)
            self.assertEqual(worker.privilege, UserPrivilege.hr)
            self.assertEqual(worker.profile.full_name, "Meglévő Munkatárs")
            self.assertEqual(User.query.count(), 1)
        response, verifier, _ = self.other_authorization()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith(self.other_callback))
        code = parse_qs(urlsplit(response.headers["Location"]).query)["code"][0]
        token = self.exchange_other_code(code, verifier)
        self.assertEqual(token.status_code, 200, token.text)
        info = self.request("GET", self.issuer + "/userinfo",
                            headers={"Authorization": "Bearer " + token.json()["access_token"]})
        self.assertEqual(info.status_code, 200, info.text)
        self.assertEqual(info.json()["sub"], self.subject)
        self.assertEqual(info.json()["email"], "worker@example.invalid")
        self.assertFalse(info.json().get("email_verified", False))
        for private_field in ("password_hash", "privilege", "contracts", "full_name", "tax_number"):
            self.assertNotIn(private_field, info.json())

    def test_one_post_logs_out_csupor_and_shared_account(self):
        page = self.sign_in()
        csrf = _HiddenInputs(page.text).values['csrf_token']
        response = self.request('POST', self.rp_url + '/logout', data={'csrf_token': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(urlsplit(response.url).netloc, urlsplit(self.issuer).netloc)
        self.assertEqual(urlsplit(response.url).path, '/login')
        self.assertNotIn('Kijelentkezés a közös fiókból', response.text)
        with self.provider.app_context():
            self.assertEqual(accounts_db.session.get(Account, self.subject).version, 2)
        with self.app.app_context():
            self.assertEqual(ExternalLoginSession.query.count(), 0)
        response = self.request('GET', self.rp_url + '/login')
        self.assertEqual(urlsplit(response.url).path, '/login')
        self.assertEqual(urlsplit(response.url).netloc, urlsplit(self.issuer).netloc)

    def test_get_and_bad_csrf_cannot_log_out_shared_account(self):
        self.sign_in()
        self.assertEqual(self.request('GET', self.rp_url + '/logout').status_code, 200)
        denied = self.request('POST', self.rp_url + '/logout', data={'csrf_token': 'wrong'})
        self.assertEqual(denied.status_code, 400)
        with self.provider.app_context():
            self.assertEqual(accounts_db.session.get(Account, self.subject).version, 1)
        with self.app.app_context():
            self.assertEqual(ExternalLoginSession.query.count(), 1)
        self.assertEqual(self.request('GET', self.rp_url + '/dashboard').status_code, 200)

    def test_logout_outage_clears_local_session_and_reports_incomplete_global_logout(self):
        page = self.sign_in()
        csrf = _HiddenInputs(page.text).values['csrf_token']
        with patch('app.shared_auth.requests.post', side_effect=requests.ConnectionError('private-bearer')):
            response = self.request('POST', self.rp_url + '/logout', data={'csrf_token': csrf})
        self.assertEqual(response.status_code, 503)
        self.assertIn('Kijelentkezés befejezése a fiókkezelőben', response.text)
        self.assertNotIn('private-bearer', response.text)
        with self.app.app_context():
            self.assertEqual(ExternalLoginSession.query.count(), 0)
        with self.provider.app_context():
            self.assertEqual(accounts_db.session.get(Account, self.subject).version, 1)

    def test_new_registration_grants_only_the_requested_application(self):
        with self.provider.app_context():
            accounts_db.session.get(Client, "csupor").allow_registration = True
            accounts_db.session.commit()
        response = self.request("GET", self.rp_url + "/register")
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(urlsplit(response.url).path, "/register")
        form = _HiddenInputs(response.text).values
        form.update(username="new-worker", email="new-worker@example.invalid",
                    password="New-central-password-123", password_confirm="New-central-password-123")
        response = self.continue_sign_in(self.request("POST", response.url, data=form))
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(urlsplit(response.url).netloc, urlsplit(self.rp_url).netloc)
        with self.app.app_context():
            new_worker = User.query.filter_by(username="new-worker").one()
            self.assertEqual(new_worker.privilege, UserPrivilege.employee)
            new_subject = ExternalIdentity.query.filter_by(user_id=new_worker.id).one().subject
        response, _, _ = self.other_authorization()
        self.assertEqual(response.status_code, 403)
        with self.provider.app_context():
            self.assertIsNotNone(accounts_db.session.get(Account, new_subject))
            self.assertIsNone(accounts_db.session.get(Membership, (new_subject, "work-safety")))
            accounts_db.session.add(Membership(account_subject=new_subject, client_id="work-safety"))
            accounts_db.session.commit()
        response, verifier, _ = self.other_authorization()
        self.assertEqual(response.status_code, 302)
        code = parse_qs(urlsplit(response.headers["Location"]).query)["code"][0]
        token = self.exchange_other_code(code, verifier)
        self.assertEqual(token.status_code, 200, token.text)
        info = self.request("GET", self.issuer + "/userinfo",
                            headers={"Authorization": "Bearer " + token.json()["access_token"]})
        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.json()["sub"], new_subject)

    def test_code_is_single_use_and_wrong_pkce_verifier_is_rejected(self):
        self.sign_in()
        response, verifier, _ = self.other_authorization()
        code = parse_qs(urlsplit(response.headers["Location"]).query)["code"][0]
        rejected = self.exchange_other_code(code, "incorrect-" + "x" * 48)
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn(rejected.json()["error"], ("invalid_grant", "invalid_request"))
        response, verifier, _ = self.other_authorization()
        code = parse_qs(urlsplit(response.headers["Location"]).query)["code"][0]
        self.assertEqual(self.exchange_other_code(code, verifier).status_code, 200)
        self.assertEqual(self.exchange_other_code(code, verifier).status_code, 400)

    def test_unregistered_redirect_and_plain_pkce_are_rejected(self):
        self.sign_in()
        response, _, _ = self.other_authorization(redirect_uri="https://attacker.example/callback")
        self.assertFalse(response.headers.get("Location", "").startswith("https://attacker.example"))
        self.assertEqual(response.status_code, 400)
        response, _, _ = self.other_authorization(code_challenge_method="plain")
        self.assertNotIn("code=", response.headers.get("Location", ""))
        self.assertIn(response.status_code, (400, 302))

    def test_revoking_client_membership_revokes_that_app_session(self):
        self.sign_in()
        with self.provider.app_context():
            Membership.query.filter_by(account_subject=self.subject, client_id="csupor").delete()
            accounts_db.session.commit()
        response = self.request("GET", self.rp_url + "/dashboard", allow_redirects=False)
        self.assertIn(response.status_code, (302, 401, 403))
        self.assertNotIn("Meglévő Munkatárs", response.text)
        response, _, _ = self.other_authorization()
        self.assertEqual(response.status_code, 302)
        self.assertIn("code=", response.headers.get("Location", ""))

    def test_password_change_revokes_existing_rp_session(self):
        self.sign_in()
        with self.provider.app_context():
            account = accounts_db.session.get(Account, self.subject)
            account.set_password("Replacement-central-password-456")
            accounts_db.session.commit()
        response = self.request("GET", self.rp_url + "/dashboard", allow_redirects=False)
        self.assertIn(response.status_code, (302, 401, 403))
        self.assertNotIn("Meglévő Munkatárs", response.text)

    def test_provider_outage_closes_existing_session_without_local_fallback(self):
        self.sign_in()
        self.provider_server.shutdown()
        self.provider_server.server_close()
        response = self.request("GET", self.rp_url + "/dashboard", allow_redirects=False)
        self.assertIn(response.status_code, (302, 401, 403, 503))
        self.assertNotIn("Meglévő Munkatárs", response.text)

    def test_administrator_deletion_requires_fresh_central_login_and_confirmation(self):
        target_subject = str(uuid4())
        with self.provider.app_context():
            accounts_db.session.add(Account(subject=target_subject, username="target-worker",
                                            email="target-worker@example.invalid", password_hash=self.password_hash))
            accounts_db.session.commit()
        with self.app.app_context():
            target = User(username="target-worker", email="target-worker@example.invalid",
                          password_hash=self.password_hash, privilege=UserPrivilege.employee)
            target.profile = UserProfile(full_name="Target worker")
            db.session.add(target)
            db.session.flush()
            target_id = target.id
            db.session.add(ExternalIdentity(issuer=self.issuer, subject=target_subject, user_id=target_id))
            db.session.commit()
        self.sign_in()
        response = self.request("GET", self.rp_url + "/users/profiles?status=all")
        csrf = _HiddenInputs(response.text).values["csrf_token"]
        delete_url = self.rp_url + f"/users/{target_id}/delete"
        response = self.request("POST", delete_url, data={"csrf_token": csrf,
                                "manager_password": self.password})
        # Even a correct historical local password cannot authorise deletion.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(urlsplit(response.url).netloc, urlsplit(self.issuer).netloc)
        self.assertEqual(urlsplit(response.url).path, "/login")
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(User, target_id))
        form = _HiddenInputs(response.text).values
        form.update(login="existing-worker", password=self.password)
        response = self.continue_sign_in(self.request("POST", response.url, data=form))
        self.assertEqual(response.status_code, 200, response.text[:700])
        self.assertEqual(urlsplit(response.url).path, "/auth/callback")
        self.assertIn("target-worker", response.text)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(User, target_id))
        confirmation = _HiddenInputs(response.text).values
        cookie_name = self.app.config["SESSION_COOKIE_NAME"]
        replay_cookie = next(item for item in self.browser.cookies if item.name == cookie_name)
        replay_value = replay_cookie.value
        response = self.request("POST", delete_url, data=confirmation)
        self.assertEqual(response.status_code, 200, response.text[:700])
        with self.app.app_context():
            self.assertIsNone(db.session.get(User, target_id))
            self.assertTrue(ExternalIdentity.query.filter_by(subject=target_subject).one().disabled)
        with self.provider.app_context():
            self.assertIsNotNone(accounts_db.session.get(Account, target_subject))
        # A stolen old signed browser cookie must not replay the one-time
        # deletion approval, including if a database reuses a deleted user ID.
        with self.app.app_context():
            db.session.add(User(id=target_id, username="different-worker",
                                email="different-worker@example.invalid", password_hash=self.password_hash))
            db.session.commit()
        self.browser.cookies.set(replay_cookie.name, replay_value,
                                 domain=replay_cookie.domain, path=replay_cookie.path)
        response = self.request("POST", delete_url, data=confirmation)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(urlsplit(response.url).netloc, urlsplit(self.issuer).netloc)
        self.assertEqual(urlsplit(response.url).path, "/login")
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(User, target_id))

    def test_mismatched_nonce_and_callback_replay_are_rejected(self):
        callback = self.sign_in(finish=False)
        state = parse_qs(urlsplit(callback).query)["state"][0]
        cookie_name = self.app.config["SESSION_COOKIE_NAME"]
        cookie = next(item for item in self.browser.cookies if item.name == cookie_name)
        serializer = self.app.session_interface.get_signing_serializer(self.app)
        values = serializer.loads(cookie.value)
        values[f"_state_accounts_{state}"]["data"]["nonce"] = "different-expected-nonce-1234567890"
        self.browser.cookies.set(cookie.name, serializer.dumps(values), domain=cookie.domain, path=cookie.path)
        response = self.request("GET", callback, allow_redirects=False)
        self.assertEqual(response.status_code, 400, response.text[:500])
        response = self.request("GET", callback, allow_redirects=False)
        self.assertEqual(response.status_code, 400, response.text[:500])
        response = self.request("GET", self.rp_url + "/dashboard", allow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_unrequested_callback_with_valid_looking_state_is_rejected(self):
        response = self.request("GET", self.callback, params={"code": "forged-code", "state": "unrequested-state"})
        self.assertEqual(response.status_code, 400)
        response = self.request("GET", self.rp_url + "/dashboard", allow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_local_deletion_does_not_delete_central_account_or_restore_access(self):
        self.sign_in()
        with self.app.app_context():
            from app.account_deletion import delete_user_account
            delete_user_account(db.session.get(User, self.user_id))
            db.session.commit()
            mapping = ExternalIdentity.query.filter_by(issuer=self.issuer, subject=self.subject).one()
            self.assertTrue(mapping.disabled)
            self.assertIsNone(mapping.user_id)
        with self.provider.app_context():
            self.assertIsNotNone(accounts_db.session.get(Account, self.subject))
        response = self.request("GET", self.rp_url + "/login")
        self.assertEqual(response.status_code, 403, response.text[:500])
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)

    def test_existing_account_cannot_be_adopted_by_matching_email(self):
        with self.app.app_context():
            ExternalIdentity.query.delete()
            db.session.commit()
        # The provider still knows the same name/email, but without an explicit
        # migrated (issuer, subject) binding these are not proof of ownership.
        response = self.request("GET", self.rp_url + "/login")
        form = _HiddenInputs(response.text).values
        form.update(login="existing-worker", password=self.password)
        response = self.continue_sign_in(self.request("POST", response.url, data=form))
        self.assertEqual(response.status_code, 403, response.text[:500])
        with self.app.app_context():
            self.assertEqual(User.query.count(), 1)
            self.assertEqual(db.session.get(User, self.user_id).privilege, UserPrivilege.hr)
            self.assertEqual(ExternalIdentity.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
