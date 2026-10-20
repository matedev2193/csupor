"""Actionable startup categories without leaking configuration or driver text."""
import os
from pathlib import Path
import subprocess
import sys
import traceback

from mysql.connector.errors import DatabaseError
import pytest
from sqlalchemy.exc import InterfaceError, OperationalError

import accounts.start as startup
from accounts.errors import StartupError, diagnostic

ROOT = Path(__file__).resolve().parents[2]
SENSITIVE = 'NEVER_PRINT_private_database_password_84af29'
PRIVATE_SQL = "SELECT private_token FROM secret_table WHERE password='" + SENSITIVE + "'"


def clean_environment(tmp_path):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('ACCOUNTS_') and key != 'PORT'}
    environment.update({
        'ACCOUNTS_SECRET_KEY': 'valid-test-secret-' + 's' * 40,
        'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'unused.sqlite'),
        'ACCOUNTS_ISSUER': 'https://accounts.example.invalid',
        'ACCOUNTS_SETUP_TOKEN': 'valid-setup-token-' + 't' * 40,
        'PYTHONUNBUFFERED': '1',
    })
    return environment


def assert_redacted(output, values=()):
    assert SENSITIVE not in output
    assert PRIVATE_SQL not in output
    assert 'Traceback' not in output
    assert 'secret_table' not in output
    for value in values:
        if value:
            assert value not in output


@pytest.mark.parametrize(('key', 'value', 'code'), [
    ('ACCOUNTS_ISSUER', None, 'ACCOUNTS_ISSUER'),
    ('ACCOUNTS_ISSUER', 'https://user:' + SENSITIVE + '@accounts.example.invalid', 'ACCOUNTS_ISSUER'),
    ('ACCOUNTS_SECRET_KEY', None, 'ACCOUNTS_SECRET_KEY'),
    ('ACCOUNTS_SECRET_KEY', 'tiny-private-value', 'ACCOUNTS_SECRET_KEY'),
    ('ACCOUNTS_DATABASE_URL', None, 'ACCOUNTS_DATABASE_URL'),
    ('ACCOUNTS_DATABASE_URL', 'not-a-database-url-' + SENSITIVE, 'ACCOUNTS_DATABASE_URL'),
    ('ACCOUNTS_DATABASE_URL', 'mysql://user:' + SENSITIVE + '@db.example.invalid:65536/accounts', 'ACCOUNTS_DATABASE_URL'),
    ('ACCOUNTS_DATABASE_URL', 'mysql://user:' + SENSITIVE + '@db.example.invalid:0/accounts', 'ACCOUNTS_DATABASE_URL'),
    ('ACCOUNTS_DATABASE_URL', 'mysql://user:' + SENSITIVE + '@db.example.invalid:invalid/accounts', 'ACCOUNTS_DATABASE_URL'),
    ('ACCOUNTS_SETUP_TOKEN', 'tiny-setup-secret', 'ACCOUNTS_SETUP_TOKEN'),
    ('ACCOUNTS_TOKEN_TTL', 'not-an-integer-' + SENSITIVE, 'ACCOUNTS_TOKEN_TTL'),
    ('ACCOUNTS_TOKEN_TTL', '59', 'ACCOUNTS_TOKEN_TTL'),
    ('ACCOUNTS_PROXY_HOPS', 'not-an-integer-' + SENSITIVE, 'ACCOUNTS_PROXY_HOPS'),
    ('ACCOUNTS_PROXY_HOPS', '3', 'ACCOUNTS_PROXY_HOPS'),
    ('ACCOUNTS_SIGNING_KEY_FILE', '/private/' + SENSITIVE + '.pem', 'ACCOUNTS_SIGNING_KEY_FILE'),
    ('PORT', 'not-an-integer-' + SENSITIVE, 'PORT'),
    ('PORT', '0', 'PORT'),
])
def test_real_entrypoint_names_invalid_setting_and_redacts_value(tmp_path, key, value, code):
    environment = clean_environment(tmp_path)
    if value is None:
        environment.pop(key, None)
    else:
        environment[key] = value
    result = subprocess.run([sys.executable, '-m', 'accounts.start'], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=10)
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert f'[ACCOUNTS_STARTUP_{code}]' in result.stderr
    assert 'A webkiszolgáló nem indult el.' in result.stderr
    # Numeric ranges may legitimately appear in remediation instructions.
    sensitive_values = [environment['ACCOUNTS_SETUP_TOKEN']]
    if key != 'ACCOUNTS_SECRET_KEY':
        sensitive_values.append(environment['ACCOUNTS_SECRET_KEY'])
    if value and len(value) > 10:
        sensitive_values.append(value)
    assert_redacted(output, sensitive_values)
    assert not (tmp_path / 'unused.sqlite').exists(), 'Invalid configuration must fail before creating a database.'


def test_real_entrypoint_reports_invalid_ca_without_attempting_database_connection(tmp_path):
    environment = clean_environment(tmp_path)
    environment.update({
        'ACCOUNTS_DATABASE_URL': 'mysql://private_user:' + SENSITIVE + '@db.example.invalid/accounts?ssl-mode=REQUIRED',
        'ACCOUNTS_DATABASE_CA_CERT': '-----BEGIN CERTIFICATE-----\n' + SENSITIVE + '\n-----END CERTIFICATE-----',
    })
    result = subprocess.run([sys.executable, '-m', 'accounts.start'], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=10)
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert '[ACCOUNTS_STARTUP_ACCOUNTS_DATABASE_CA_CERT]' in result.stderr
    assert_redacted(output, [environment['ACCOUNTS_DATABASE_URL'], environment['ACCOUNTS_DATABASE_CA_CERT']])


def mysql_error(errno, *, wrapper=OperationalError, invalidated=False):
    original = DatabaseError(msg=SENSITIVE + ' ' + PRIVATE_SQL, errno=errno, sqlstate='HY000')
    return wrapper(PRIVATE_SQL, {'private_password': SENSITIVE}, original,
                   connection_invalidated=invalidated)


@pytest.mark.parametrize(('errno', 'code'), [
    (1044, 'DATABASE_PERMISSION'),
    (1045, 'DATABASE_AUTH'),
    (1698, 'DATABASE_AUTH'),
    (2061, 'DATABASE_AUTH'),
    (1049, 'DATABASE_NAME'),
    (1142, 'DATABASE_PERMISSION'),
    (1143, 'DATABASE_PERMISSION'),
    (1227, 'DATABASE_PERMISSION'),
    (2026, 'DATABASE_TLS'),
    (3159, 'DATABASE_TLS'),
    (1054, 'DATABASE_SCHEMA'),
    (1146, 'DATABASE_SCHEMA'),
    (1064, 'DATABASE_SCHEMA'),
    (1040, 'DATABASE_CAPACITY'),
    (1205, 'DATABASE_BUSY'),
    (1213, 'DATABASE_BUSY'),
    (2002, 'DATABASE_NETWORK'),
    (2003, 'DATABASE_NETWORK'),
    (2005, 'DATABASE_NETWORK'),
    (2006, 'DATABASE_NETWORK'),
    (2013, 'DATABASE_NETWORK'),
    (2055, 'DATABASE_NETWORK'),
])
def test_entrypoint_classifies_wrapped_mysql_failure_without_driver_details(monkeypatch, capsys, errno, code):
    error = mysql_error(errno)
    def failed_preparation():
        raise error
    monkeypatch.setenv('PORT', '8080')
    monkeypatch.setattr(startup, 'prepare_app', failed_preparation)
    monkeypatch.setattr(startup.os, 'execv', lambda *args: pytest.fail('Gunicorn must not start after preparation fails.'))
    assert startup.main() == 1
    captured = capsys.readouterr()
    assert f'[ACCOUNTS_STARTUP_{code}]' in captured.err
    assert len(captured.err) > len(f'[ACCOUNTS_STARTUP_{code}]') + 30
    assert_redacted(captured.out + captured.err)


@pytest.mark.parametrize('code', ['SIGNING_KEY', 'DATABASE_LOCK', 'DATABASE_TLS'])
def test_typed_nonconfiguration_failures_have_safe_categories(code):
    message = diagnostic(StartupError(code, SENSITIVE + PRIVATE_SQL))
    assert f'[ACCOUNTS_STARTUP_{code}]' in message
    assert_redacted(message)


@pytest.mark.parametrize('error', [
    RuntimeError(SENSITIVE + PRIVATE_SQL),
    ValueError('[ACCOUNTS_STARTUP_PORT] ' + SENSITIVE),
])
def test_unexpected_exception_cannot_leak_text_or_spoof_a_diagnostic(monkeypatch, capsys, error):
    def fail():
        raise error
    monkeypatch.setattr(startup, 'prepare_app', fail)
    monkeypatch.setenv('PORT', '8080')
    assert startup.main() == 1
    captured = capsys.readouterr()
    assert '[ACCOUNTS_STARTUP_UNEXPECTED]' in captured.err
    assert '[ACCOUNTS_STARTUP_PORT]' not in captured.err
    assert_redacted(captured.out + captured.err)


@pytest.mark.parametrize('errno', [2026, 3159])
@pytest.mark.parametrize('wrapper', [OperationalError, InterfaceError])
def test_tls_configuration_failures_are_not_retried_even_if_connection_invalidated(monkeypatch, errno, wrapper):
    error = mysql_error(errno, wrapper=wrapper, invalidated=True)
    assert startup._retryable(error) is False
    attempts = []
    def unavailable_factory():
        attempts.append(True)
        raise error
    monkeypatch.setattr(startup, 'create_app', unavailable_factory)
    monkeypatch.setattr(startup.time, 'sleep', lambda seconds: pytest.fail('A TLS configuration failure must not be retried.'))
    with pytest.raises(type(error)) as caught:
        startup.prepare_app(retry_seconds=30)
    assert caught.value is error
    assert len(attempts) == 1


def test_unknown_typed_code_and_unknown_driver_errno_use_safe_fallbacks():
    message = diagnostic(StartupError('UNREGISTERED_' + SENSITIVE, PRIVATE_SQL))
    assert '[ACCOUNTS_STARTUP_UNEXPECTED]' in message
    assert_redacted(message)
    message = diagnostic(mysql_error(99999))
    assert '[ACCOUNTS_STARTUP_DATABASE]' in message
    assert_redacted(message)


@pytest.mark.parametrize(('error', 'code'), [
    (mysql_error(1045), 'DATABASE_AUTH'),
    (mysql_error(2026), 'DATABASE_TLS'),
    (ValueError('mysql://user:' + SENSITIVE + '@db.example.invalid/database ' + PRIVATE_SQL), 'UNEXPECTED'),
])
def test_worker_factory_suppresses_sensitive_exception_chains(monkeypatch, error, code):
    def fail():
        raise error
    monkeypatch.setattr(startup, 'create_app', fail)
    with pytest.raises(RuntimeError) as caught:
        startup.worker_app()
    formatted = ''.join(traceback.format_exception(caught.value))
    assert f'[ACCOUNTS_STARTUP_{code}]' in formatted
    assert caught.value.__suppress_context__ is True
    assert SENSITIVE not in formatted
    assert PRIVATE_SQL not in formatted
    assert 'mysql://user:' not in formatted
    assert 'secret_table' not in formatted
    assert 'OperationalError' not in formatted
    assert 'During handling of the above exception' not in formatted


def test_worker_factory_passes_through_successful_app(monkeypatch):
    application = object()
    monkeypatch.setattr(startup, 'create_app', lambda: application)
    assert startup.worker_app() is application


@pytest.mark.parametrize('errno', [2003, 2006, 2013])
def test_transient_database_disconnects_remain_retryable(errno):
    assert startup._retryable(mysql_error(errno)) is True
