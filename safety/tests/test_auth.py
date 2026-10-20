"""Real shared-account sign-in and isolated training permissions."""
from datetime import timedelta
from html.parser import HTMLParser
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
import requests
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server, WSGIRequestHandler

from accounts import create_app as create_accounts
from accounts.models import Account, Client, Membership, db as accounts_db
from safety import create_app
from safety.models import LoginSession, Member, db, now


class _HiddenInputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('type') == 'hidden' and attrs.get('name'):
            self.values[attrs['name']] = attrs.get('value', '')


class _QuietHandler(WSGIRequestHandler):
    def log_request(self, code='-', size='-'):
        pass


class SafetyAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {'AUTHLIB_INSECURE_TRANSPORT': '1'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.provider_server = make_server('127.0.0.1', 0, Flask('pending-provider'), threaded=True, request_handler=_QuietHandler)
        self.rp_server = make_server('127.0.0.1', 0, Flask('pending-rp'), threaded=True, request_handler=_QuietHandler)
        self.issuer = f'http://127.0.0.1:{self.provider_server.server_port}'
        self.rp_url = f'http://127.0.0.1:{self.rp_server.server_port}'
        self.callback = self.rp_url + '/auth/callback'
        self.subject = str(uuid4())
        self.password = 'Existing-worker-password-123'
        self.secret = 'separate-client-secret-' + 'c' * 32
        key_path = Path(self.directory.name) / 'signing.pem'
        key_path.write_bytes(self.pem)
        self.provider = create_accounts({
            'TESTING': True, 'ACCOUNTS_DATABASE_URL': 'sqlite:///' + self.directory.name + '/accounts.db',
            'ACCOUNTS_SECRET_KEY': 'account-test-secret-' + 'a' * 40,
            'ACCOUNTS_ISSUER': self.issuer, 'ACCOUNTS_SIGNING_KEY_FILE': str(key_path),
            'ACCOUNTS_ALLOW_HTTP': True,
        })
        with self.provider.app_context():
            accounts_db.create_all()
            account = Account(subject=self.subject, username='existing-worker', email='worker@example.invalid',
                              password_hash=generate_password_hash(self.password, method='pbkdf2:sha256:1000'))
            client = Client(client_id='munkavedelem', name='Tűz- és munkavédelem', redirect_uris=[self.callback])
            client.set_secret(self.secret)
            accounts_db.session.add_all([account, client])
            accounts_db.session.flush()
            accounts_db.session.add(Membership(account_subject=self.subject, client_id='munkavedelem'))
            accounts_db.session.commit()
        self.config = {
            'TESTING': True, 'SECRET_KEY': 'safety-test-secret-' + 's' * 40,
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + self.directory.name + '/safety.db',
            'SAFETY_UPLOAD_DIR': self.directory.name + '/uploads',
            'SAFETY_OIDC_ISSUER': self.issuer, 'SAFETY_OIDC_CLIENT_ID': 'munkavedelem',
            'SAFETY_OIDC_CLIENT_SECRET': self.secret, 'SAFETY_OIDC_REDIRECT_URI': self.callback,
            'SAFETY_ALLOW_HTTP': True,
        }
        self.app = create_app(self.config)
        with self.app.app_context():
            db.create_all()
        for server, app in ((self.provider_server, self.provider), (self.rp_server, self.app)):
            server.app = app
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(self._stop_server, server, thread)
        self.browser = requests.Session()
        self.addCleanup(self.browser.close)
        self.addCleanup(self._dispose_databases)

    def _dispose_databases(self):
        for app, database in ((self.app, db), (self.provider, accounts_db)):
            with app.app_context():
                database.session.remove()
                database.engine.dispose()

    @staticmethod
    def _stop_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    def request(self, method, url, **kwargs):
        return self.browser.request(method, url, timeout=10, **kwargs)

    def sign_in(self, *, finish=True):
        response = self.request('GET', self.rp_url + '/auth/login')
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(urlsplit(response.url).netloc, urlsplit(self.issuer).netloc)
        form = _HiddenInputs(response.text).values
        form.update(login='existing-worker', password=self.password)
        response = self.request('POST', response.url, data=form, allow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text[:500])
        while response.is_redirect:
            destination = urljoin(response.url, response.headers['Location'])
            if destination.startswith(self.callback) and not finish:
                return destination
            response = self.request('GET', destination, allow_redirects=False)
        return response

    def test_existing_account_gets_employee_access_with_server_side_token(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, 200, response.text[:500])
        with self.app.app_context():
            member = Member.query.one()
            self.assertEqual(member.subject, self.subject)
            self.assertEqual(member.role, 'employee')
            self.assertEqual(member.email, 'worker@example.invalid')
            saved = LoginSession.query.one()
            self.assertTrue(saved.access_token.startswith('gAAAA'))
        serializer = self.app.session_interface.get_signing_serializer(self.app)
        cookie = next(c for c in self.browser.cookies if c.name == 'micimacko_safety')
        values = serializer.loads(cookie.value)
        self.assertIn('safety_login', values)
        self.assertNotIn('access_token', values)
        self.assertNotIn('id_token', values)
        response = self.request('GET', self.rp_url + '/admin', allow_redirects=False)
        self.assertEqual(response.status_code, 403)

    def test_existing_local_admin_and_display_name_are_retained(self):
        with self.app.app_context():
            db.session.add(Member(issuer=self.issuer, subject=self.subject, name='Saját megjelenített név',
                                  email='old@example.invalid', role='admin'))
            db.session.commit()
        response = self.sign_in()
        self.assertEqual(response.status_code, 200, response.text[:500])
        response = self.request('GET', self.rp_url + '/admin')
        self.assertEqual(response.status_code, 200, response.text[:500])
        with self.app.app_context():
            member = Member.query.one()
            self.assertEqual(member.name, 'Saját megjelenített név')
            self.assertEqual(member.role, 'admin')
            self.assertEqual(member.email, 'worker@example.invalid')

    def test_matching_email_never_adopts_another_local_identity_or_role(self):
        other_subject = str(uuid4())
        with self.app.app_context():
            db.session.add(Member(issuer=self.issuer, subject=other_subject, name='Existing admin',
                                  email='worker@example.invalid', role='admin'))
            db.session.commit()
        response = self.sign_in()
        self.assertEqual(response.status_code, 200, response.text[:500])
        with self.app.app_context():
            self.assertEqual(Member.query.count(), 2)
            self.assertEqual(Member.query.filter_by(subject=self.subject).one().role, 'employee')
            self.assertEqual(Member.query.filter_by(subject=other_subject).one().role, 'admin')

    def test_revoked_central_membership_denies_existing_session(self):
        self.assertEqual(self.sign_in().status_code, 200)
        with self.provider.app_context():
            Membership.query.filter_by(account_subject=self.subject, client_id='munkavedelem').delete()
            accounts_db.session.commit()
        response = self.request('GET', self.rp_url + '/', allow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/auth/login', response.headers['Location'])

    def test_local_deactivation_denies_session_and_reauthentication(self):
        self.assertEqual(self.sign_in().status_code, 200)
        with self.app.app_context():
            Member.query.one().active = False
            db.session.commit()
        self.assertEqual(self.request('GET', self.rp_url + '/', allow_redirects=False).status_code, 302)
        # The provider has an active session, but the local disabled identity
        # must stay disabled after a fresh successful OIDC round trip.
        response = self.request('GET', self.rp_url + '/auth/login')
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertEqual(Member.query.count(), 1)
            self.assertFalse(Member.query.one().active)

    def test_password_change_revokes_session(self):
        self.assertEqual(self.sign_in().status_code, 200)
        with self.provider.app_context():
            accounts_db.session.get(Account, self.subject).set_password('changed-password-123')
            accounts_db.session.commit()
        self.assertEqual(self.request('GET', self.rp_url + '/', allow_redirects=False).status_code, 302)

    @unittest.skipUnless(hasattr(time, 'tzset'), 'POSIX timezone regression')
    def test_sign_in_uses_utc_even_when_server_timezone_is_not_utc(self):
        try:
            with patch.dict(os.environ, {'TZ': 'America/New_York'}):
                time.tzset()
                response = self.sign_in()
                self.assertEqual(response.status_code, 200, response.text[:500])
                with self.app.app_context():
                    remaining = (LoginSession.query.one().expires_at - now()).total_seconds()
                    self.assertGreater(remaining, 3500)
                    self.assertLessEqual(remaining, 3600)
        finally:
            time.tzset()

    def test_provider_outage_denies_access_without_local_fallback(self):
        self.assertEqual(self.sign_in().status_code, 200)
        self.provider_server.shutdown()
        self.provider_server.server_close()
        self.assertEqual(self.request('GET', self.rp_url + '/', allow_redirects=False).status_code, 302)

    def test_expired_local_session_is_not_accepted(self):
        self.assertEqual(self.sign_in().status_code, 200)
        with self.app.app_context():
            LoginSession.query.one().expires_at = now() - timedelta(seconds=1)
            db.session.commit()
        self.assertEqual(self.request('GET', self.rp_url + '/', allow_redirects=False).status_code, 302)

    def test_nonce_and_state_replay_are_rejected(self):
        callback = self.sign_in(finish=False)
        state = parse_qs(urlsplit(callback).query)['state'][0]
        cookie = next(c for c in self.browser.cookies if c.name == 'micimacko_safety')
        serializer = self.app.session_interface.get_signing_serializer(self.app)
        values = serializer.loads(cookie.value)
        values[f'_state_accounts_{state}']['data']['nonce'] = 'different-nonce-0123456789'
        self.browser.cookies.set(cookie.name, serializer.dumps(values), domain=cookie.domain, path=cookie.path)
        self.assertEqual(self.request('GET', callback, allow_redirects=False).status_code, 400)
        self.assertEqual(self.request('GET', callback, allow_redirects=False).status_code, 400)
        with self.app.app_context():
            self.assertEqual(Member.query.count(), 0)
            self.assertEqual(LoginSession.query.count(), 0)

    def test_forged_callback_has_no_session(self):
        response = self.request('GET', self.callback, params={'code': 'forged-code', 'state': 'forged-state'})
        self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertEqual(Member.query.count(), 0)

    def test_logout_requires_csrf_and_removes_local_token(self):
        self.assertEqual(self.sign_in().status_code, 200)
        response = self.request('POST', self.rp_url + '/auth/logout', allow_redirects=False)
        self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertEqual(LoginSession.query.count(), 1)
        response = self.request('GET', self.rp_url + '/')
        token = _HiddenInputs(response.text).values['csrf_token']
        response = self.request('POST', self.rp_url + '/auth/logout', data={'csrf_token': token}, allow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], self.issuer + '/logout')
        with self.app.app_context():
            self.assertEqual(LoginSession.query.count(), 0)

    def test_operator_commands_are_isolated_and_do_not_escalate_by_email(self):
        runner = self.app.test_cli_runner()
        self.assertEqual(runner.invoke(args=['init-db']).exit_code, 0)
        subject = str(uuid4())
        result = runner.invoke(args=['set-admin', subject, '--name', 'Admin név', '--email', 'worker@example.invalid'])
        self.assertEqual(result.exit_code, 0, result.output)
        other = str(uuid4())
        result = runner.invoke(args=['add-member', other, '--name', 'Másik kolléga', '--email', 'worker@example.invalid'])
        self.assertEqual(result.exit_code, 0, result.output)
        result = runner.invoke(args=['set-admin', subject, '--remove'])
        self.assertNotEqual(result.exit_code, 0)
        with self.app.app_context():
            self.assertEqual(Member.query.filter_by(subject=subject).one().role, 'admin')
            self.assertEqual(Member.query.filter_by(subject=other).one().role, 'employee')
        with self.provider.app_context():
            self.assertEqual(Account.query.count(), 1)
            self.assertEqual(Membership.query.count(), 1)

    def test_invalid_public_http_and_wrong_host_are_rejected(self):
        with self.assertRaises(RuntimeError):
            create_app({**self.config, 'SAFETY_OIDC_ISSUER': 'http://public.example.invalid'})
        self.assertEqual(self.app.test_client().get('/', base_url='https://evil.example.invalid').status_code, 400)


if __name__ == '__main__':
    unittest.main()
