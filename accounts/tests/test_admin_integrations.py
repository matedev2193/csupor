"""Deployment-selected client IDs govern setup and operator instructions."""
import pytest

from accounts.admin import AccountAdmin
from accounts.models import Account, Client, Membership, RateLimit, db
from accounts.tests.test_admin import (
    app, pem, payload, bootstrap, submit_setup, admin_post, get, assert_unclaimed,
    ISSUER, CALLBACK, CLIENT_SECRET,
)


def test_setup_imports_into_configured_client_and_preserves_export_protocol(app, payload):
    app.config.update(OIDC_CLIENT_ID='nursery-office.prod', SAFETY_OIDC_CLIENT_ID='training-prod')
    browser = bootstrap(app, payload)
    with app.app_context():
        assert [client.client_id for client in Client.query.all()] == ['nursery-office.prod']
        assert {membership.client_id for membership in Membership.query.all()} == {'nursery-office.prod'}
        assert Account.query.count() == 2
        assert AccountAdmin.query.count() == 1
    response = get(browser, '/admin/clients/nursery-office.prod/edit')
    for public in ('OIDC_CLIENT_ID', 'nursery-office.prod', 'OIDC_CLIENT_SECRET', 'AUTH_MODE', CALLBACK):
        assert public.encode() in response.data
    assert b'SAFETY_OIDC_CLIENT_SECRET' not in response.data
    assert CLIENT_SECRET.encode() not in response.data


@pytest.mark.parametrize('config,code', [
    ({'OIDC_CLIENT_ID': None}, 'ACCOUNTS_INTEGRATION_CSUPOR_MISSING'),
    ({'OIDC_CLIENT_ID': ''}, 'ACCOUNTS_INTEGRATION_CSUPOR_MISSING'),
    ({'OIDC_CLIENT_ID': 'not-a-valid/id-private'}, 'ACCOUNTS_INTEGRATION_CSUPOR_INVALID'),
    ({'OIDC_CLIENT_ID': 'munkavedelem'}, 'ACCOUNTS_INTEGRATION_CSUPOR_CONFLICT'),
])
def test_setup_invalid_integration_is_visible_and_cannot_write(app, payload, config, code):
    app.config.update(config)
    browser = app.test_client()
    page = get(browser, '/setup')
    assert page.status_code == 200
    assert code.encode() in page.data and b'OIDC_CLIENT_ID' in page.data
    assert b'type="submit" disabled' in page.data
    response = submit_setup(browser, payload)
    assert response.status_code == 503
    assert b'ACCOUNTS_SETUP_CLIENT_CONFIG' in response.data
    assert b'not-a-valid/id-private' not in response.data
    assert_unclaimed(app)
    with app.app_context():
        assert RateLimit.query.count() == 0


def test_safety_creation_and_secret_labels_follow_configured_id(app, payload):
    app.config['SAFETY_OIDC_CLIENT_ID'] = 'hazard-training.2026'
    browser = bootstrap(app, payload)
    page = get(browser, '/admin')
    assert b'/admin/clients/new?integration_role=safety' in page.data
    page = get(browser, '/admin/clients/new?integration_role=safety')
    assert b'value="hazard-training.2026"' in page.data
    assert b'SAFETY_OIDC_CLIENT_SECRET' in page.data
    assert 'Tűz- és munkavédelem'.encode() in page.data
    response = admin_post(browser, '/admin/clients/new?integration_role=safety', {
        'client_id': 'hazard-training.2026', 'integration_role': 'safety',
        'name': 'Training', 'redirect_uris': 'https://training.example.invalid/auth/callback',
        'client_secret': 'private-training-client-secret-more-than-32-characters',
    })
    assert response.status_code == 302
    page = get(browser, response.location)
    for public in ('SAFETY_OIDC_ISSUER', 'SAFETY_OIDC_CLIENT_ID', 'SAFETY_OIDC_REDIRECT_URI',
                   'SAFETY_OIDC_CLIENT_SECRET', 'hazard-training.2026', ISSUER):
        assert public.encode() in page.data
    assert b'private-training-client-secret' not in page.data
    assert b'<strong>OIDC_CLIENT_SECRET</strong>' not in page.data
    assert b'/admin/clients/hazard-training.2026/edit' in get(browser, '/admin').data


def test_role_creation_rejects_stale_or_manipulated_identifier(app, payload):
    browser = bootstrap(app, payload)
    app.config['SAFETY_OIDC_CLIENT_ID'] = 'new-training-id'
    response = admin_post(browser, '/admin/clients/new?integration_role=safety', {
        'client_id': 'munkavedelem', 'integration_role': 'safety', 'name': 'Training',
        'redirect_uris': 'https://training.example.invalid/auth/callback', 'client_secret': CLIENT_SECRET,
    })
    assert response.status_code == 200
    assert 'nem egyezik'.encode() in response.data
    with app.app_context():
        assert Client.query.count() == 1


@pytest.mark.parametrize('identifier', [None, 'invalid/private-value', 'CSUPOR'])
def test_missing_or_conflicting_safety_notice_does_not_break_login(app, payload, identifier):
    browser = bootstrap(app, payload)
    app.config['SAFETY_OIDC_CLIENT_ID'] = identifier
    assert get(browser, '/account').status_code == 200
    assert get(app.test_client(), '/login').status_code == 200
    page = get(browser, '/admin')
    assert page.status_code == 200
    assert b'SAFETY_OIDC_CLIENT_ID' in page.data
    assert b'/admin/clients/new?integration_role=safety' not in page.data
    assert b'invalid/private-value' not in page.data
    response = admin_post(browser, '/admin/clients/new?integration_role=safety', {
        'client_id': 'training', 'integration_role': 'safety', 'name': 'Training',
        'redirect_uris': CALLBACK, 'client_secret': CLIENT_SECRET,
    })
    assert response.status_code == 200
    with app.app_context():
        assert Client.query.count() == 1


def test_legacy_name_or_id_does_not_infer_integration_role(app, payload):
    app.config['SAFETY_OIDC_CLIENT_ID'] = 'configured-training-id'
    browser = bootstrap(app, payload)
    response = admin_post(browser, '/admin/clients/new', {
        'client_id': 'munkavedelem', 'name': 'Tűz- és munkavédelem',
        'redirect_uris': 'https://training.example.invalid/auth/callback', 'client_secret': CLIENT_SECRET,
    })
    assert response.status_code == 302
    page = get(browser, response.location)
    assert b'SAFETY_OIDC_CLIENT_SECRET' not in page.data
    assert b'<strong>OIDC_CLIENT_SECRET</strong>' not in page.data
    assert b'value="munkavedelem"' not in get(browser, '/admin/clients/new?client_id=munkavedelem').data


def test_safety_setting_is_not_required_for_initial_csupor_import(app, payload):
    app.config['SAFETY_OIDC_CLIENT_ID'] = None
    browser = bootstrap(app, payload)
    assert get(browser, '/admin').status_code == 200
    with app.app_context():
        assert Client.query.one().client_id == app.config['OIDC_CLIENT_ID']
