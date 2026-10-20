"""Safe, repeatable identity import and isolated provider configuration."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import inspect
from werkzeug.security import generate_password_hash

from accounts import create_app
from accounts.models import Account, Client, Membership, db


class OperatorCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Path(self.directory.name) / 'key.pem'
        self.key.write_bytes(self.pem)
        self.config = {
            'TESTING': True, 'ACCOUNTS_DATABASE_URL': 'sqlite:///' + self.directory.name + '/accounts.db',
            'ACCOUNTS_SECRET_KEY': 'independent-account-service-test-secret',
            'ACCOUNTS_ISSUER': 'https://accounts.example.invalid',
            'ACCOUNTS_SIGNING_KEY_FILE': str(self.key),
        }
        self.app = create_app(self.config)
        self.runner = self.app.test_cli_runner()
        self.assertEqual(self.runner.invoke(args=['init-db']).exit_code, 0)
        with patch.dict(os.environ, {'ACCOUNTS_CLIENT_SECRET': 'a-private-application-secret-at-least-32-characters'}):
            result = self.runner.invoke(args=['add-client', 'csupor', '--name', 'CSUPOR', '--redirect-uri', 'https://csupor.example.invalid/auth/callback'])
            self.assertEqual(result.exit_code, 0, result.output)
        self.subject = str(uuid4())
        self.password_hash = generate_password_hash('Existing-user-password', method='pbkdf2:sha256:600000')

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def export(self, rows=None):
        value = {'version': 1, 'source': 'csupor', 'issuer': self.config['ACCOUNTS_ISSUER'], 'users': rows if rows is not None else [{
            'subject': self.subject, 'username': 'mate', 'email': 'mate@example.invalid', 'password_hash': self.password_hash,
        }]}
        filename = Path(self.directory.name) / 'export.json'
        filename.write_text(json.dumps(value), encoding='utf-8')
        return filename

    def import_file(self, filename):
        return self.runner.invoke(args=['import-users', str(filename), '--client-id', 'csupor'])

    def test_import_preserves_hash_subject_and_is_idempotent(self):
        filename = self.export()
        for _ in range(2):
            result = self.import_file(filename)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn(self.password_hash, result.output)
        with self.app.app_context():
            account = db.session.get(Account, self.subject)
            self.assertEqual(account.password_hash, self.password_hash)
            self.assertTrue(account.check_password('Existing-user-password'))
            self.assertFalse(account.email_verified)
            self.assertEqual(Account.query.count(), 1)
            self.assertEqual(Membership.query.count(), 1)
            self.assertNotIn('users', inspect(db.engine).get_table_names())

    def test_same_account_can_use_its_email_as_username(self):
        filename = self.export([{'subject': self.subject, 'username': 'mate@example.invalid', 'email': 'mate@example.invalid', 'password_hash': self.password_hash}])
        result = self.import_file(filename)
        self.assertEqual(result.exit_code, 0, result.output)

    def test_conflicting_second_record_rolls_back_whole_import(self):
        filename = self.export([
            {'subject': self.subject, 'username': 'first', 'email': 'first@example.invalid', 'password_hash': self.password_hash},
            {'subject': str(uuid4()), 'username': 'FIRST', 'email': 'second@example.invalid', 'password_hash': self.password_hash},
        ])
        result = self.import_file(filename)
        self.assertNotEqual(result.exit_code, 0)
        with self.app.app_context():
            self.assertEqual(Account.query.count(), 0)
            self.assertEqual(Membership.query.count(), 0)

    def test_reimport_never_overwrites_a_changed_central_password(self):
        filename = self.export()
        self.assertEqual(self.import_file(filename).exit_code, 0)
        with self.app.app_context():
            account = db.session.get(Account, self.subject)
            account.set_password('Changed-central-password')
            db.session.commit()
        self.assertNotEqual(self.import_file(filename).exit_code, 0)
        with self.app.app_context():
            self.assertTrue(db.session.get(Account, self.subject).check_password('Changed-central-password'))

    def test_import_rejects_wrong_issuer_and_unsafe_kdf(self):
        for issuer, password_hash in [('https://other.example.invalid', self.password_hash), (self.config['ACCOUNTS_ISSUER'], 'scrypt:1073741824:8:8$salt$' + 'a'*128)]:
            filename = self.export()
            value = json.loads(filename.read_text())
            value['issuer'] = issuer
            value['users'][0]['password_hash'] = password_hash
            filename.write_text(json.dumps(value))
            self.assertNotEqual(self.import_file(filename).exit_code, 0)
        with self.app.app_context():
            self.assertEqual(Account.query.count(), 0)

    def test_grants_are_per_application_and_deactivation_revokes_version(self):
        self.assertEqual(self.import_file(self.export()).exit_code, 0)
        with self.app.app_context():
            other = Client(client_id='safety', name='Safety', secret_hash='not-used', redirect_uris=['https://safety.example.invalid/cb'])
            db.session.add(other); db.session.commit()
        self.assertEqual(self.runner.invoke(args=['grant-access', 'safety', self.subject]).exit_code, 0)
        self.assertEqual(self.runner.invoke(args=['revoke-access', 'csupor', self.subject]).exit_code, 0)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Membership, (self.subject, 'csupor')))
            self.assertIsNotNone(db.session.get(Membership, (self.subject, 'safety')))
            version = db.session.get(Account, self.subject).version
        self.assertEqual(self.runner.invoke(args=['disable-user', self.subject]).exit_code, 0)
        with self.app.app_context():
            account = db.session.get(Account, self.subject)
            self.assertFalse(account.active)
            self.assertGreater(account.version, version)

    def test_canonical_origin_and_secure_cookie_even_with_dev_flag(self):
        app = create_app({**self.config, 'ACCOUNTS_ALLOW_HTTP': True})
        self.assertTrue(app.config['SESSION_COOKIE_SECURE'])
        browser = app.test_client()
        self.assertEqual(browser.get('/login', base_url='http://accounts.example.invalid').status_code, 400)
        self.assertEqual(browser.get('/login', base_url='https://evil.example.invalid').status_code, 400)
        response = browser.get('/login', base_url=self.config['ACCOUNTS_ISSUER'])
        self.assertEqual(response.status_code, 200)
        self.assertIn('Secure', response.headers['Set-Cookie'])
        self.assertIn('Strict-Transport-Security', response.headers)

    def test_insecure_public_issuer_and_small_secret_fail_at_startup(self):
        for config in ({'ACCOUNTS_ISSUER': 'http://public.example.invalid', 'ACCOUNTS_ALLOW_HTTP': True}, {'ACCOUNTS_SECRET_KEY': 'too-short'}):
            with self.assertRaises(RuntimeError):
                create_app({**self.config, **config})


if __name__ == '__main__':
    unittest.main()
