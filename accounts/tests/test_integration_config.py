"""Integration authority comes from explicit deployment configuration only."""
from base64 import b64encode

import pytest

from accounts import create_app
from accounts.integration_config import integration_client_id, integration_status
from accounts.tests.test_end_session import app, pem, bearer, TOKENS, ISSUER


@pytest.mark.parametrize(('value', 'category'), [
    (None, 'MISSING'), ('', 'MISSING'), ('   ', 'MISSING'),
    ('with space', 'INVALID'), ('bad/path', 'INVALID'), ('a' * 81, 'INVALID'),
    (42, 'INVALID'), ('safety', 'CONFLICT'), ('SAFETY', 'CONFLICT'),
])
def test_unconfigured_roles_disable_only_integration_without_breaking_login(app, value, category):
    app.config.update(OIDC_CLIENT_ID=value, SAFETY_OIDC_CLIENT_ID='safety')
    with app.app_context():
        status = integration_status('csupor')
        assert status['code'] == 'ACCOUNTS_INTEGRATION_CSUPOR_' + category
        assert status['client_id'] is None
        assert integration_client_id('csupor') is None
    browser = app.test_client()
    assert browser.get('/login', base_url=ISSUER).status_code == 200
    assert browser.get('/userinfo', base_url=ISSUER, headers=bearer(TOKENS['csupor'])).status_code == 200
    for path, method in [('/api/csupor/workplaces', 'post'), ('/api/csupor/profile', 'get'),
                         ('/api/csupor/profile/import', 'post'), ('/api/csupor/profiles/read', 'post'),
                         ('/api/csupor/profile/photo', 'get'), ('/api/csupor/profile/photo/import', 'post')]:
        response = getattr(browser, method)(path, base_url=ISSUER, headers=bearer(TOKENS['csupor']))
        assert response.status_code == 503, (path, response.status_code)
        assert response.json['error'] == 'integration_not_configured'
        assert response.json['code'] == status['code']


@pytest.mark.parametrize(('value', 'category'), [(None, 'MISSING'), ('bad id', 'INVALID'), ('csupor', 'CONFLICT')])
def test_safety_missing_invalid_or_conflicting_role_fails_closed(app, value, category):
    app.config['SAFETY_OIDC_CLIENT_ID'] = value
    response = app.test_client().get('/api/workplaces', base_url=ISSUER, headers=bearer(TOKENS['safety']))
    assert response.status_code == 503
    assert response.json['code'] == 'ACCOUNTS_INTEGRATION_SAFETY_' + category
    with app.app_context():
        assert integration_client_id('safety') is None
        if category != 'CONFLICT':
            assert integration_client_id('csupor') == 'csupor'


def test_factory_loads_arbitrary_ids_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('OIDC_CLIENT_ID', 'staff-tenant-17')
    monkeypatch.setenv('SAFETY_OIDC_CLIENT_ID', 'training-tenant-42')
    instance = create_app({'TESTING': True, 'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'fresh.db'),
                           'ACCOUNTS_SECRET_KEY': 'fresh-config-secret-at-least-32-characters',
                           'ACCOUNTS_ISSUER': ISSUER})
    with instance.app_context():
        assert integration_client_id('csupor') == 'staff-tenant-17'
        assert integration_client_id('safety') == 'training-tenant-42'
        assert all(value['code'] is None for value in instance.extensions['accounts_integration_status'].values())
        from accounts.models import db
        db.session.remove()
        db.engine.dispose()


def test_factory_without_role_environment_has_no_default_ids_and_still_starts(tmp_path, monkeypatch):
    monkeypatch.delenv('OIDC_CLIENT_ID', raising=False)
    monkeypatch.delenv('SAFETY_OIDC_CLIENT_ID', raising=False)
    instance = create_app({'TESTING': True, 'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'empty.db'),
                           'ACCOUNTS_SECRET_KEY': 'empty-config-secret-at-least-32-characters',
                           'ACCOUNTS_ISSUER': ISSUER})
    with instance.app_context():
        assert integration_client_id('csupor') is None
        assert integration_client_id('safety') is None
    assert instance.test_client().get('/login', base_url=ISSUER).status_code == 200
    response = instance.test_client().post('/api/csupor/workplaces', base_url=ISSUER,
        headers={'Authorization': 'Basic ' + b64encode(b'csupor:old-secret').decode()})
    assert response.status_code == 503
    with instance.app_context():
        from accounts.models import db
        db.session.remove()
        db.engine.dispose()
