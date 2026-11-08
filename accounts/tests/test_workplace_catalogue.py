"""Only the authorised apps may publish/read the limited workplace directory."""
from base64 import b64encode
from hashlib import sha256
import json
import time

import pytest

from accounts.models import AccessToken, Client, Membership, db
from accounts.workplaces import WorkplaceCatalogue
from accounts.tests.test_end_session import app, pem, bearer, TOKENS, ISSUER


ROWS = [{'id': 17, 'name': 'Óvoda – Első utca 1.', 'address': 'Első utca 1.'}]
SAFETY_TOKEN = 'workplace-reader-token'


@pytest.fixture
def configured(app):
    with app.app_context():
        db.session.get(Client, 'csupor').set_secret('existing-secret')
        safety = Client(client_id='munkavedelem', name='Tűz- és munkavédelem',
                        redirect_uris=['https://safety.example.invalid/auth/callback'])
        safety.set_secret('safety-secret')
        db.session.add(safety)
        db.session.flush()
        db.session.add(Membership(account_subject=app.config['TEST_OWNER'], client_id='munkavedelem'))
        db.session.add(AccessToken(token_hash=sha256(SAFETY_TOKEN.encode()).hexdigest(),
            account_subject=app.config['TEST_OWNER'], client_id='munkavedelem', version=1,
            scope='openid profile', expires_at=int(time.time()) + 300))
        db.session.commit()
    return app


def basic(client='csupor', secret='existing-secret'):
    return {'Authorization': 'Basic ' + b64encode(f'{client}:{secret}'.encode()).decode()}


def publish(app, generation=1, rows=None, headers=None):
    return app.test_client().post('/api/csupor/workplaces', base_url=ISSUER,
        headers=basic() if headers is None else headers,
        json={'generation': generation, 'workplaces': ROWS if rows is None else rows})


def read(app, token=SAFETY_TOKEN):
    return app.test_client().get('/api/workplaces', base_url=ISSUER, headers=bearer(token))


def test_published_catalogue_is_minimal_and_readable_only_by_safety(configured):
    assert read(configured).status_code == 503
    response = publish(configured)
    assert response.status_code == 200
    assert set(response.json) == {'source', 'revision', 'published_at', 'workplaces'}
    assert response.json['source'] == 'csupor'
    assert len(response.json['revision']) == 64
    assert response.json['published_at'].endswith('+00:00')
    assert response.json['workplaces'] == ROWS
    assert read(configured).json == response.json
    assert read(configured, TOKENS['csupor']).status_code == 403
    assert read(configured, TOKENS['safety']).status_code == 403
    assert read(configured, 'invalid-token').status_code == 401


@pytest.mark.parametrize('header', [{}, bearer(TOKENS['csupor']), basic(secret='wrong'),
                                   basic(client='munkavedelem', secret='safety-secret')])
def test_publication_requires_exact_csupor_service_credentials(configured, header):
    assert publish(configured, headers=header).status_code == 401
    assert read(configured).status_code == 503


def test_inactive_csupor_cannot_publish(configured):
    with configured.app_context():
        db.session.get(Client, 'csupor').active = False
        db.session.commit()
    assert publish(configured).status_code == 401


@pytest.mark.parametrize('invalid', [
    [{'id': 18, 'name': 'New', 'address': 'Address', 'tax_number': 'private'}],
    ROWS + [{'id': 18, 'name': 'Other', 'address': 'Bad\naddress'}],
    ROWS + [dict(ROWS[0], name='Duplicate ID')],
    ROWS + [dict(ROWS[0], id=18, name=ROWS[0]['name'].upper())],
    [{'id': True, 'name': 'Name', 'address': 'Address'}],
    [{'id': 18, 'name': 'x' * 121, 'address': 'Address'}],
    [{'id': 18, 'name': 'Name', 'address': 'x' * 256}],
    [dict(ROWS[0], id=i + 1, name=str(i)) for i in range(1001)],
])
def test_invalid_whole_snapshot_never_partially_replaces_catalogue(configured, invalid):
    original = publish(configured).json
    assert publish(configured, generation=2, rows=invalid).status_code == 400
    assert read(configured).json == original


@pytest.mark.parametrize('generation', [True, 0, -1, 1.5, '2', 9223372036854775808])
def test_invalid_generation_is_rejected(configured, generation):
    assert publish(configured, generation=generation).status_code == 400


def test_generation_order_and_idempotency_preserve_latest_snapshot(configured):
    original = publish(configured, generation=8).json
    assert publish(configured, generation=8).json == original
    modified = [dict(ROWS[0], address='Updated address')]
    assert publish(configured, generation=7, rows=modified).status_code == 409
    assert publish(configured, generation=8, rows=modified).status_code == 409
    assert read(configured).json == original
    latest = publish(configured, generation=9, rows=modified)
    assert latest.status_code == 200
    assert latest.json['revision'] != original['revision']
    assert publish(configured, generation=8).status_code == 409
    assert read(configured).json == latest.json
    assert publish(configured, generation=10, rows=[]).json['workplaces'] == []


@pytest.mark.parametrize('reason', ['profile-scope', 'openid-scope', 'inactive', 'membership', 'expired'])
def test_safety_requires_live_membership_and_both_scopes(configured, reason):
    publish(configured)
    with configured.app_context():
        token = db.session.get(AccessToken, sha256(SAFETY_TOKEN.encode()).hexdigest())
        if reason == 'profile-scope':
            token.scope = 'openid'
        elif reason == 'openid-scope':
            token.scope = 'profile'
        elif reason == 'inactive':
            db.session.get(Client, 'munkavedelem').active = False
        elif reason == 'membership':
            db.session.delete(db.session.get(Membership, (configured.config['TEST_OWNER'], 'munkavedelem')))
        else:
            token.expires_at = int(time.time()) - 1
        db.session.commit()
    assert read(configured).status_code in {401, 403}


def test_body_limit_allows_valid_unicode_directory_and_blocks_larger_requests(configured):
    rows = [{'id': i + 1, 'name': f'{i} – ' + '🌳' * 110, 'address': '🌳' * 255} for i in range(1000)]
    response = configured.test_client().post('/api/csupor/workplaces', base_url=ISSUER, headers=basic(),
        data=json.dumps({'generation': 1, 'workplaces': rows}, ensure_ascii=False).encode(),
        content_type='application/json')
    assert response.status_code == 200
    assert response.json['workplaces'] == rows
    assert len(response.data) < 2 * 1024 * 1024
    result = configured.test_client().post('/api/csupor/workplaces', base_url=ISSUER, headers=basic(),
        data=b' ' * (2 * 1024 * 1024 + 1), content_type='application/json')
    assert result.status_code == 413


def test_factory_creates_catalogue_without_manual_migration(tmp_path):
    from accounts import create_app
    fresh = create_app({'TESTING': True, 'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'fresh.db'),
                       'ACCOUNTS_SECRET_KEY': 'fresh-workplaces-secret-at-least-32-characters',
                       'ACCOUNTS_ISSUER': ISSUER})
    with fresh.app_context():
        assert db.session.execute(db.select(WorkplaceCatalogue)).scalars().all() == []
        db.session.remove()
        db.engine.dispose()
