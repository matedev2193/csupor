"""Automatic account-service bootstrap, persistent keys and deployment config."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest
import requests
from sqlalchemy import text

from accounts import create_app, _require_mysql_tls
from accounts.bootstrap import StoredSigningKey, key_id
from accounts.models import db
import accounts.start as startup

ROOT = Path(__file__).resolve().parents[2]


def config(tmp_path, **overrides):
    return {
        'TESTING': True, 'ACCOUNTS_SECRET_KEY': 'accounts-test-secret-' + 'x' * 40,
        'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'accounts.sqlite'),
        'ACCOUNTS_ISSUER': 'http://localhost:9001', 'ACCOUNTS_ALLOW_HTTP': True,
        'ACCOUNTS_SIGNING_KEY_FILE': None, 'ACCOUNTS_SETUP_TOKEN': '',
        **overrides,
    }


@pytest.fixture()
def private_file(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / 'legacy-private.pem'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(0o600)
    return key, path


def dispose(app):
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def test_encrypted_key_is_persistent_and_only_ciphertext_is_stored(tmp_path):
    first = create_app(config(tmp_path))
    with first.app_context():
        saved = StoredSigningKey.query.one()
        encrypted, public_id = saved.encrypted_pem, saved.public_key_id
        assert encrypted.startswith('gAAAA')
        assert 'PRIVATE KEY' not in encrypted
        assert public_id == first.extensions['accounts_key_id']
        assert not any(name.startswith('csupor') for name in db.metadata.tables)
    dispose(first)
    second = create_app(config(tmp_path))
    assert second.extensions['accounts_public_key'] == first.extensions['accounts_public_key']
    with second.app_context():
        assert StoredSigningKey.query.count() == 1
        assert StoredSigningKey.query.one().encrypted_pem == encrypted
    dispose(second)
    assert b'BEGIN PRIVATE KEY' not in (tmp_path / 'accounts.sqlite').read_bytes()


@pytest.mark.parametrize('damage', ['secret', 'ciphertext', 'public-id'])
def test_wrong_secret_or_corrupt_stored_key_fails_without_replacement(tmp_path, damage):
    app = create_app(config(tmp_path))
    with app.app_context():
        saved = StoredSigningKey.query.one()
        if damage == 'ciphertext':
            saved.encrypted_pem = 'invalid-ciphertext'
        elif damage == 'public-id':
            saved.public_key_id = 'wrong-public-key-id'
        db.session.commit()
        before = (saved.encrypted_pem, saved.public_key_id)
    dispose(app)
    changes = {'ACCOUNTS_SECRET_KEY': 'different-secret-' + 'z' * 40} if damage == 'secret' else {}
    with pytest.raises(RuntimeError, match='stored account signing key cannot be opened'):
        create_app(config(tmp_path, **changes))
    with sqlite3.connect(tmp_path / 'accounts.sqlite') as connection:
        assert connection.execute('SELECT encrypted_pem,public_key_id FROM accounts_signing_keys').fetchall() == [before]


def test_existing_key_file_remains_supported_and_start_creates_tables(tmp_path, private_file, monkeypatch):
    key, path = private_file
    settings = config(tmp_path, ACCOUNTS_SIGNING_KEY_FILE=str(path))
    monkeypatch.setattr(startup, 'create_app', lambda: create_app(settings))
    app = startup.prepare_app()
    assert app.extensions['accounts_key_id'] == key_id(key)
    with app.app_context():
        assert StoredSigningKey.query.count() == 0
        assert db.session.execute(text('SELECT COUNT(*) FROM account_setup_state')).scalar_one() == 1
    dispose(app)


def test_mysql_url_normalisation_and_required_tls(tmp_path, private_file):
    _, path = private_file
    app = create_app(config(tmp_path, ACCOUNTS_SIGNING_KEY_FILE=str(path),
                            ACCOUNTS_DATABASE_URL='mysql://dbuser:example-password@db.example.invalid:25060/accounts?ssl-mode=REQUIRED'))
    assert app.config['SQLALCHEMY_DATABASE_URI'].startswith('mysql+mysqlconnector://')
    assert 'ssl-mode' not in app.config['SQLALCHEMY_DATABASE_URI']
    arguments = app.config['SQLALCHEMY_ENGINE_OPTIONS']['connect_args']
    assert arguments['connection_timeout'] == 5
    assert arguments['ssl_disabled'] is False
    assert arguments['ssl_verify_cert'] is True and arguments['ssl_verify_identity'] is True
    assert app.config['ACCOUNTS_DATABASE_REQUIRE_TLS'] is True
    dispose(app)


def test_database_ca_pem_is_validated_and_written_as_certificate_only(tmp_path, private_file):
    key, path = private_file
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test database CA')])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
                   .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode())
    settings = config(tmp_path, ACCOUNTS_SIGNING_KEY_FILE=str(path),
                      ACCOUNTS_DATABASE_URL='mysql://dbuser:example-password@db.example.invalid/accounts',
                      ACCOUNTS_DATABASE_CA_CERT=certificate.replace('\n', '\\n'))
    app = create_app(settings)
    ca_path = Path(app.config['SQLALCHEMY_ENGINE_OPTIONS']['connect_args']['ssl_ca'])
    assert ca_path.read_text() == certificate
    assert ca_path.stat().st_mode & 0o777 == 0o600
    assert 'PRIVATE KEY' not in ca_path.read_text()
    dispose(app)
    with pytest.raises(RuntimeError, match='CA certificate'):
        create_app({**settings, 'ACCOUNTS_DATABASE_CA_CERT': 'not a certificate'})
    with pytest.raises(RuntimeError, match='DATABASE_URL'):
        create_app({**settings, 'ACCOUNTS_DATABASE_URL': 'mysql://x:y@host/db?ssl-mode=DISABLED'})


def test_unencrypted_driver_fallback_is_rejected():
    class Cursor:
        def execute(self, statement):
            assert statement == "SHOW SESSION STATUS LIKE 'Ssl_cipher'"
        def fetchone(self):
            return ('Ssl_cipher', '')
        def close(self):
            pass
    class Connection:
        closed = False
        def cursor(self):
            return Cursor()
        def close(self):
            self.closed = True
    connection = Connection()
    with pytest.raises(RuntimeError, match='encrypted TLS'):
        _require_mysql_tls(connection, None)
    assert connection.closed


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def environment(tmp_path, port):
    env = os.environ.copy()
    env.update({
        'ACCOUNTS_SECRET_KEY': 'startup-test-secret-' + 's' * 40,
        'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'accounts.sqlite'),
        'ACCOUNTS_ISSUER': f'http://127.0.0.1:{port}',
        'ACCOUNTS_ALLOW_HTTP': 'true', 'ACCOUNTS_SETUP_TOKEN': 'setup-test-token-' + 't' * 40,
        'PORT': str(port), 'PYTHONUNBUFFERED': '1',
    })
    env.pop('ACCOUNTS_SIGNING_KEY_FILE', None)
    env.pop('ACCOUNTS_DATABASE_CA_CERT', None)
    return env


@contextmanager
def running_server(tmp_path, env, label):
    log = tmp_path / f'{label}.log'
    with log.open('wb') as output:
        process = subprocess.Popen([sys.executable, '-m', 'accounts.start'], cwd=ROOT, env=env,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            yield process, log
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def wait_ready(process, log, port):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail('Account startup exited unexpectedly: ' + log.read_text(errors='replace'))
        try:
            response = requests.get(f'http://127.0.0.1:{port}/healthz', timeout=0.5)
            if response.status_code == 200:
                assert response.json() == {'status': 'ok'}
                keys = requests.get(f'http://127.0.0.1:{port}/jwks', timeout=2).json()
                assert len(keys['keys']) == 1
                return keys
        except requests.RequestException:
            pass
        time.sleep(0.05)
    pytest.fail('Account Gunicorn startup timed out: ' + log.read_text(errors='replace'))


def test_real_start_restart_and_concurrent_start_keep_one_key_and_existing_accounts(tmp_path):
    port = free_port()
    env = environment(tmp_path, port)
    with running_server(tmp_path, env, 'first') as server:
        first_keys = wait_ready(*server, port)
    with sqlite3.connect(tmp_path / 'accounts.sqlite') as connection:
        original_key = connection.execute('SELECT encrypted_pem,public_key_id FROM accounts_signing_keys').fetchone()
        connection.execute("INSERT INTO accounts VALUES ('unchanged-user','Old user','old@example.hu','unchanged-password-hash',1,4,0)")
        connection.commit()
        original_account = connection.execute('SELECT * FROM accounts').fetchall()
    other_port = free_port()
    with running_server(tmp_path, env, 'restart') as server:
        with running_server(tmp_path, environment(tmp_path, other_port), 'parallel') as second:
            assert wait_ready(*server, port) == first_keys
            assert wait_ready(*second, other_port) == first_keys
    with sqlite3.connect(tmp_path / 'accounts.sqlite') as connection:
        assert connection.execute('SELECT encrypted_pem,public_key_id FROM accounts_signing_keys').fetchall() == [original_key]
        assert connection.execute('SELECT * FROM accounts').fetchall() == original_account
    for path in tmp_path.glob('*.log'):
        content = path.read_text()
        assert env['ACCOUNTS_SECRET_KEY'] not in content
        assert env['ACCOUNTS_SETUP_TOKEN'] not in content
        assert original_key[0] not in content
        assert 'BEGIN PRIVATE KEY' not in content


def test_parallel_fresh_servers_create_exactly_one_signing_key(tmp_path):
    first_port, second_port = free_port(), free_port()
    with running_server(tmp_path, environment(tmp_path, first_port), 'fresh-one') as first:
        with running_server(tmp_path, environment(tmp_path, second_port), 'fresh-two') as second:
            assert wait_ready(*first, first_port) == wait_ready(*second, second_port)
    with sqlite3.connect(tmp_path / 'accounts.sqlite') as connection:
        assert connection.execute('SELECT COUNT(*) FROM accounts_signing_keys').fetchone() == (1,)
        assert connection.execute('SELECT COUNT(*) FROM accounts').fetchone() == (0,)


def test_wrong_secret_start_fails_without_http_or_secret_output(tmp_path):
    port = free_port()
    env = environment(tmp_path, port)
    with running_server(tmp_path, env, 'initial') as server:
        wait_ready(*server, port)
    env['ACCOUNTS_SECRET_KEY'] = 'wrong-secret-do-not-print-' + 'z' * 40
    result = subprocess.run([sys.executable, '-m', 'accounts.start'], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert 'A webkiszolgáló nem indult el.' in result.stderr
    assert env['ACCOUNTS_SECRET_KEY'] not in result.stderr
    assert env['ACCOUNTS_DATABASE_URL'] not in result.stderr
    assert 'Traceback' not in result.stderr
    with pytest.raises(requests.ConnectionError):
        requests.get(f'http://127.0.0.1:{port}/healthz', timeout=0.5)


def test_entrypoint_execs_after_preparation_and_validates_port(monkeypatch):
    calls = []
    monkeypatch.setenv('PORT', '9123')
    monkeypatch.setattr(startup, 'prepare_app', lambda: calls.append('prepared'))
    monkeypatch.setattr(startup.os, 'execv', lambda executable, argv: calls.append((executable, argv)))
    assert startup.main() == 0
    assert calls[0] == 'prepared'
    assert calls[1][1] == [sys.executable, '-m', 'gunicorn', '--bind', '0.0.0.0:9123',
                           '--workers', '2', '--worker-tmp-dir', '/dev/shm', 'accounts.start:worker_app()']
    monkeypatch.delenv('PORT')
    assert startup._port() == 8080
    monkeypatch.setenv('PORT', 'invalid')
    calls.clear()
    assert startup.main() == 1
    assert calls == []
