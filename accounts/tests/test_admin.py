"""Real browser bootstrap transactions and application-access administration."""
import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
import json
import re
import threading
import time
from uuid import uuid4

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash

from accounts import create_app
from accounts.admin import AccountAdmin, AccountSetupState, prepare_setup_state
from accounts.importing import TRANSFER_PREFIX
from accounts.models import Account, AccessToken, AuthorizationCode, Client, Membership, db

ISSUER = 'https://accounts.example.invalid'
SETUP_TOKEN = 'setup-token-with-more-than-thirty-two-random-characters'
TRANSFER_KEY = 'transfer-key-with-more-than-thirty-two-random-characters'
CLIENT_SECRET = 'csupor-client-secret-with-more-than-thirty-two-characters'
CALLBACK = 'https://csupor.example.invalid/auth/callback'
OWNER_PASSWORD = 'Existing-owner-password'


@pytest.fixture(scope='module')
def pem():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


@pytest.fixture
def app(tmp_path, pem):
    key = tmp_path / 'key.pem'
    key.write_bytes(pem)
    value = create_app({
        'TESTING': True, 'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'accounts.db'),
        'ACCOUNTS_SECRET_KEY': 'independent-test-account-service-secret',
        'ACCOUNTS_ISSUER': ISSUER, 'ACCOUNTS_SIGNING_KEY_FILE': str(key),
        'ACCOUNTS_SETUP_TOKEN': SETUP_TOKEN,
    })
    with value.app_context():
        db.create_all()
        prepare_setup_state()
    yield value
    with value.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def payload():
    return {'version': 1, 'source': 'csupor', 'issuer': ISSUER, 'users': [
        {'subject': str(uuid4()), 'username': 'owner', 'email': 'owner@example.invalid',
         'password_hash': generate_password_hash(OWNER_PASSWORD, method='pbkdf2:sha256:1000')},
        {'subject': str(uuid4()), 'username': 'employee', 'email': 'employee@example.invalid',
         'password_hash': generate_password_hash('Existing-employee-password', method='pbkdf2:sha256:1000')},
    ]}


def encrypted(payload, *, timestamp=None, plaintext=None):
    key = base64.urlsafe_b64encode(sha256(b'micimacko-account-transfer-v1\0' + TRANSFER_KEY.encode()).digest())
    content = plaintext if plaintext is not None else json.dumps(payload).encode()
    cipher = Fernet(key)
    token = cipher.encrypt(content) if timestamp is None else cipher.encrypt_at_time(content, timestamp)
    return TRANSFER_PREFIX + token


def get(browser, path):
    return browser.get(path, base_url=ISSUER)


def csrf(browser, path='/setup'):
    response = get(browser, path)
    assert response.status_code == 200
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match, response.data.decode()
    return match.group(1).decode()


def setup_form(payload, **overrides):
    values = {
        'setup_token': SETUP_TOKEN, 'transfer_key': TRANSFER_KEY,
        'admin_login': 'owner', 'admin_password': OWNER_PASSWORD,
        'csupor_redirect_uri': CALLBACK, 'csupor_client_secret': CLIENT_SECRET,
        'transfer_file': (BytesIO(encrypted(payload)), 'accounts.transfer'),
    }
    values.update(overrides)
    return values


def submit_setup(browser, payload, **overrides):
    values = setup_form(payload, **overrides)
    values['csrf_token'] = csrf(browser)
    return browser.post('/setup', data=values, base_url=ISSUER)


def assert_unclaimed(app):
    with app.app_context():
        assert not db.session.get(AccountSetupState, 'initial-admin').completed
        assert AccountAdmin.query.count() == 0
        assert Account.query.count() == 0
        assert Client.query.count() == 0
        assert Membership.query.count() == 0


def bootstrap(app, payload):
    browser = app.test_client()
    response = submit_setup(browser, payload)
    assert response.status_code == 302, response.data.decode()
    assert response.location.endswith('/admin')
    return browser


def admin_post(browser, path, data):
    return browser.post(path, data={'csrf_token': csrf(browser, '/account'), **data}, base_url=ISSUER)


def create_safety(browser):
    response = admin_post(browser, '/admin/clients/new', {
        'client_id': 'munkavedelem', 'name': 'Tűz- és munkavédelem',
        'redirect_uris': 'https://safety.example.invalid/auth/callback',
        'client_secret': 'safety-client-secret-that-is-at-least-thirty-two-characters',
    })
    assert response.status_code == 302, response.data.decode()


def test_setup_preserves_identity_password_and_only_grants_csupor(app, payload):
    browser = bootstrap(app, payload)
    with app.app_context():
        assert Account.query.count() == 2
        assert Client.query.count() == 1
        assert AccountAdmin.query.one().account_subject == payload['users'][0]['subject']
        assert db.session.get(AccountSetupState, 'initial-admin').completed
        for row in payload['users']:
            account = db.session.get(Account, row['subject'])
            assert account.username == row['username']
            assert account.email == row['email']
            assert account.password_hash == row['password_hash']
            assert not account.email_verified
            assert db.session.get(Membership, (row['subject'], 'csupor'))
        assert db.session.get(Account, payload['users'][0]['subject']).check_password(OWNER_PASSWORD)
        client = db.session.get(Client, 'csupor')
        assert client.check_client_secret(CLIENT_SECRET)
        assert client.secret_hash != CLIENT_SECRET
    assert get(browser, '/admin').status_code == 200
    public = get(browser, '/admin/clients/csupor/edit').data.decode()
    for value in ('AUTH_MODE', 'OIDC_ISSUER', 'OIDC_CLIENT_ID', 'OIDC_REDIRECT_URI', 'OIDC_CLIENT_SECRET', CALLBACK, ISSUER):
        assert value in public
    for value in (SETUP_TOKEN, TRANSFER_KEY, CLIENT_SECRET, OWNER_PASSWORD, payload['users'][0]['password_hash']):
        assert value not in public


@pytest.mark.parametrize('overrides,status,code', [
    ({'setup_token': 'wrong-but-long-enough-setup-secret-for-this-test'}, 403, 'TOKEN'),
    ({'transfer_key': 'wrong-but-long-enough-transfer-secret-for-this-test'}, 400, 'TRANSFER_DECRYPT'),
    ({'admin_password': 'wrong-password'}, 400, 'OWNER_LOGIN'),
    ({'admin_login': 'missing-account'}, 400, 'OWNER_LOGIN'),
    ({'csupor_redirect_uri': 'http://csupor.example.invalid/auth/callback'}, 400, 'CALLBACK'),
    ({'csupor_redirect_uri': 'https://csupor.example.invalid/wrong'}, 400, 'CALLBACK'),
    ({'csupor_redirect_uri': CALLBACK + '?next=evil'}, 400, 'CALLBACK'),
    ({'csupor_client_secret': 'short'}, 400, 'CLIENT_SECRET'),
])
def test_setup_bad_proofs_or_client_config_leave_no_partial_state(app, payload, overrides, status, code, caplog):
    response = submit_setup(app.test_client(), payload, **overrides)
    assert response.status_code == status
    assert ('ACCOUNTS_SETUP_' + code).encode() in response.data
    assert '[ACCOUNTS_SETUP_' + code + ']' in caplog.text
    for secret in (SETUP_TOKEN, TRANSFER_KEY, CLIENT_SECRET, OWNER_PASSWORD):
        assert secret.encode() not in response.data
        assert secret not in caplog.text
    assert_unclaimed(app)


def test_setup_requires_csrf_before_any_import(app, payload):
    browser = app.test_client()
    csrf(browser)
    response = browser.post('/setup', data=setup_form(payload), base_url=ISSUER)
    assert response.status_code == 400
    assert_unclaimed(app)


@pytest.mark.parametrize('invalid', ['issuer', 'duplicate-subject', 'duplicate-name', 'duplicate-email', 'ambiguous-login', 'unsafe-hash'])
def test_export_identity_conflicts_are_atomic(app, payload, invalid):
    first, second = payload['users']
    if invalid == 'issuer':
        payload['issuer'] = 'https://other.example.invalid'
    elif invalid == 'duplicate-subject':
        second['subject'] = first['subject']
    elif invalid == 'duplicate-name':
        second['username'] = first['username'].upper()
    elif invalid == 'duplicate-email':
        second['email'] = first['email']
    elif invalid == 'ambiguous-login':
        second['username'] = first['email']
    else:
        second['password_hash'] = 'scrypt:1073741824:8:8$salt$' + 'a' * 128
    assert submit_setup(app.test_client(), payload).status_code == 400
    assert_unclaimed(app)


@pytest.mark.parametrize('invalid', ['expired', 'future', 'plaintext', 'duplicate-json-key', 'deep-json'])
def test_invalid_encrypted_package_cannot_import(app, payload, invalid):
    if invalid == 'expired':
        content = encrypted(payload, timestamp=int(time.time()) - 86401)
    elif invalid == 'future':
        content = encrypted(payload, timestamp=int(time.time()) + 120)
    elif invalid == 'plaintext':
        content = json.dumps(payload).encode()
    elif invalid == 'duplicate-json-key':
        content = encrypted(payload, plaintext=b'{"version":1,"version":1}')
    else:
        content = encrypted(payload, plaintext=b'[' * 2000 + b'0' + b']' * 2000)
    response = submit_setup(app.test_client(), payload, transfer_file=(BytesIO(content), 'accounts.transfer'))
    assert response.status_code == 400
    if invalid == 'expired':
        assert b'ACCOUNTS_SETUP_TRANSFER_EXPIRED' in response.data
    elif invalid == 'future':
        assert b'ACCOUNTS_SETUP_TRANSFER_TIME' in response.data
    assert_unclaimed(app)


def test_setup_token_failure_never_reveals_export_diagnostics(app, payload, monkeypatch):
    def must_not_decrypt(*args, **kwargs):
        raise AssertionError('Untrusted request reached export decryption')
    monkeypatch.setattr('accounts.admin.decrypt_transfer', must_not_decrypt)
    response = submit_setup(app.test_client(), payload, setup_token='wrong-' + 'x' * 40)
    assert response.status_code == 403
    assert b'ACCOUNTS_SETUP_TOKEN' in response.data
    assert b'ACCOUNTS_SETUP_TRANSFER_' not in response.data
    assert_unclaimed(app)


def test_issuer_error_is_actionable_without_echoing_export(app, payload, caplog):
    payload['issuer'] = 'https://private-export-host.example.invalid'
    response = submit_setup(app.test_client(), payload)
    assert response.status_code == 400
    assert b'ACCOUNTS_SETUP_EXPORT_ISSUER' in response.data
    for private in (payload['issuer'], payload['users'][0]['email'], payload['users'][0]['password_hash']):
        assert private.encode() not in response.data
        assert private not in caplog.text
    assert_unclaimed(app)


def test_database_failure_logs_only_numeric_code_and_rolls_back_import(app, payload, monkeypatch, caplog):
    from accounts.admin import apply_import

    def fail_after_rows(*args, **kwargs):
        apply_import(*args, **kwargs)
        original = Exception('private database details ' + OWNER_PASSWORD)
        original.errno = 1142
        raise OperationalError('INSERT private_sql', {'secret': CLIENT_SECRET}, original)

    monkeypatch.setattr('accounts.admin.apply_import', fail_after_rows)
    response = submit_setup(app.test_client(), payload)
    assert response.status_code == 400
    assert b'ACCOUNTS_SETUP_DATABASE' in response.data
    assert b'1142' in response.data
    assert '[ACCOUNTS_SETUP_DATABASE] database_errno=1142' in caplog.text
    for private in (SETUP_TOKEN, TRANSFER_KEY, CLIENT_SECRET, OWNER_PASSWORD,
                    'private_sql', 'private database details', payload['users'][0]['password_hash']):
        assert private.encode() not in response.data
        assert private not in caplog.text
    assert_unclaimed(app)


def test_conflict_never_overwrites_existing_account_and_rolls_back_client(app, payload):
    original = dict(payload['users'][1])
    original['password_hash'] = generate_password_hash('Changed-central-password', method='pbkdf2:sha256:1000')
    with app.app_context():
        db.session.add(Account(**original, active=False, version=8, email_verified=True))
        db.session.commit()
    assert submit_setup(app.test_client(), payload).status_code == 400
    with app.app_context():
        existing = Account.query.one()
        assert existing.password_hash == original['password_hash']
        assert existing.version == 8 and existing.email_verified and not existing.active
        assert Client.query.count() == Membership.query.count() == AccountAdmin.query.count() == 0
        assert not db.session.get(AccountSetupState, 'initial-admin').completed


def test_existing_inactive_owner_cannot_be_promoted_and_client_is_rolled_back(app, payload):
    with app.app_context():
        db.session.add(Account(**payload['users'][0], active=False))
        db.session.commit()
    assert submit_setup(app.test_client(), payload).status_code == 400
    with app.app_context():
        assert Account.query.count() == 1
        assert not Account.query.one().active
        assert Client.query.count() == Membership.query.count() == AccountAdmin.query.count() == 0
        assert not db.session.get(AccountSetupState, 'initial-admin').completed


def test_setup_never_changes_existing_client_to_fit_upload(app, payload):
    with app.app_context():
        client = Client(client_id='csupor', name='Existing name', redirect_uris=['https://previous.example.invalid/auth/callback'])
        client.set_secret(CLIENT_SECRET)
        db.session.add(client)
        db.session.commit()
    assert submit_setup(app.test_client(), payload).status_code == 400
    with app.app_context():
        client = Client.query.one()
        assert client.name == 'Existing name'
        assert client.redirect_uris == ['https://previous.example.invalid/auth/callback']
        assert Account.query.count() == Membership.query.count() == AccountAdmin.query.count() == 0
        assert not db.session.get(AccountSetupState, 'initial-admin').completed


def test_replay_and_new_environment_token_cannot_reopen_after_last_admin_removed(app, payload):
    browser = bootstrap(app, payload)
    with app.app_context():
        db.session.execute(db.delete(AccountAdmin))
        db.session.commit()
    app.config['ACCOUNTS_SETUP_TOKEN'] = 'a-different-new-environment-setup-token-over-32-characters'
    assert get(browser, '/setup').status_code == 404
    assert browser.post('/setup', data=setup_form(payload), base_url=ISSUER).status_code == 404
    with app.app_context():
        assert db.session.get(AccountSetupState, 'initial-admin').completed
        assert AccountAdmin.query.count() == 0


def test_existing_inactive_admin_seals_setup_permanently(app, payload):
    with app.app_context():
        db.session.add(Account(**payload['users'][0], active=False))
        db.session.flush()
        db.session.add(AccountAdmin(account_subject=payload['users'][0]['subject']))
        db.session.commit()
        prepare_setup_state()
        assert db.session.get(AccountSetupState, 'initial-admin').completed
    assert get(app.test_client(), '/setup').status_code == 404


def test_missing_setup_token_disables_bootstrap(app):
    app.config['ACCOUNTS_SETUP_TOKEN'] = ''
    response = get(app.test_client(), '/login')
    assert response.status_code == 200 and b'href="/setup"' not in response.data
    assert get(app.test_client(), '/setup').status_code == 404


def test_setup_password_attempts_are_throttled_without_partial_import(app, payload):
    browser = app.test_client()
    for _ in range(10):
        assert submit_setup(browser, payload, admin_password='wrong').status_code == 400
    assert submit_setup(browser, payload).status_code == 429
    assert_unclaimed(app)


def test_simultaneous_setup_claims_grant_exactly_one_admin(app, payload, monkeypatch):
    import accounts.admin as admin_module
    # Synchronize after independent CSRF/password/throttle checks and just before
    # the real SQL claim. The database must arbitrate the two valid installers.
    barrier = threading.Barrier(2)
    original = admin_module._state
    def synchronized_state():
        barrier.wait(timeout=10)
        return original()
    monkeypatch.setattr(admin_module, '_state', synchronized_state)
    browsers = [app.test_client(), app.test_client()]
    forms = [setup_form(payload, csrf_token=csrf(browser)) for browser in browsers]
    forms[1].update(admin_login='employee', admin_password='Existing-employee-password')
    def claim(i):
        return browsers[i].post('/setup', data=forms[i], base_url=ISSUER).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(claim, range(2)))
    assert sorted(statuses) == [302, 404]
    with app.app_context():
        assert AccountAdmin.query.count() == 1
        assert Account.query.count() == Membership.query.count() == 2
        assert Client.query.count() == 1
        assert db.session.get(AccountSetupState, 'initial-admin').completed


def test_setup_accepts_real_export_larger_than_normal_form_limit(app, payload):
    for i in range(100):
        payload['users'].append({**payload['users'][1], 'subject': str(uuid4()),
                                 'username': f'employee-{i}', 'email': f'employee-{i}@example.invalid'})
    assert len(encrypted(payload)) > 16384
    browser = bootstrap(app, payload)
    assert browser.post('/login', data={'padding': 'x' * 20000}, base_url=ISSUER).status_code == 413
    with app.app_context():
        assert Account.query.count() == 102


def test_admin_authorization_requires_live_active_central_admin(app, payload):
    browser = bootstrap(app, payload)
    anonymous = app.test_client()
    assert get(anonymous, '/admin').status_code == 302
    assert anonymous.post('/admin/clients/new', base_url=ISSUER).status_code == 401
    employee = app.test_client()
    token = csrf(employee, '/login')
    assert employee.post('/login', data={'csrf_token': token, 'login': 'employee',
                                       'password': 'Existing-employee-password'}, base_url=ISSUER).status_code == 302
    assert get(employee, '/admin').status_code == 403
    assert employee.post('/admin/clients/new', base_url=ISSUER).status_code == 403
    with app.app_context():
        db.session.get(Account, payload['users'][0]['subject']).active = False
        db.session.commit()
    assert get(browser, '/admin').status_code == 302
    assert browser.post('/admin/clients/new', base_url=ISSUER).status_code == 401


def test_client_creation_maps_public_env_values_without_disclosing_secrets(app, payload):
    browser = bootstrap(app, payload)
    create_safety(browser)
    response = get(browser, '/admin/clients/munkavedelem/edit')
    for value in ('SAFETY_OIDC_ISSUER', 'SAFETY_OIDC_CLIENT_ID', 'SAFETY_OIDC_REDIRECT_URI',
                  'SAFETY_OIDC_CLIENT_SECRET', ISSUER, 'https://safety.example.invalid/auth/callback'):
        assert value.encode() in response.data
    assert b'safety-client-secret-that-is-at-least-thirty-two-characters' not in response.data
    with app.app_context():
        assert db.session.get(Client, 'munkavedelem').check_client_secret('safety-client-secret-that-is-at-least-thirty-two-characters')
        assert Membership.query.filter_by(client_id='munkavedelem').count() == 0


@pytest.mark.parametrize('changes', [
    {'client_id': 'bad/id'}, {'redirect_uris': 'http://unsafe.example.invalid/callback'},
    {'redirect_uris': 'https://safe.example.invalid/callback?query=1'},
    {'redirect_uris': 'https://safe.example.invalid/callback#fragment'},
    {'client_secret': 'short'}, {'name': ''}, {'active': 'other'}, {'client_id': 'csupor'},
])
def test_invalid_client_registration_changes_nothing(app, payload, changes):
    browser = bootstrap(app, payload)
    values = {'client_id': 'new-client', 'name': 'New client', 'redirect_uris': CALLBACK, 'client_secret': CLIENT_SECRET}
    values.update(changes)
    response = admin_post(browser, '/admin/clients/new', values)
    assert response.status_code == 200
    assert CLIENT_SECRET.encode() not in response.data
    with app.app_context():
        assert Client.query.count() == 1
        assert db.session.get(Client, 'csupor').name == 'CSUPOR'


def issue_rows(subject, client_id, suffix):
    db.session.add(AccessToken(token_hash=suffix * 64, account_subject=subject, client_id=client_id,
                               version=1, scope='openid', expires_at=int(time.time()) + 3600))
    db.session.add(AuthorizationCode(code_hash=suffix * 64, account_subject=subject, client_id=client_id,
        version=1, redirect_uri=CALLBACK, scope='openid', nonce='nonce-' + suffix,
        code_challenge='a' * 43, code_challenge_method='S256', auth_time=int(time.time()),
        expires_at=int(time.time()) + 60))


def test_membership_grant_and_revoke_invalidates_only_selected_application(app, payload):
    browser = bootstrap(app, payload)
    create_safety(browser)
    subject = payload['users'][1]['subject']
    path = '/admin/accounts/' + subject + '/access'
    assert admin_post(browser, path, {'client_ids': ['csupor', 'munkavedelem']}).status_code == 302
    with app.app_context():
        issue_rows(subject, 'csupor', 'a')
        issue_rows(subject, 'munkavedelem', 'b')
        db.session.commit()
    assert admin_post(browser, path, {'client_ids': ['csupor']}).status_code == 302
    with app.app_context():
        assert db.session.get(Membership, (subject, 'csupor'))
        assert not db.session.get(Membership, (subject, 'munkavedelem'))
        assert AccessToken.query.one().client_id == 'csupor'
        assert AuthorizationCode.query.one().client_id == 'csupor'
    assert admin_post(browser, path, {'client_ids': ['missing']}).status_code == 400
    with app.app_context():
        assert db.session.get(Membership, (subject, 'csupor'))


def test_admin_writes_require_csrf_and_secret_rotation_is_explicit(app, payload):
    browser = bootstrap(app, payload)
    subject = payload['users'][0]['subject']
    assert browser.post('/admin/accounts/' + subject + '/access', base_url=ISSUER).status_code == 400
    assert browser.post('/admin/clients/csupor/edit', base_url=ISSUER).status_code == 400
    replacement = 'replacement-client-secret-more-than-thirty-two-characters'
    values = {'name': 'CSUPOR edited', 'redirect_uris': CALLBACK, 'active': 'true', 'client_secret': replacement}
    assert admin_post(browser, '/admin/clients/csupor/edit', values).status_code == 200
    with app.app_context():
        assert db.session.get(Client, 'csupor').check_client_secret(CLIENT_SECRET)
        assert db.session.get(Client, 'csupor').name == 'CSUPOR'
        issue_rows(subject, 'csupor', 'a')
        db.session.commit()
    values['rotate_secret'] = '1'
    assert admin_post(browser, '/admin/clients/csupor/edit', values).status_code == 302
    with app.app_context():
        assert db.session.get(Client, 'csupor').check_client_secret(replacement)
        assert AccessToken.query.count() == AuthorizationCode.query.count() == 0
        assert db.session.get(Membership, (subject, 'csupor'))
