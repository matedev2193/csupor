"""Browser credential transfer stays isolated from normal CSUPOR use and HR data."""
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken

from app import create_app, db
from app.models import User, UserPrivilege, UserProfile
from app.password_reset_models import PasswordResetThrottle
from app.shared_auth_models import ExternalIdentity


TRANSFER_PATH = '/developer/shared-accounts'
ISSUER = 'https://accounts.example.test'
TRANSFER_KEY = 'independent-transfer-secret-' + 'x' * 40
PREFIX = b'MICIMACKO-ACCOUNTS-TRANSFER-1\n'


def decrypt_transfer(data, key=TRANSFER_KEY):
    if not data.startswith(PREFIX):
        raise ValueError('Missing transfer envelope')
    cipher_key = base64.urlsafe_b64encode(sha256(
        b'micimacko-account-transfer-v1\0' + key.encode('utf-8'),
    ).digest())
    return json.loads(Fernet(cipher_key).decrypt(data[len(PREFIX):]))


class AccountTransferTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {
            'DATABASE_URL': 'sqlite:///' + str(Path(self.directory.name) / 'browser-transfer.db'),
            'SECRET_KEY': 'browser-transfer-tests-' + 'x' * 40,
            'AUTH_MODE': 'local', 'EMAIL_WORKER_ENABLED': 'false',
            'ACCOUNTS_MIGRATION_MODE': 'false',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in ('INITIAL_ADMIN_USERNAME', 'INITIAL_ADMIN_EMAIL', 'INITIAL_ADMIN_PASSWORD'):
            os.environ.pop(key, None)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.credentials = {}
        with self.app.app_context():
            for username, role in (
                ('developer-user', UserPrivilege.developer),
                ('hr-user', UserPrivilege.hr),
                ('worker-user', UserPrivilege.employee),
            ):
                password = f'{username}-private-password-123'
                user = User(username=username, email=f'{username}@example.test', privilege=role)
                user.set_password(password)
                user.profile = UserProfile(
                    full_name=f'Private HR name of {username}',
                    bank_account_number=f'private-bank-{username}',
                )
                db.session.add(user)
                db.session.flush()
                self.credentials[username] = {
                    'id': user.id, 'password': password, 'hash': user.password_hash,
                    'role': role, 'session_id': user.get_id(),
                }
            db.session.commit()
        self.client = self.login_as('developer-user')

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def login_as(self, username):
        client = self.app.test_client()
        data = {'login': username, 'password': self.credentials[username]['password']}
        if self.app.config.get('ACCOUNTS_MIGRATION_MODE'):
            self.assertEqual(client.get('/login').status_code, 200)
            with client.session_transaction() as browser:
                data['csrf_token'] = browser['account_transfer_csrf']
        response = client.post('/login', data=data)
        self.assertEqual(response.status_code, 302)
        return client

    def enable_transfer(self):
        self.app.config['ACCOUNTS_MIGRATION_MODE'] = True

    def form(self, client=None, **overrides):
        client = client or self.client
        response = client.get(TRANSFER_PATH)
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as browser:
            token = browser['account_transfer_csrf']
        return {
            'csrf_token': token,
            'current_password': self.credentials['developer-user']['password'],
            'issuer': ISSUER,
            'transfer_key': TRANSFER_KEY,
            'maintenance_confirm': '1',
            **overrides,
        }

    def export(self, client=None, **overrides):
        client = client or self.client
        return client.post(TRANSFER_PATH, data=self.form(client, **overrides))

    def assert_private_error(self, response):
        self.assertNotIn('attachment', response.headers.get('Content-Disposition', ''))
        for credential in self.credentials.values():
            self.assertNotIn(credential['password'].encode(), response.data)
            self.assertNotIn(credential['hash'].encode(), response.data)
        self.assertNotIn(TRANSFER_KEY.encode(), response.data)
        self.assertNotIn(b'private-bank-', response.data)
        self.assertNotIn(b'Traceback', response.data)

    def assert_local_records_unchanged(self):
        with self.app.app_context():
            self.assertEqual(User.query.count(), len(self.credentials))
            for username, before in self.credentials.items():
                user = db.session.get(User, before['id'])
                self.assertEqual(user.username, username)
                self.assertEqual(user.password_hash, before['hash'])
                self.assertEqual(user.privilege, before['role'])
                self.assertEqual(user.get_id(), before['session_id'])
                self.assertEqual(user.profile.full_name, f'Private HR name of {username}')
                self.assertEqual(user.profile.bank_account_number, f'private-bank-{username}')

    def test_normal_local_login_remains_available_and_export_requires_maintenance(self):
        self.assertEqual(self.client.get('/dashboard').status_code, 200)
        page = self.client.get(TRANSFER_PATH)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'ACCOUNTS_MIGRATION_MODE', page.data)
        response = self.export()
        self.assertEqual(response.status_code, 409)
        self.assert_private_error(response)
        with self.app.app_context():
            self.assertEqual(ExternalIdentity.query.count(), 0)
        self.assert_local_records_unchanged()

    def test_only_developers_can_view_or_submit_the_transfer(self):
        other_clients = {username: self.login_as(username) for username in ('hr-user', 'worker-user')}
        for flag in (False, True):
            self.app.config['ACCOUNTS_MIGRATION_MODE'] = flag
            with self.subTest(maintenance=flag):
                anonymous = self.app.test_client()
                self.assertEqual(anonymous.get(TRANSFER_PATH).status_code, 302)
                self.assertEqual(anonymous.post(TRANSFER_PATH).status_code, 302)
                for username in ('hr-user', 'worker-user'):
                    other = other_clients[username]
                    self.assertEqual(other.get(TRANSFER_PATH).status_code, 403)
                    self.assertEqual(other.post(TRANSFER_PATH, data={
                        'current_password': self.credentials[username]['password'],
                        'issuer': ISSUER, 'transfer_key': TRANSFER_KEY,
                    }).status_code, 403)
        self.assert_local_records_unchanged()

    def test_maintenance_freezes_existing_sessions_and_anonymous_mutations(self):
        worker = self.login_as('worker-user')
        self.enable_transfer()
        paths = (
            '/dashboard', '/profile', '/profile/edit', '/password', '/register',
            '/forgot-password', '/reset-password', '/admin/users', '/worktime',
            '/profile/email',
            f"/users/{self.credentials['worker-user']['id']}/profile",
            f"/users/{self.credentials['worker-user']['id']}/delete",
        )
        for client in (self.client, worker, self.app.test_client()):
            for method in ('get', 'post'):
                for path in paths:
                    with self.subTest(method=method, path=path):
                        response = getattr(client, method)(path, data={
                            'username': 'forbidden-new-user', 'email': 'forbidden@example.test',
                            'password': 'unwanted-new-password',
                            'current_password': self.credentials['worker-user']['password'],
                            'new_password': 'unwanted-new-password',
                            'new_password_confirm': 'unwanted-new-password',
                        })
                        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.app.test_client().get('/login').status_code, 200)
        self.assertEqual(self.client.get('/login').status_code, 302)
        self.assertNotEqual(self.client.get('/static/unknown-file.css').status_code, 503)
        self.assertNotEqual(self.client.get('/healthz').status_code, 503)
        self.assertEqual(worker.get('/logout').status_code, 302)
        self.assert_local_records_unchanged()

    def test_developer_login_during_maintenance_lands_on_transfer_page(self):
        self.enable_transfer()
        client = self.app.test_client()
        self.assertEqual(client.get('/login').status_code, 200)
        with client.session_transaction() as browser:
            csrf_token = browser['account_transfer_csrf']
        data = {
            'login': 'developer-user',
            'password': self.credentials['developer-user']['password'],
        }
        self.assertEqual(client.post('/login', data=data).status_code, 400)
        self.assertEqual(client.post('/login', data={
            'csrf_token': csrf_token, 'login': 'worker-user',
            'password': self.credentials['worker-user']['password'],
        }).status_code, 400)
        response = client.post('/login', data={**data, 'csrf_token': csrf_token})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(TRANSFER_PATH), response.location)
        self.assertEqual(client.get(response.location).status_code, 200)
        self.assertEqual(client.get('/dashboard').status_code, 503)

    def test_encrypted_export_preserves_identity_credentials_roles_and_hr(self):
        self.enable_transfer()
        response = self.export()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(PREFIX))
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertIn('csupor.accounts-transfer.enc', response.headers['Content-Disposition'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        document = decrypt_transfer(response.data)
        self.assertEqual(set(document), {'version', 'source', 'issuer', 'users'})
        self.assertEqual((document['version'], document['source'], document['issuer']), (1, 'csupor', ISSUER))
        self.assertEqual(len(document['users']), 3)
        rows = {row['username']: row for row in document['users']}
        for username, before in self.credentials.items():
            row = rows[username]
            self.assertEqual(set(row), {'subject', 'username', 'email', 'password_hash'})
            self.assertEqual(row['password_hash'], before['hash'])
            self.assertEqual(row['email'], f'{username}@example.test')
            self.assertNotIn(before['hash'].encode(), response.data)
            self.assertNotIn(before['password'], json.dumps(document))
            with self.app.app_context():
                link = ExternalIdentity.query.filter_by(user_id=before['id']).one()
                self.assertEqual((link.issuer, link.subject, link.disabled), (ISSUER, row['subject'], False))
        self.assertNotIn('Private HR', json.dumps(document))
        self.assertNotIn('private-bank-', json.dumps(document))
        self.assertNotIn(TRANSFER_KEY.encode(), response.data)
        with self.assertRaises(InvalidToken):
            decrypt_transfer(response.data, 'wrong-secret-' + 'x' * 40)
        retry = self.export()
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(decrypt_transfer(retry.data), document)
        self.assertNotEqual(retry.data, response.data)
        self.assert_local_records_unchanged()

    def test_password_proof_is_required_again_for_every_export(self):
        self.enable_transfer()
        self.assertEqual(self.export().status_code, 200)
        for password in ('', 'incorrect-current-password'):
            response = self.export(current_password=password)
            self.assertEqual(response.status_code, 400)
            self.assert_private_error(response)
        self.assertEqual(self.export().status_code, 200)
        self.assert_local_records_unchanged()

    def test_csrf_token_is_required_and_bound_to_the_browser(self):
        self.enable_transfer()
        data = self.form()
        other = self.login_as('developer-user')
        other_data = self.form(other)
        self.assertNotEqual(data['csrf_token'], other_data['csrf_token'])
        for token in ('', 'not-the-current-token', other_data['csrf_token']):
            response = self.client.post(TRANSFER_PATH, data={**data, 'csrf_token': token})
            self.assertEqual(response.status_code, 400)
            self.assert_private_error(response)
        with self.app.app_context():
            self.assertEqual(ExternalIdentity.query.count(), 0)

    def test_issuer_must_be_the_exact_https_origin(self):
        self.enable_transfer()
        invalid = (
            'http://accounts.example.test', 'https://accounts.example.test/path',
            'https://accounts.example.test/', 'https://accounts.example.test?x=1',
            'https://accounts.example.test#fragment', 'https://user:password@accounts.example.test',
            'https://accounts.example.test/../', 'https://accounts.example.test\\evil',
            ' https://accounts.example.test', 'https://accounts.example.test\n',
        )
        for issuer in invalid:
            with self.subTest(issuer=issuer):
                # This case exercises URL validation independently of proof throttling.
                with self.app.app_context():
                    PasswordResetThrottle.query.delete()
                    db.session.commit()
                response = self.export(issuer=issuer)
                self.assertEqual(response.status_code, 400)
                self.assert_private_error(response)
                with self.app.app_context():
                    self.assertEqual(ExternalIdentity.query.count(), 0)

    def test_http_requires_explicit_loopback_development_configuration(self):
        self.enable_transfer()
        loopback = 'http://localhost:9001'
        response = self.export(issuer=loopback)
        self.assertEqual(response.status_code, 400)
        with patch.dict(self.app.config, AUTH_ALLOW_HTTP=True):
            for issuer in ('http://accounts.example.test', 'http://localhost.example.test',
                           'http://127.0.0.1.example.test', 'http://0.0.0.0:9001'):
                with self.subTest(issuer=issuer):
                    response = self.export(issuer=issuer)
                    self.assertEqual(response.status_code, 400)
                    self.assert_private_error(response)
            response = self.export(issuer=loopback)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(decrypt_transfer(response.data)['issuer'], loopback)
        self.assert_local_records_unchanged()

    def test_transfer_key_length_and_explicit_maintenance_confirmation(self):
        self.enable_transfer()
        for key in ('s' * 31, 'l' * 1025):
            response = self.export(transfer_key=key)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(key.encode(), response.data)
        response = self.export(maintenance_confirm='')
        self.assertEqual(response.status_code, 400)
        self.assert_private_error(response)
        for key in ('s' * 32, 'l' * 1024):
            response = self.export(transfer_key=key)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(decrypt_transfer(response.data, key)['issuer'], ISSUER)
        self.assert_local_records_unchanged()

    def test_conflicting_identity_rolls_back_new_links_without_exposing_secrets(self):
        self.enable_transfer()
        with self.app.app_context():
            db.session.add(ExternalIdentity(
                issuer='https://other-issuer.example.test', subject='existing-other-subject',
                user_id=self.credentials['worker-user']['id'],
            ))
            db.session.commit()
        response = self.export()
        self.assertEqual(response.status_code, 400)
        self.assert_private_error(response)
        self.assertNotIn(b'existing-other-subject', response.data)
        with self.app.app_context():
            links = ExternalIdentity.query.all()
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].subject, 'existing-other-subject')
        self.assert_local_records_unchanged()

    def test_oversized_export_rolls_back_staged_identity_links(self):
        self.enable_transfer()
        # Exercise the size guard with a small test-only limit, without allocating
        # tens of megabytes or manufacturing thousands of real account records.
        with patch('app.account_transfer.MAX_PLAINTEXT_BYTES', 10):
            response = self.export()
        self.assertEqual(response.status_code, 400)
        self.assert_private_error(response)
        with self.app.app_context():
            self.assertEqual(ExternalIdentity.query.count(), 0)
        self.assert_local_records_unchanged()

    def test_password_attempt_limit_persists_across_new_browser_sessions(self):
        self.enable_transfer()
        for _ in range(5):
            response = self.export(current_password='wrong-proof-secret')
            self.assertEqual(response.status_code, 400)
        fresh_client = self.login_as('developer-user')
        response = self.export(fresh_client)
        self.assertEqual(response.status_code, 429)
        self.assert_private_error(response)
        with self.app.app_context():
            self.assertEqual(ExternalIdentity.query.count(), 0)
            limits = PasswordResetThrottle.query.all()
            self.assertTrue(limits)
            self.assertTrue(any(limit.request_count >= 5 for limit in limits))
            for limit in limits:
                self.assertRegex(limit.key_hash, r'^[0-9a-f]{64}$')
        self.assert_local_records_unchanged()

    def test_oidc_mode_ignores_maintenance_flag_and_disables_export(self):
        self.app.config.update(
            AUTH_MODE='oidc', ACCOUNTS_MIGRATION_MODE=True,
            OIDC_ISSUER=ISSUER,
        )
        # No network or identity provider is necessary to verify routing policy.
        with patch('app.routes.begin_login', return_value=('Central sign-in', 302)) as start_login:
            response = self.app.test_client().get('/login')
            self.assertEqual(response.status_code, 302)
            start_login.assert_called_once()
        response = self.app.test_client().post('/forgot-password', data={'email': 'someone@example.test'})
        self.assertNotEqual(response.status_code, 503)
        self.assertEqual(response.location, ISSUER + '/forgot-password')
        for client in (self.client, self.app.test_client()):
            self.assertEqual(client.get(TRANSFER_PATH).status_code, 404)
            self.assertEqual(client.post(TRANSFER_PATH).status_code, 404)
        self.app.config['AUTH_MODE'] = 'local'
        self.assert_local_records_unchanged()


if __name__ == '__main__':
    unittest.main()
