"""Bearer-authorised application logout and safe browser cookie cleanup."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import threading
import time
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
from sqlalchemy.exc import OperationalError

from accounts import create_app
from accounts.models import Account, AccessToken, Client, Membership, db

ISSUER = 'https://accounts.example.invalid'
TOKENS = {'csupor': 'owner-csupor-private-token', 'safety': 'owner-safety-private-token',
          'other': 'other-account-private-token'}


@pytest.fixture(scope='module')
def pem():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


@pytest.fixture
def app(tmp_path, pem):
    key = tmp_path / 'key.pem'
    key.write_bytes(pem)
    app = create_app({
        'TESTING': True, 'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'accounts.db'),
        'ACCOUNTS_SECRET_KEY': 'independent-test-account-service-secret',
        'ACCOUNTS_ISSUER': ISSUER, 'ACCOUNTS_SIGNING_KEY_FILE': str(key),
    })
    app.config['TEST_OWNER'] = str(uuid4())
    app.config['TEST_OTHER'] = str(uuid4())
    with app.app_context():
        db.create_all()
        owner, other = app.config['TEST_OWNER'], app.config['TEST_OTHER']
        db.session.add_all([
            Account(subject=owner, username='owner', email='owner@example.invalid', password_hash='unused', version=1),
            Account(subject=other, username='other', email='other@example.invalid', password_hash='unused', version=1),
            Client(client_id='csupor', name='CSUPOR', secret_hash='unused', redirect_uris=['https://csupor.example.invalid/auth/callback']),
            Client(client_id='safety', name='Safety', secret_hash='unused', redirect_uris=['https://safety.example.invalid/auth/callback']),
        ])
        db.session.flush()
        for subject, client_id, raw in ((owner, 'csupor', TOKENS['csupor']),
                                        (owner, 'safety', TOKENS['safety']),
                                        (other, 'csupor', TOKENS['other'])):
            db.session.add(Membership(account_subject=subject, client_id=client_id))
            db.session.add(AccessToken(token_hash=sha256(raw.encode()).hexdigest(), account_subject=subject,
                                       client_id=client_id, version=1, scope='openid profile',
                                       expires_at=int(time.time()) + 300))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def authenticated_browser(app, subject=None):
    browser = app.test_client()
    with browser.session_transaction(base_url=ISSUER) as session:
        session.update(account_subject=subject or app.config['TEST_OWNER'], account_version=1,
                       account_auth_time=int(time.time()), csrf_token='browser-csrf-token')
    return browser


def bearer(raw):
    return {'Authorization': 'Bearer ' + raw}


def version(app, subject=None):
    with app.app_context():
        return db.session.get(Account, subject or app.config['TEST_OWNER']).version


def test_browser_cookies_and_query_parameters_cannot_authorise_global_logout(app):
    browser = authenticated_browser(app)
    assert browser.get('/end-session', base_url=ISSUER).status_code == 405
    response = browser.post('/end-session', base_url=ISSUER,
                            data={'csrf_token': 'browser-csrf-token', 'account_subject': app.config['TEST_OWNER']})
    assert response.status_code == 401
    response = browser.post('/end-session?access_token=' + TOKENS['csupor'], base_url=ISSUER)
    assert response.status_code == 401
    assert version(app) == 1


def test_backchannel_revokes_only_bearer_owner_and_stale_browser_logout_needs_no_click(app):
    browser = authenticated_browser(app)
    second_browser = authenticated_browser(app)
    response = browser.post('/end-session', base_url=ISSUER, headers=bearer(TOKENS['csupor']),
                            data={'account_subject': app.config['TEST_OTHER'], 'next': 'https://untrusted.invalid/'})
    assert response.status_code == 204
    assert response.data == b''
    assert 'Location' not in response.headers
    assert version(app) == 2
    assert version(app, app.config['TEST_OTHER']) == 1
    for key in ('csupor', 'safety'):
        assert browser.get('/userinfo', base_url=ISSUER, headers=bearer(TOKENS[key])).status_code == 401
    assert browser.get('/userinfo', base_url=ISSUER, headers=bearer(TOKENS['other'])).status_code == 200
    assert second_browser.get('/account', base_url=ISSUER).status_code == 302
    response = browser.get('/logout', base_url=ISSUER, follow_redirects=True)
    assert response.status_code == 200
    assert response.request.path == '/login'
    assert 'Kijelentkezés a közös fiókból'.encode() not in response.data
    with browser.session_transaction(base_url=ISSUER) as session:
        assert 'account_subject' not in session


def test_repeated_old_bearer_cannot_revoke_a_new_session(app):
    browser = app.test_client()
    assert browser.post('/end-session', base_url=ISSUER, headers=bearer(TOKENS['csupor'])).status_code == 204
    new_browser = authenticated_browser(app)
    with new_browser.session_transaction(base_url=ISSUER) as session:
        session['account_version'] = 2
    assert browser.post('/end-session', base_url=ISSUER, headers=bearer(TOKENS['csupor'])).status_code == 401
    assert version(app) == 2
    assert new_browser.get('/account', base_url=ISSUER).status_code == 200


def test_simultaneous_authenticated_requests_only_increment_version_once(app, monkeypatch):
    barrier = threading.Barrier(2)
    original = AccessToken.is_revoked

    def validate_before_either_revokes(token):
        revoked = original(token)
        barrier.wait(timeout=5)
        return revoked

    monkeypatch.setattr(AccessToken, 'is_revoked', validate_before_either_revokes)

    def logout():
        return app.test_client().post('/end-session', base_url=ISSUER,
                                      headers=bearer(TOKENS['csupor'])).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: logout(), range(2)))
    assert results == [204, 204]
    assert version(app) == 2


@pytest.mark.parametrize('invalid', ['expired', 'wrong-scope', 'inactive-client', 'missing-membership'])
def test_invalid_bearer_cannot_log_out_account(app, invalid):
    with app.app_context():
        token = db.session.get(AccessToken, sha256(TOKENS['csupor'].encode()).hexdigest())
        if invalid == 'expired':
            token.expires_at = int(time.time()) - 1
        elif invalid == 'wrong-scope':
            token.scope = 'profile'
        elif invalid == 'inactive-client':
            db.session.get(Client, 'csupor').active = False
        else:
            db.session.delete(db.session.get(Membership, (app.config['TEST_OWNER'], 'csupor')))
        db.session.commit()
    response = app.test_client().post('/end-session', base_url=ISSUER, headers=bearer(TOKENS['csupor']))
    assert response.status_code in {401, 403}
    assert version(app) == 1


def test_valid_different_browser_account_keeps_manual_csrf_confirmation(app):
    browser = authenticated_browser(app, app.config['TEST_OTHER'])
    assert browser.post('/end-session', base_url=ISSUER, headers=bearer(TOKENS['csupor'])).status_code == 204
    response = browser.get('/logout', base_url=ISSUER)
    assert response.status_code == 200
    assert 'Kijelentkezés a közös fiókból'.encode() in response.data
    assert version(app, app.config['TEST_OTHER']) == 1
    assert browser.post('/logout', base_url=ISSUER).status_code == 400
    assert version(app, app.config['TEST_OTHER']) == 1


def test_database_failure_is_sanitised_and_rolls_back_version(app, monkeypatch, caplog):
    def fail_commit():
        raise OperationalError('private SQL', {'access_token': TOKENS['csupor']}, Exception('private details'))

    with app.app_context():
        monkeypatch.setattr(db.session, 'commit', fail_commit)
        response = app.test_client().post('/end-session', base_url=ISSUER, headers=bearer(TOKENS['csupor']))
        assert response.status_code == 503
        assert response.json == {'error': 'temporarily_unavailable'}
        assert db.session.get(Account, app.config['TEST_OWNER']).version == 1
    for private in ('private SQL', 'private details', TOKENS['csupor']):
        assert private not in caplog.text
        assert private.encode() not in response.data
