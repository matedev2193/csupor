"""Profile integrations follow configured client roles, never historical IDs."""
from base64 import b64encode
from hashlib import sha256
from io import BytesIO
import time

from PIL import Image
import pytest

from accounts.models import AccessToken, Client, Membership, db
from accounts.photo_models import AccountPhoto
from accounts.profile_models import AccountProfile
from accounts.tests.test_end_session import app, pem, authenticated_browser, bearer, ISSUER, TOKENS


HR_CLIENT = 'hr-portal.blue.2026'
SAFETY_CLIENT = 'training-portal.orange'
HR_TOKEN = 'configured-hr-owner-bearer'
SAFETY_TOKEN = 'configured-training-owner-bearer'
SECRET = 'configured-service-secret-1234567890'


def basic(client_id=HR_CLIENT):
    return {'Authorization': 'Basic ' + b64encode(f'{client_id}:{SECRET}'.encode()).decode()}


def photo_payload():
    image = BytesIO()
    Image.new('RGB', (48, 48), 'green').save(image, 'JPEG')
    return {'version': 'a' * 32, 'data': b64encode(image.getvalue()).decode(), 'width': 48, 'height': 48}


@pytest.fixture
def configured(app):
    app.config.update(OIDC_CLIENT_ID=HR_CLIENT, SAFETY_OIDC_CLIENT_ID=SAFETY_CLIENT)
    with app.app_context():
        db.session.get(Client, 'csupor').set_secret(SECRET)
        for client_id, raw in ((HR_CLIENT, HR_TOKEN), (SAFETY_CLIENT, SAFETY_TOKEN)):
            client = Client(client_id=client_id, name=client_id,
                            redirect_uris=['https://configured-hr.example.invalid/auth/callback'])
            client.set_secret(SECRET)
            db.session.add(client)
            db.session.flush()
            db.session.add(Membership(account_subject=app.config['TEST_OWNER'], client_id=client_id))
            db.session.add(AccessToken(token_hash=sha256(raw.encode()).hexdigest(),
                account_subject=app.config['TEST_OWNER'], client_id=client_id, version=1,
                scope='openid profile', expires_at=int(time.time()) + 300))
        # This person belongs only to the old CSUPOR client. Configuring a new
        # integration role must not widen the new service's batch access.
        db.session.add(AccountProfile(subject=app.config['TEST_OTHER'], fields={'full_name': 'Private other'},
                                      legacy_imported=True, revision='b' * 32))
        db.session.commit()
    return app


def test_arbitrary_configured_client_imports_and_reads_profile_and_photo(configured):
    client = configured.test_client()
    imported = client.post('/api/csupor/profile/import', base_url=ISSUER, headers=bearer(HR_TOKEN),
                           json={'fields': {'full_name': 'Configured owner'}})
    assert imported.status_code == 200
    assert imported.json['legacy_imported']
    assert imported.json['sub'] == configured.config['TEST_OWNER']
    assert client.get('/api/csupor/profile', base_url=ISSUER, headers=bearer(HR_TOKEN)).json == imported.json
    batch = client.post('/api/csupor/profiles/read', base_url=ISSUER, headers=basic(),
                        json={'subjects': [configured.config['TEST_OWNER'], configured.config['TEST_OTHER']]})
    assert batch.status_code == 200
    assert [row['sub'] for row in batch.json['profiles']] == [configured.config['TEST_OWNER']]
    assert 'Private other' not in batch.get_data(as_text=True)
    photo = client.post('/api/csupor/profile/photo/import', base_url=ISSUER,
                        headers=bearer(HR_TOKEN), json=photo_payload())
    assert photo.status_code == 200
    downloaded = client.get('/api/csupor/profile/photo', base_url=ISSUER, headers=bearer(HR_TOKEN))
    assert downloaded.status_code == 200
    assert downloaded.json['version'] == photo.json['version']
    with configured.app_context():
        assert db.session.get(AccountPhoto, configured.config['TEST_OWNER']) is not None


@pytest.mark.parametrize('token', [TOKENS['csupor'], TOKENS['safety'], SAFETY_TOKEN])
def test_historical_client_and_other_role_have_no_permanent_profile_privilege(configured, token):
    client = configured.test_client()
    for path, method in (('/api/csupor/profile', client.get),
                         ('/api/csupor/profile/import', client.post),
                         ('/api/csupor/profile/photo', client.get),
                         ('/api/csupor/profile/photo/import', client.post)):
        assert method(path, base_url=ISSUER, headers=bearer(token)).status_code == 403
    result = client.post('/api/csupor/profiles/read', base_url=ISSUER, headers=basic('csupor'),
                         json={'subjects': [configured.config['TEST_OWNER']]})
    assert result.status_code == 401
    with configured.app_context():
        assert db.session.get(AccountProfile, configured.config['TEST_OWNER']) is None
        assert db.session.get(AccountPhoto, configured.config['TEST_OWNER']) is None


@pytest.mark.parametrize('reason', ['scope', 'membership', 'inactive', 'expired'])
def test_configured_bearer_still_requires_scope_and_live_authorisation(configured, reason):
    with configured.app_context():
        token = db.session.get(AccessToken, sha256(HR_TOKEN.encode()).hexdigest())
        if reason == 'scope':
            token.scope = 'openid'
        elif reason == 'membership':
            db.session.delete(db.session.get(Membership, (configured.config['TEST_OWNER'], HR_CLIENT)))
        elif reason == 'inactive':
            db.session.get(Client, HR_CLIENT).active = False
        else:
            token.expires_at = int(time.time()) - 1
        db.session.commit()
    client = configured.test_client()
    for path, method in (('/api/csupor/profile', client.get),
                         ('/api/csupor/profile/import', client.post),
                         ('/api/csupor/profile/photo', client.get),
                         ('/api/csupor/profile/photo/import', client.post)):
        assert method(path, base_url=ISSUER, headers=bearer(HR_TOKEN)).status_code in {401, 403}


def test_profile_import_gate_and_backlink_follow_configured_membership(configured):
    browser = authenticated_browser(configured)
    page = browser.get('/profile', base_url=ISSUER)
    assert page.status_code == 200
    assert b'https://configured-hr.example.invalid/profile' in page.data
    assert b'https://csupor.example.invalid/profile' not in page.data
    assert 'Meglévő adataim betöltése'.encode() in page.data
    with configured.app_context():
        db.session.delete(db.session.get(Membership, (configured.config['TEST_OWNER'], HR_CLIENT)))
        db.session.commit()
    page = browser.get('/profile', base_url=ISSUER)
    assert b'name="full_name"' in page.data


@pytest.mark.parametrize('value', [None, '', 'invalid client id', SAFETY_CLIENT])
def test_missing_invalid_or_conflicting_role_disables_only_integration(configured, value):
    configured.config['OIDC_CLIENT_ID'] = value
    client = configured.test_client()
    for path, method in (('/api/csupor/profile', client.get),
                         ('/api/csupor/profile/import', client.post),
                         ('/api/csupor/profile/photo', client.get),
                         ('/api/csupor/profile/photo/import', client.post)):
        result = method(path, base_url=ISSUER, headers=bearer(HR_TOKEN))
        assert result.status_code == 503
        assert result.is_json and result.json['code']
    assert client.post('/api/csupor/profiles/read', base_url=ISSUER, headers=basic()).status_code == 503
    page = authenticated_browser(configured).get('/profile', base_url=ISSUER)
    assert page.status_code == 200
    assert b'https://configured-hr.example.invalid/profile' not in page.data
    assert b'name="full_name"' not in page.data
    assert b'ACCOUNTS_INTEGRATION_CSUPOR_' in page.data
    assert client.get('/userinfo', base_url=ISSUER, headers=bearer(HR_TOKEN)).status_code == 200


@pytest.mark.parametrize('value', [None, 'invalid client id', SAFETY_CLIENT])
@pytest.mark.parametrize('existing', [False, True])
def test_unavailable_integration_never_saves_blank_replacement_before_legacy_import(configured, value, existing):
    if existing:
        with configured.app_context():
            db.session.add(AccountProfile(subject=configured.config['TEST_OWNER'],
                fields={'phone_number': 'Central phone'}, legacy_imported=False, revision='c' * 32))
            db.session.commit()
    configured.config['OIDC_CLIENT_ID'] = value
    browser = authenticated_browser(configured)
    page = browser.get('/profile', base_url=ISSUER)
    assert page.status_code == 200
    assert b'name="full_name"' not in page.data
    assert b'ACCOUNTS_INTEGRATION_CSUPOR_' in page.data
    blocked = browser.post('/profile', base_url=ISSUER, data={
        'csrf_token': 'browser-csrf-token', 'profile_version': 'c' * 32 if existing else '',
        'full_name': '', 'phone_number': '',
    })
    assert blocked.status_code == 409
    assert b'name="full_name"' not in blocked.data
    with configured.app_context():
        record = db.session.get(AccountProfile, configured.config['TEST_OWNER'])
        if existing:
            assert record.fields == {'phone_number': 'Central phone'}
            assert record.revision == 'c' * 32
        else:
            assert record is None
    # The account system remains available while the optional integration is
    # unconfigured; restoring it must import actual legacy values, not nulls.
    assert browser.get('/account', base_url=ISSUER).status_code == 200
    assert configured.test_client().get('/login', base_url=ISSUER).status_code == 200
    configured.config['OIDC_CLIENT_ID'] = HR_CLIENT
    imported = configured.test_client().post('/api/csupor/profile/import', base_url=ISSUER,
        headers=bearer(HR_TOKEN), json={'fields': {'full_name': 'Actual legacy name',
                                                'phone_number': 'Legacy phone', 'tax_number': '1234567890'}})
    assert imported.status_code == 200
    assert imported.json['legacy_imported'] is True
    assert imported.json['fields']['full_name'] == 'Actual legacy name'
    assert imported.json['fields']['tax_number'] == '1234567890'
    assert imported.json['fields']['phone_number'] == ('Central phone' if existing else 'Legacy phone')


@pytest.mark.parametrize('value', [None, 'invalid client id', SAFETY_CLIENT])
def test_completed_central_profile_remains_editable_without_integration(configured, value):
    with configured.app_context():
        db.session.add(AccountProfile(subject=configured.config['TEST_OWNER'],
            fields={'full_name': 'Central name'}, legacy_imported=True, revision='d' * 32))
        db.session.commit()
    configured.config['OIDC_CLIENT_ID'] = value
    browser = authenticated_browser(configured)
    assert b'name="full_name"' in browser.get('/profile', base_url=ISSUER).data
    saved = browser.post('/profile', base_url=ISSUER, data={
        'csrf_token': 'browser-csrf-token', 'profile_version': 'd' * 32, 'full_name': 'Changed centrally',
    })
    assert saved.status_code == 302
    with configured.app_context():
        assert db.session.get(AccountProfile, configured.config['TEST_OWNER']).fields['full_name'] == 'Changed centrally'


@pytest.mark.parametrize('reason', ['unregistered', 'inactive'])
def test_unavailable_registered_source_blocks_blank_edit_and_recovers_without_data_loss(configured, reason):
    if reason == 'unregistered':
        configured.config['OIDC_CLIENT_ID'] = 'not-registered-hr-client'
    else:
        with configured.app_context():
            db.session.get(Client, HR_CLIENT).active = False
            db.session.commit()
    browser = authenticated_browser(configured)
    page = browser.get('/profile', base_url=ISSUER)
    assert page.status_code == 200
    assert b'name="full_name"' not in page.data
    assert ('ACCOUNTS_INTEGRATION_CSUPOR_' + reason.upper()).encode() in page.data
    result = browser.post('/profile', base_url=ISSUER, data={
        'csrf_token': 'browser-csrf-token', 'profile_version': '', 'full_name': '',
    })
    assert result.status_code == 409
    with configured.app_context():
        assert db.session.get(AccountProfile, configured.config['TEST_OWNER']) is None
        db.session.get(Client, HR_CLIENT).active = True
        db.session.commit()
    configured.config['OIDC_CLIENT_ID'] = HR_CLIENT
    imported = configured.test_client().post('/api/csupor/profile/import', base_url=ISSUER,
        headers=bearer(HR_TOKEN), json={'fields': {'full_name': 'Preserved legacy name',
                                                'tax_number': '1234567890'}})
    assert imported.status_code == 200
    assert imported.json['fields'] == {'full_name': 'Preserved legacy name', 'tax_number': '1234567890'}
