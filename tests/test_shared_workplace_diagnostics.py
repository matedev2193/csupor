"""Publisher diagnostics reveal failure classes without leaking private data."""
from unittest.mock import Mock
import json

from flask import Flask
import pytest
import requests
from sqlalchemy.exc import OperationalError
from urllib3.exceptions import ReadTimeoutError

from app import db
from app import shared_workplaces as publisher


PRIVATE = 'private-password-and-source-address'


@pytest.fixture
def app(monkeypatch):
    instance = Flask(__name__)
    instance.config.update(AUTH_MODE='oidc', OIDC_CLIENT_ID='csupor',
                           OIDC_CLIENT_SECRET=PRIVATE, OIDC_ISSUER='https://private.example.invalid')
    publisher.init_shared_workplaces(instance)
    monkeypatch.setattr(publisher, '_allocate_snapshot', lambda: ({'generation': 1, 'workplaces': []}, 'a' * 64))
    monkeypatch.setattr(db.session, 'rollback', Mock())
    return instance


def response(status, body=PRIVATE.encode()):
    result = Mock(status_code=status)
    result.iter_content.return_value = [body]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def assert_diagnostic(app, caplog, code):
    with app.app_context():
        assert publisher.publish_workplaces(force=True) is False
    assert f'[CSUPOR_WORKPLACES_{code}]' in caplog.text
    assert PRIVATE not in caplog.text
    assert 'private.example.invalid' not in caplog.text
    assert app.extensions['shared_workplace_publisher']['last_success'] is False
    db.session.rollback.assert_called_once()


@pytest.mark.parametrize(('status', 'code'), [
    (401, 'AUTH_REJECTED'), (403, 'AUTH_REJECTED'), (404, 'ENDPOINT_MISSING'),
    (409, 'STALE_SNAPSHOT'), (429, 'RATE_LIMITED'),
    (500, 'UPSTREAM_UNAVAILABLE'), (502, 'UPSTREAM_UNAVAILABLE'), (503, 'UPSTREAM_UNAVAILABLE'),
    (400, 'UPSTREAM_REJECTED'), (302, 'UPSTREAM_REJECTED'),
])
def test_http_failures_have_fixed_codes_and_never_log_private_error_bodies(app, monkeypatch, caplog, status, code):
    reply = response(status)
    post = Mock(return_value=reply)
    monkeypatch.setattr(publisher.requests, 'post', post)
    assert_diagnostic(app, caplog, code)
    if status == 503:
        reply.iter_content.assert_called_once_with(1024)
    else:
        reply.iter_content.assert_not_called()
    assert post.call_args.kwargs['allow_redirects'] is False
    assert post.call_args.kwargs['timeout'] == 3


@pytest.mark.parametrize('category', ['MISSING', 'INVALID', 'CONFLICT'])
def test_central_configuration_errors_name_the_required_env_without_echoing_remote_text(app, monkeypatch, caplog, category):
    body = json.dumps({'error': 'integration_not_configured',
                      'code': 'ACCOUNTS_INTEGRATION_CSUPOR_' + category,
                      'message': PRIVATE}).encode()
    monkeypatch.setattr(publisher.requests, 'post', Mock(return_value=response(503, body)))
    assert_diagnostic(app, caplog, 'CONFIG_' + category)
    assert 'OIDC_CLIENT_ID' in caplog.text
    if category == 'CONFLICT':
        assert 'SAFETY_OIDC_CLIENT_ID' in caplog.text


@pytest.mark.parametrize('body', [
    b'not-json-' + PRIVATE.encode(), b'\xff', b'null', b'[]', b'[' * 1100 + b']' * 1100,
    json.dumps({'error': 'other_error', 'code': 'ACCOUNTS_INTEGRATION_CSUPOR_MISSING', 'message': PRIVATE}).encode(),
    json.dumps({'error': 'integration_not_configured', 'code': 'ACCOUNTS_INTEGRATION_SAFETY_MISSING', 'message': PRIVATE}).encode(),
    json.dumps({'error': 'integration_not_configured', 'code': PRIVATE}).encode(),
    json.dumps({'error': 'integration_not_configured', 'code': {'private': PRIVATE}}).encode(),
])
def test_unknown_or_malformed_503_body_stays_unavailable_and_private(app, monkeypatch, caplog, body):
    monkeypatch.setattr(publisher.requests, 'post', Mock(return_value=response(503, body)))
    assert_diagnostic(app, caplog, 'UPSTREAM_UNAVAILABLE')


def test_503_configuration_body_reader_stops_at_4096_bytes(app, monkeypatch, caplog):
    reply = response(503)
    def chunks():
        yield b' ' * 4096
        yield b' '
        raise AssertionError('An oversized upstream error must not be read further')
    reply.iter_content.side_effect = lambda size: chunks()
    monkeypatch.setattr(publisher.requests, 'post', Mock(return_value=reply))
    assert_diagnostic(app, caplog, 'UPSTREAM_UNAVAILABLE')


@pytest.mark.parametrize(('error', 'code'), [
    (requests.Timeout(PRIVATE), 'TIMEOUT'),
    (requests.ConnectionError(PRIVATE), 'NETWORK'),
    (requests.ConnectionError(ReadTimeoutError(None, PRIVATE, PRIVATE)), 'TIMEOUT'),
    (OperationalError(PRIVATE, {'password': PRIVATE}, Exception(PRIVATE)), 'SOURCE_DATABASE'),
    (publisher.WorkplacePublishError('INVALID_SOURCE'), 'INVALID_SOURCE'),
    (publisher.WorkplacePublishError('SOURCE_CHANGED'), 'SOURCE_CHANGED'),
    (RuntimeError(PRIVATE), 'INTERNAL'),
])
def test_exception_classes_produce_sanitised_diagnostics(app, monkeypatch, caplog, error, code):
    monkeypatch.setattr(publisher.requests, 'post', Mock(side_effect=error))
    assert_diagnostic(app, caplog, code)


@pytest.mark.parametrize('body', [PRIVATE.encode(), b'\xff', b'null', b'{}', b' ' * (2 * 1024 * 1024 + 1)])
def test_bad_success_responses_are_sanitised(app, monkeypatch, caplog, body):
    monkeypatch.setattr(publisher.requests, 'post', Mock(return_value=response(200, body)))
    assert_diagnostic(app, caplog, 'INVALID_RESPONSE')


def test_invalid_source_is_detected_before_network(app, monkeypatch, caplog):
    monkeypatch.setattr(publisher, '_allocate_snapshot', lambda: publisher._source_text('\u200b\ufeff', 255))
    post = Mock()
    monkeypatch.setattr(publisher.requests, 'post', post)
    assert_diagnostic(app, caplog, 'INVALID_SOURCE')
    post.assert_not_called()


def test_configured_writer_id_is_used_without_literal_client_restriction(app, monkeypatch):
    app.config['OIDC_CLIENT_ID'] = 'staff-tenant-17'
    post = Mock(return_value=response(200, b'{"source":"csupor","revision":"' + b'a' * 64 + b'"}'))
    monkeypatch.setattr(publisher.requests, 'post', post)
    with app.app_context():
        assert publisher.publish_workplaces(force=True) is True
    assert post.call_args.kwargs['auth'] == ('staff-tenant-17', PRIVATE)


def test_normalisation_keeps_visible_unicode_and_separates_whitespace():
    assert publisher._source_text(' \ufeffŐzike\u200b óvoda\u202e\n\t1.\x00 ', 120) == 'Őzike óvoda 1.'
    assert publisher._source_text('👨\u200d👩 és 🌳', 120) == '👨👩 és 🌳'


@pytest.mark.parametrize('value', [None, 3, '', '\u200b', ' ' * 10, 'ő' * 121])
def test_empty_or_oversized_normalised_source_is_rejected(value):
    with pytest.raises(publisher.WorkplacePublishError) as error:
        publisher._source_text(value, 120)
    assert error.value.code == 'INVALID_SOURCE'
