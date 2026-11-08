"""Central account administration and links to authorised applications."""
from hashlib import sha256
import importlib
import time

import pytest
from sqlalchemy.exc import IntegrityError

from accounts.admin import AccountAdmin
from accounts.models import Account, AccessToken, AuthorizationCode, Client, Membership, db
from accounts.tests.test_end_session import app, pem, authenticated_browser, ISSUER, TOKENS


@pytest.fixture
def administrator(app):
    with app.app_context():
        db.session.add(AccountAdmin(account_subject=app.config['TEST_OWNER']))
        db.session.commit()
    return authenticated_browser(app)


def post(browser, path, values=None, csrf=True):
    data = {'csrf_token': 'browser-csrf-token'} if csrf else {}
    data.update(values or {})
    return browser.post(path, base_url=ISSUER, data=data)


def new_account(**changes):
    return dict(username='new.employee', email='New.Employee@example.invalid',
                password='A-new-account-password', password_confirm='A-new-account-password',
                client_ids=['safety'], **changes)


def test_admin_creates_account_with_only_selected_access_and_no_admin_role(app, administrator):
    response = post(administrator, '/admin/accounts/new', new_account())
    assert response.status_code == 302
    with app.app_context():
        created = Account.query.filter_by(username='new.employee').one()
        assert created.email == 'new.employee@example.invalid'
        assert created.active and not created.email_verified and created.version == 1
        assert created.check_password('A-new-account-password')
        assert created.password_hash != 'A-new-account-password'
        assert db.session.get(AccountAdmin, created.subject) is None
        assert [m.client_id for m in Membership.query.filter_by(account_subject=created.subject)] == ['safety']
        subject = created.subject
    browser = authenticated_browser(app, subject)
    assert browser.get('/account', base_url=ISSUER).status_code == 200
    assert browser.get('/admin', base_url=ISSUER).status_code == 403


def test_creating_accounts_requires_admin_and_csrf(app, administrator):
    browser = authenticated_browser(app, app.config['TEST_OTHER'])
    assert browser.get('/admin/accounts/new', base_url=ISSUER).status_code == 403
    assert post(browser, '/admin/accounts/new', new_account()).status_code == 403
    assert post(administrator, '/admin/accounts/new', new_account(), csrf=False).status_code == 400
    assert app.test_client().get('/admin/accounts/new', base_url=ISSUER).status_code == 302
    with app.app_context():
        assert Account.query.count() == 2


@pytest.mark.parametrize('changes', [
    {'username': 'bad user'}, {'username': 'OWNER'}, {'email': 'other@example.invalid'},
    {'email': 'bad@example.invalid\r\nBcc: other@example.invalid'},
    {'password': 'short'}, {'password_confirm': 'different-password'}, {'client_ids': ['unknown']},
])
def test_invalid_new_accounts_do_not_write_or_echo_password(app, administrator, changes):
    values = new_account()
    values.update(changes)
    response = post(administrator, '/admin/accounts/new', values)
    assert response.status_code == 400
    assert b'A-new-account-password' not in response.data
    with app.app_context():
        assert Account.query.count() == 2


def test_inactive_application_not_offered_or_granted(app, administrator):
    with app.app_context():
        db.session.get(Client, 'safety').active = False
        db.session.commit()
    page = administrator.get('/admin/accounts/new', base_url=ISSUER)
    assert b'value="safety"' not in page.data
    assert post(administrator, '/admin/accounts/new', new_account()).status_code == 400


@pytest.mark.parametrize('revoke', ['admin_role', 'session_version'])
def test_admin_authority_is_rechecked_after_password_hashing(app, administrator, monkeypatch, revoke):
    module = importlib.import_module('accounts.admin')
    original = module.generate_password_hash

    def revoke_during_hash(password):
        with app.app_context():
            if revoke == 'admin_role':
                db.session.delete(db.session.get(AccountAdmin, app.config['TEST_OWNER']))
            else:
                db.session.get(Account, app.config['TEST_OWNER']).version += 1
            db.session.commit()
        return original(password)

    monkeypatch.setattr(module, 'generate_password_hash', revoke_during_hash)
    assert post(administrator, '/admin/accounts/new', new_account()).status_code == 403
    with app.app_context():
        assert Account.query.count() == 2


def test_create_database_failure_is_atomic_and_hides_raw_exception(app, administrator, monkeypatch):
    module = importlib.import_module('accounts.admin')

    def fail(*args, **kwargs):
        raise IntegrityError('secret-query', {'password': 'do-not-print'}, Exception('private-db-details'))

    monkeypatch.setattr(module.db.session, 'flush', fail)
    response = post(administrator, '/admin/accounts/new', new_account())
    assert response.status_code == 409
    for secret in (b'secret-query', b'do-not-print', b'private-db-details'):
        assert secret not in response.data
    monkeypatch.undo()
    with app.app_context():
        assert Account.query.count() == 2


def test_disable_revokes_tokens_and_browser_sessions_but_preserves_data(app, administrator):
    subject = app.config['TEST_OTHER']
    browser = authenticated_browser(app, subject)
    with app.app_context():
        account = db.session.get(Account, subject)
        before = (account.username, account.email, account.password_hash)
        db.session.add(AuthorizationCode(code_hash='c' * 64, client_id='csupor', account_subject=subject,
                                        version=1, redirect_uri='https://csupor.example.invalid/auth/callback',
                                        scope='openid', nonce='test-nonce', code_challenge='x' * 43,
                                        code_challenge_method='S256', auth_time=int(time.time()),
                                        expires_at=int(time.time()) + 300))
        db.session.commit()
    response = post(administrator, f'/admin/accounts/{subject}/status', {'account_version': '1', 'active': 'false'})
    assert response.status_code == 302
    assert browser.get('/account', base_url=ISSUER).status_code == 302
    with app.app_context():
        account = db.session.get(Account, subject)
        assert not account.active and account.version == 2
        assert (account.username, account.email, account.password_hash) == before
        assert db.session.get(Membership, (subject, 'csupor')) is not None
        assert AccessToken.query.filter_by(account_subject=subject).count() == 0
        assert AuthorizationCode.query.filter_by(account_subject=subject).count() == 0
        assert db.session.get(AccessToken, sha256(TOKENS['csupor'].encode()).hexdigest()) is not None
    response = post(administrator, f'/admin/accounts/{subject}/status', {'account_version': '2', 'active': 'true'})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Account, subject).active
        assert db.session.get(Account, subject).version == 3
        assert db.session.get(AccountAdmin, subject) is None
    assert browser.get('/account', base_url=ISSUER).status_code == 302


def test_disable_is_admin_csrf_and_revision_guarded(app, administrator):
    subject = app.config['TEST_OTHER']
    path = f'/admin/accounts/{subject}/status'
    browser = authenticated_browser(app, subject)
    assert post(browser, path, {'account_version': '1', 'active': 'false'}).status_code == 403
    assert post(administrator, path, {'account_version': '1', 'active': 'false'}, csrf=False).status_code == 400
    assert post(administrator, path, {'account_version': '2', 'active': 'false'}).status_code == 409
    assert post(administrator, path, {'account_version': '1', 'active': 'unknown'}).status_code == 400
    with app.app_context():
        assert db.session.get(Account, subject).active
        assert db.session.get(Account, subject).version == 1


def test_admin_cannot_disable_their_own_account(app, administrator):
    subject = app.config['TEST_OWNER']
    assert post(administrator, f'/admin/accounts/{subject}/status',
                {'account_version': '1', 'active': 'false'}).status_code == 400
    with app.app_context():
        assert db.session.get(Account, subject).active
        assert AccountAdmin.query.count() == 1
    assert administrator.get('/admin', base_url=ISSUER).status_code == 200


def test_account_launch_links_only_show_active_granted_applications(app, administrator):
    page = administrator.get('/account', base_url=ISSUER).data
    assert b'href="https://csupor.example.invalid/"' in page
    assert b'href="https://safety.example.invalid/"' in page
    assert b'href="https://csupor.example.invalid/auth/callback"' not in page
    employee_page = authenticated_browser(app, app.config['TEST_OTHER']).get('/account', base_url=ISSUER).data
    assert b'href="https://csupor.example.invalid/"' in employee_page
    assert b'href="https://safety.example.invalid/"' not in employee_page
    with app.app_context():
        db.session.get(Client, 'safety').active = False
        db.session.commit()
    assert b'https://safety.example.invalid/' not in administrator.get('/account', base_url=ISSUER).data


@pytest.mark.parametrize('uri', ['javascript:alert(1)', '//evil.invalid/path',
                                'https://name:password@evil.invalid/callback', 'http://evil.invalid/callback'])
def test_invalid_registered_legacy_uri_never_becomes_a_link(app, administrator, uri):
    with app.app_context():
        db.session.get(Client, 'safety').redirect_uris = [uri]
        db.session.commit()
    response = administrator.get('/account', base_url=ISSUER)
    assert response.status_code == 200
    assert b'evil.invalid' not in response.data
    assert b'javascript:' not in response.data


def test_admin_page_links_to_registration_email_and_application(app, administrator):
    response = administrator.get('/admin', base_url=ISSUER)
    assert response.status_code == 200
    for target in ('/admin/accounts/new', '/admin/email-settings', 'https://csupor.example.invalid/'):
        assert ('href="' + target + '"').encode() in response.data
    assert administrator.get('/admin/email-settings', base_url=ISSUER).status_code == 200
