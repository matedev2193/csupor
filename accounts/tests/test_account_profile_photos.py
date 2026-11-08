"""Shared-account photo ownership, legacy migration and non-destructive edits."""
import base64
from hashlib import sha256
from io import BytesIO
import time
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image
import pytest

from accounts import create_app
from accounts.models import Account, AccessToken, Client, Membership, db
from accounts.photo_models import AccountPhoto, AccountPhotoSource


ISSUER = 'https://accounts.example.invalid'
CSRF = 'private-photo-test-csrf'


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
        'ACCOUNTS_SECRET_KEY': 'independent-private-photo-test-secret',
        'ACCOUNTS_ISSUER': ISSUER, 'ACCOUNTS_SIGNING_KEY_FILE': str(key),
    })
    app.config.update(PHOTO_OWNER=str(uuid4()), PHOTO_OTHER=str(uuid4()))
    with app.app_context():
        db.create_all()
        for label in ('OWNER', 'OTHER'):
            db.session.add(Account(subject=app.config['PHOTO_' + label], username=label,
                                   email=label.lower() + '@example.invalid', password_hash='unused', version=1))
        for client_id in ('csupor', 'munkavedelem'):
            db.session.add(Client(client_id=client_id, name=client_id, secret_hash='unused',
                                  redirect_uris=['https://' + client_id + '.example.invalid/auth/callback']))
        db.session.flush()
        for label, client_id in (('OWNER', 'csupor'), ('OTHER', 'csupor'), ('OWNER', 'munkavedelem')):
            subject = app.config['PHOTO_' + label]
            raw = label + '-' + client_id
            db.session.add(Membership(account_subject=subject, client_id=client_id))
            db.session.add(AccessToken(token_hash=sha256(raw.encode()).hexdigest(), account_subject=subject,
                                       client_id=client_id, scope='openid profile', version=1,
                                       expires_at=int(time.time()) + 300))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def browser(app, label='OWNER'):
    client = app.test_client()
    with client.session_transaction(base_url=ISSUER) as session:
        session.update(account_subject=app.config['PHOTO_' + label], account_version=1,
                       account_auth_time=int(time.time()), csrf_token=CSRF)
    return client


def jpeg(size=(96, 64), colour='red'):
    out = BytesIO()
    Image.new('RGB', size, colour).save(out, 'JPEG')
    return out.getvalue()


def upload(client, **data):
    values = {'csrf_token': CSRF, 'photo_version': '', 'photo': (BytesIO(jpeg()), 'picture.jpg')}
    values.update(data)
    return client.post('/profile/photo', base_url=ISSUER, data=values,
                       headers={'Accept': 'application/json'})


def imported_payload():
    return {
        'data': base64.b64encode(jpeg((384, 384))).decode(), 'width': 384, 'height': 384,
        'source_data': base64.b64encode(jpeg((1200, 800), 'blue')).decode(),
        'source_width': 1200, 'source_height': 800,
        'center_x': 0.6, 'center_y': 0.5, 'zoom': 2.0, 'version': 'a' * 32,
    }


def import_photo(app, payload=None, token='OWNER-csupor'):
    return app.test_client().post('/api/csupor/profile/photo/import', base_url=ISSUER,
                                 json=imported_payload() if payload is None else payload,
                                 headers={'Authorization': 'Bearer ' + token})


def test_photo_upload_and_source_require_live_owner_session_and_csrf(app):
    anonymous = app.test_client()
    assert anonymous.get('/profile/photo/source', base_url=ISSUER).status_code == 302
    assert upload(anonymous).status_code == 302
    owner = browser(app)
    assert upload(owner, csrf_token='bad').status_code == 400
    assert upload(owner).status_code == 200
    with app.app_context():
        assert db.session.get(AccountPhoto, app.config['PHOTO_OWNER']) is not None
        assert db.session.get(AccountPhoto, app.config['PHOTO_OTHER']) is None
        account = db.session.get(Account, app.config['PHOTO_OWNER'])
        account.version += 1
        db.session.commit()
    assert owner.get('/profile/photo/image', base_url=ISSUER).status_code == 302


def test_crop_editor_keeps_original_source_and_rejects_stale_changes(app):
    owner = browser(app)
    saved = upload(owner, photo=(BytesIO(jpeg((800, 600))), 'picture.jpg'),
                   crop_center_x='0.5', crop_center_y='0.5', crop_zoom='2').get_json()
    with app.app_context():
        before = bytes(db.session.get(AccountPhotoSource, app.config['PHOTO_OWNER']).data)
    metadata = owner.get('/profile/photo/editor', base_url=ISSUER).get_json()
    assert (metadata['width'], metadata['height'], metadata['zoom']) == (800, 600, 2)
    edited = owner.post('/profile/photo', base_url=ISSUER, headers={'Accept': 'application/json'}, data={
        'csrf_token': CSRF, 'use_existing': '1', 'photo_version': saved['version'],
        'crop_center_x': '0.6', 'crop_center_y': '0.5', 'crop_zoom': '3',
    })
    assert edited.status_code == 200
    assert edited.get_json()['version'] != saved['version']
    assert 'account_subject=' not in edited.get_json()['photo_url']
    with app.app_context():
        source = db.session.get(AccountPhotoSource, app.config['PHOTO_OWNER'])
        assert bytes(source.data) == before
        assert (source.center_x, source.center_y, source.zoom) == (0.6, 0.5, 3)
    assert upload(owner, photo_version=saved['version']).status_code == 409
    assert owner.get('/profile/photo/source?v=' + saved['version'], base_url=ISSUER).status_code == 409


def test_source_and_image_ignore_other_subjects_and_are_private(app):
    owner, other = browser(app), browser(app, 'OTHER')
    assert upload(owner).status_code == 200
    assert upload(other, photo=(BytesIO(jpeg(colour='blue')), 'picture.jpg')).status_code == 200
    for path in ('/profile/photo/source', '/profile/photo/image'):
        own = owner.get(path, base_url=ISSUER)
        spoofed = owner.get(path + '?account_subject=' + app.config['PHOTO_OTHER'], base_url=ISSUER)
        assert spoofed.data == own.data
        assert spoofed.data != other.get(path, base_url=ISSUER).data
        assert 'no-store' in spoofed.headers['Cache-Control']
        assert spoofed.headers['X-Content-Type-Options'] == 'nosniff'


@pytest.mark.parametrize('changes', [
    {'photo': (b'not-an-image', 'picture.jpg')},
    {'photo': (jpeg(), 'picture.svg')},
    {'crop_center_x': 'nan', 'crop_center_y': '0.5', 'crop_zoom': '2'},
    {'crop_center_x': '0', 'crop_center_y': '0', 'crop_zoom': '1'},
    {'crop_center_x': '0.5'},
])
def test_invalid_uploads_do_not_create_photo(app, changes):
    if 'photo' in changes:
        data, filename = changes['photo']
        changes = dict(changes, photo=(BytesIO(data), filename))
    assert upload(browser(app), **changes).status_code == 400
    with app.app_context():
        assert db.session.get(AccountPhoto, app.config['PHOTO_OWNER']) is None


def test_import_preserves_crop_and_is_idempotent_after_owner_edits(app):
    result = import_photo(app)
    assert result.status_code == 200
    assert result.get_json() == {'sub': app.config['PHOTO_OWNER'], 'version': 'a' * 32}
    owner = browser(app)
    metadata = owner.get('/profile/photo/editor', base_url=ISSUER).get_json()
    assert (metadata['width'], metadata['height'], metadata['center_x'], metadata['zoom']) == (1200, 800, 0.6, 2)
    result = upload(owner, photo_version='a' * 32, photo=(BytesIO(jpeg(colour='green')), 'new.jpg'))
    assert result.status_code == 200
    new_version = result.get_json()['version']
    assert import_photo(app).get_json()['version'] == new_version
    exported = app.test_client().get('/api/csupor/profile/photo', base_url=ISSUER,
                                    headers={'Authorization': 'Bearer OWNER-csupor'}).get_json()
    assert exported['version'] == new_version
    assert set(exported) == {'sub', 'version', 'data', 'width', 'height'}
    assert base64.b64decode(exported['data']) == owner.get('/profile/photo/image', base_url=ISSUER).data


def test_photo_import_does_not_accept_safety_bearer_or_browser_cookie(app):
    for path in ('/api/csupor/profile/photo', '/api/csupor/profile/photo/import'):
        method = 'post' if path.endswith('/import') else 'get'
        kwargs = {'base_url': ISSUER}
        if method == 'post':
            kwargs['json'] = imported_payload()
        assert getattr(browser(app), method)(path, **kwargs).status_code == 401
        assert getattr(app.test_client(), method)(path, **kwargs,
                   headers={'Authorization': 'Bearer OWNER-munkavedelem'}).status_code == 403
        assert getattr(app.test_client(), method)(path + '?access_token=OWNER-csupor', **kwargs).status_code == 401
    payload = dict(imported_payload(), account_subject=app.config['PHOTO_OTHER'])
    assert import_photo(app, payload).get_json()['sub'] == app.config['PHOTO_OWNER']
    with app.app_context():
        assert db.session.get(AccountPhoto, app.config['PHOTO_OTHER']) is None


@pytest.mark.parametrize('changes', [
    {'data': '!!invalid!!'}, {'width': True}, {'width': 100}, {'version': '../../x'},
    {'source_data': 'eA=='}, {'source_width': 9999}, {'center_x': float('inf')}, {'zoom': 0},
])
def test_malformed_legacy_photo_is_not_imported(app, changes):
    assert import_photo(app, dict(imported_payload(), **changes)).status_code == 400
    with app.app_context():
        assert db.session.get(AccountPhoto, app.config['PHOTO_OWNER']) is None
        assert db.session.get(AccountPhotoSource, app.config['PHOTO_OWNER']) is None


def test_photo_import_rechecks_revocation_after_decode(app, monkeypatch):
    from accounts import profile_photos
    original = profile_photos._import_image
    revoked = False

    def revoke_after_decode(*args, **kwargs):
        nonlocal revoked
        result = original(*args, **kwargs)
        if not revoked:
            db.session.execute(db.update(Account).where(Account.subject == app.config['PHOTO_OWNER'])
                               .values(version=2))
            db.session.commit()
            revoked = True
        return result

    monkeypatch.setattr(profile_photos, '_import_image', revoke_after_decode)
    assert import_photo(app).status_code in (401, 404)
    with app.app_context():
        assert db.session.get(AccountPhoto, app.config['PHOTO_OWNER']) is None
