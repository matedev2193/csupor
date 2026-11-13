"""A first deployment can expose instructions without opening account services."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import pytest
import requests

import accounts
import accounts.bootstrap as bootstrap
from accounts.domain_setup import create_domain_setup_app, domain_setup_requested
from accounts.errors import StartupError
import accounts.start as startup

ROOT = Path(__file__).resolve().parents[2]
HEALTH_SETUP = {'status': 'setup_required', 'mode': 'domain_setup'}
HOST_SENTINEL = 'untrusted-attacker.example.invalid'


@pytest.mark.parametrize(('issuer', 'expected'), [
    (None, True), ('', True), (' \t\n', True),
    ('https://accounts.example.invalid', False), ('invalid-but-present', False),
])
def test_only_missing_or_blank_issuer_selects_domain_setup(monkeypatch, issuer, expected):
    if issuer is None:
        monkeypatch.delenv('ACCOUNTS_ISSUER', raising=False)
    else:
        monkeypatch.setenv('ACCOUNTS_ISSUER', issuer)
    assert domain_setup_requested() is expected


def test_direct_account_factory_still_requires_an_explicit_valid_issuer(tmp_path):
    with pytest.raises(StartupError) as caught:
        accounts.create_app({
            'ACCOUNTS_ISSUER': '',
            'ACCOUNTS_SECRET_KEY': 'valid-secret-for-strict-factory-' + 's' * 40,
            'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'unused.sqlite'),
        })
    assert caught.value.code == 'ACCOUNTS_ISSUER'
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('entrypoint', [startup.prepare_app, startup.worker_app])
@pytest.mark.parametrize('issuer', [None, '', ' \t'])
def test_startup_setup_mode_never_initialises_accounts_database_or_keys(monkeypatch, tmp_path, entrypoint, issuer):
    if issuer is None:
        monkeypatch.delenv('ACCOUNTS_ISSUER', raising=False)
    else:
        monkeypatch.setenv('ACCOUNTS_ISSUER', issuer)
    # Invalid account settings must not even be parsed in the limited mode.
    monkeypatch.setenv('ACCOUNTS_DATABASE_URL', 'not-a-database-url')
    monkeypatch.setenv('ACCOUNTS_TOKEN_TTL', 'not-an-integer')
    monkeypatch.setenv('ACCOUNTS_SIGNING_KEY_FILE', str(tmp_path / 'nonexistent.pem'))
    monkeypatch.setenv('ACCOUNTS_SECRET_KEY', 'too-short')

    def forbidden(*args, **kwargs):
        pytest.fail('Domain setup must not initialise the account app, database or signing keys.')

    monkeypatch.setattr(startup, 'create_app', forbidden)
    monkeypatch.setattr(accounts, 'create_app', forbidden)
    monkeypatch.setattr(startup, 'prepare_database', forbidden)
    monkeypatch.setattr(bootstrap, 'prepare_database', forbidden)
    monkeypatch.setattr(bootstrap.rsa, 'generate_private_key', forbidden)
    app = entrypoint()
    assert app.config['ACCOUNTS_DOMAIN_SETUP'] is True
    assert app.secret_key is None
    assert 'sqlalchemy' not in app.extensions
    assert not any(key.startswith('accounts_') for key in app.extensions)
    assert app.test_client().get('/healthz').json == HEALTH_SETUP
    assert list(tmp_path.iterdir()) == []


def test_setup_information_and_static_stylesheet_work_without_sessions():
    app = create_domain_setup_app()
    client = app.test_client()
    assert client.get('/healthz').json == HEALTH_SETUP
    for path in ['/', '/setup']:
        response = client.get(path)
        assert response.status_code == 200
        assert 'ACCOUNTS_ISSUER' in response.text
        assert '<form' not in response.text
        assert 'Set-Cookie' not in response.headers
        assert response.headers['Cache-Control'] == 'no-store'
        assert "form-action 'none'" in response.headers['Content-Security-Policy']
        head = client.head(path)
        assert head.status_code == 200 and head.data == b''
    assert client.get('/static/accounts.css').status_code == 200


@pytest.mark.parametrize(('method', 'path'), [
    ('GET', '/authorize'), ('GET', '/token'), ('POST', '/token'),
    ('GET', '/.well-known/openid-configuration'), ('GET', '/jwks'),
    ('GET', '/userinfo'), ('GET', '/login'), ('GET', '/register'),
    ('GET', '/account'), ('GET', '/admin'), ('GET', '/unknown'),
    ('POST', '/'), ('POST', '/setup'), ('POST', '/healthz'),
    ('PUT', '/setup'), ('PATCH', '/setup'), ('DELETE', '/setup'),
    ('OPTIONS', '/setup'), ('POST', '/static/accounts.css'),
])
def test_setup_mode_rejects_account_routes_and_mutations(method, path):
    app = create_domain_setup_app()
    response = app.test_client().open(path, method=method, data={
        'ACCOUNTS_ISSUER': 'https://' + HOST_SENTINEL,
        'setup_token': 'attacker-token', 'email': 'attacker@example.invalid',
    })
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '60'
    assert 'Set-Cookie' not in response.headers
    assert HOST_SENTINEL not in response.text
    assert app.config['ACCOUNTS_DOMAIN_SETUP'] is True
    assert app.test_client().get('/jwks').status_code == 503


def test_untrusted_host_and_forwarded_headers_are_not_reflected_or_saved(monkeypatch):
    monkeypatch.delenv('ACCOUNTS_ISSUER', raising=False)
    app = create_domain_setup_app()
    client = app.test_client()
    response = client.get('/setup', headers={
        'Host': HOST_SENTINEL,
        'X-Forwarded-Host': 'forwarded-' + HOST_SENTINEL,
        'X-Forwarded-Proto': 'https',
    })
    assert response.status_code == 200
    assert HOST_SENTINEL not in response.text
    assert HOST_SENTINEL not in str(response.headers)
    assert response.headers.get('Location') is None
    assert 'ACCOUNTS_ISSUER' not in app.config
    assert domain_setup_requested()
    assert client.get('/healthz').json == HEALTH_SETUP


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


@contextmanager
def running_server(tmp_path, environment, label):
    log = tmp_path / (label + '.log')
    with log.open('wb') as output:
        process = subprocess.Popen(
            [sys.executable, '-m', 'accounts.start'], cwd=ROOT, env=environment,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
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


def wait_ready(process, log, base_url, expected):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail('Account startup exited unexpectedly: ' + log.read_text(errors='replace'))
        try:
            response = requests.get(base_url + '/healthz', timeout=0.5)
            if response.status_code == 200:
                assert response.json() == expected
                return
        except requests.RequestException:
            pass
        time.sleep(0.05)
    pytest.fail('Account startup timed out: ' + log.read_text(errors='replace'))


def test_real_domainless_deploy_then_configured_https_restart(tmp_path):
    port = free_port()
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('ACCOUNTS_') and key != 'PORT'}
    environment.update(PORT=str(port), PYTHONUNBUFFERED='1')
    base_url = f'http://127.0.0.1:{port}'
    with running_server(tmp_path, environment, 'domainless') as server:
        wait_ready(*server, base_url, HEALTH_SETUP)
        response = requests.get(base_url + '/', headers={'Host': HOST_SENTINEL}, timeout=2)
        assert response.status_code == 200
        assert 'ACCOUNTS_ISSUER' in response.text
        assert HOST_SENTINEL not in response.text + str(response.headers)
        assert 'Set-Cookie' not in response.headers
        assert requests.get(base_url + '/static/accounts.css', timeout=2).status_code == 200
        for path in ['/jwks', '/login', '/admin', '/.well-known/openid-configuration']:
            assert requests.get(base_url + path, timeout=2).status_code == 503
        assert requests.post(base_url + '/setup', data={
            'ACCOUNTS_ISSUER': 'https://' + HOST_SENTINEL,
        }, timeout=2).status_code == 503
        assert requests.get(base_url + '/healthz', timeout=2).json() == HEALTH_SETUP
    assert '[ACCOUNTS_DOMAIN_SETUP]' in (tmp_path / 'domainless.log').read_text()
    assert {path.name for path in tmp_path.iterdir()} == {'domainless.log'}

    database = tmp_path / 'accounts.sqlite'
    environment.update({
        'ACCOUNTS_ISSUER': 'https://accounts.example.invalid',
        'ACCOUNTS_SECRET_KEY': 'domain-restart-private-secret-' + 's' * 40,
        'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(database),
        'ACCOUNTS_SETUP_TOKEN': 'domain-restart-private-token-' + 't' * 40,
        'ACCOUNTS_PROXY_HOPS': '1',
    })
    canonical_headers = {'Host': 'accounts.example.invalid', 'X-Forwarded-Proto': 'https'}
    with running_server(tmp_path, environment, 'configured') as server:
        wait_ready(*server, base_url, {'status': 'ok'})
        setup = requests.get(base_url + '/setup', headers=canonical_headers, timeout=2)
        assert setup.status_code == 200
        assert '<form' in setup.text
        keys = requests.get(base_url + '/jwks', headers=canonical_headers, timeout=2)
        assert keys.status_code == 200 and len(keys.json()['keys']) == 1
        assert requests.get(base_url + '/setup', timeout=2).status_code == 400
    assert database.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM accounts_signing_keys').fetchone() == (1,)
        assert connection.execute('SELECT COUNT(*) FROM accounts').fetchone() == (0,)
    configured_log = (tmp_path / 'configured.log').read_text()
    assert '[ACCOUNTS_DOMAIN_SETUP]' not in configured_log
    for key in ['ACCOUNTS_SECRET_KEY', 'ACCOUNTS_DATABASE_URL', 'ACCOUNTS_SETUP_TOKEN']:
        assert environment[key] not in configured_log
    assert 'BEGIN PRIVATE KEY' not in configured_log
