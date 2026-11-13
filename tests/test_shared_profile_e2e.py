"""Real HTTP profile migration/edit/sync across independent account and HR DBs."""
from datetime import date, datetime
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
import pytest

from accounts.admin import AccountAdmin
from accounts.models import Account, Membership, db as accounts_db
from accounts.profile_models import AccountProfile
from app import db
from app.models import Gender, MaritalStatus, ProfilePhoto, ProfilePhotoSource, User, UserProfile
from app.shared_auth_models import ExternalIdentity
import test_shared_accounts_e2e as shared_fixture
from test_shared_accounts_e2e import _HiddenInputs


@pytest.fixture
def live():
    fixture = shared_fixture.SharedAccountsEndToEndTests(methodName='runTest')
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.doCleanups()


def _image(size=(48, 32)):
    stream = BytesIO()
    Image.new('RGB', size, 'green').save(stream, 'JPEG')
    return stream.getvalue()


def test_full_existing_profile_and_editable_photo_move_without_data_loss(live):
    source = _image()
    with live.app.app_context():
        user = db.session.get(User, live.user_id)
        user.profile.date_of_birth = date(1980, 2, 3)
        user.profile.gender = Gender.male
        user.profile.marital_status = MaritalStatus.married
        user.profile.tax_number = '1234567890'
        user.profile.bank_account_number = '12345678-12345678'
        user.profile.disability = 'Existing note'
        user.photo = ProfilePhoto(data=source, version='b'*32, width=48, height=32,
                                  size_bytes=len(source), updated_at=datetime.now())
        user.photo_source = ProfilePhotoSource(data=source, width=48, height=32,
                                               size_bytes=len(source), center_x=.5, center_y=.5, zoom=1)
        db.session.commit()
    live.sign_in()
    response = live.request('GET', live.rp_url + '/profile', allow_redirects=False)
    assert response.status_code == 302
    assert response.headers['Location'] == live.issuer + '/profile'
    with live.provider.app_context():
        record = accounts_db.session.get(AccountProfile, live.subject)
        assert record.legacy_imported
        assert record.fields['date_of_birth'] == '1980-02-03'
        assert record.fields['tax_number'] == '1234567890'
        assert record.fields['bank_account_number'] == '12345678-12345678'
        assert record.fields['marital_status'] == 'married'
        from accounts.profile_photos import ProfilePhotoSource as CentralPhotoSource
        central_source = accounts_db.session.get(CentralPhotoSource, live.subject)
        with Image.open(BytesIO(central_source.data)) as image:
            assert image.size == (48, 32)
            assert image.format == "JPEG"
        assert (central_source.center_x, central_source.center_y, central_source.zoom) == (.5, .5, 1)
        fields = dict(record.fields)
    editor = live.request('GET', live.issuer + '/profile')
    assert editor.status_code == 200
    form = _HiddenInputs(editor.text).values
    fields.update(full_name='Központban szerkesztett név', tax_number='', disability='')
    form.update({name: value or '' for name, value in fields.items()})
    saved = live.request('POST', live.issuer + '/profile', data=form, allow_redirects=False)
    assert saved.status_code == 302
    live.request('GET', live.rp_url + '/profile', allow_redirects=False)
    with live.app.app_context():
        user = db.session.get(User, live.user_id)
        assert user.profile.full_name == 'Központban szerkesztett név'
        assert user.profile.tax_number is None
        assert user.profile.disability is None
        assert user.profile.date_of_birth == date(1980, 2, 3)
        assert user.photo_source.data == source
    # The account service remains independent: another application's userinfo
    # receives no personal-profile fields through ordinary OIDC claims.
    with live.provider.app_context():
        from accounts.provider import user_info
        claims = dict(user_info(accounts_db.session.get(Account, live.subject), 'openid profile email'))
        assert 'tax_number' not in claims
        assert 'disability' not in claims
        assert 'date_of_birth' not in claims


def test_central_photo_change_reaches_csupor_avatar_cache(live):
    live.sign_in()
    page = live.request('GET', live.issuer + '/profile')
    form = _HiddenInputs(page.text).values
    payload = _image((90, 60))
    result = live.request('POST', live.issuer + '/profile/photo', data={
        'csrf_token': form['csrf_token'], 'photo_version': ''},
        files={'photo': ('portrait.jpg', payload, 'image/jpeg')}, allow_redirects=False)
    assert result.status_code == 302
    live.request('GET', live.rp_url + '/profile', allow_redirects=False)
    with live.app.app_context():
        photo = db.session.get(ProfilePhoto, live.user_id)
        assert photo is not None
        assert photo.mime_type == 'image/jpeg'
        assert (photo.width, photo.height) == (90, 60)
    served = live.request('GET', live.rp_url + f'/users/{live.user_id}/photo')
    assert served.status_code == 200
    assert served.headers['Content-Type'].startswith('image/jpeg')


def test_csupor_old_profile_writes_are_blocked_and_manager_record_is_readonly(live):
    live.sign_in()
    for path in ('/profile', '/profile/photo', f'/users/{live.user_id}/profile'):
        response = live.request('POST', live.rp_url + path, data={'full_name': 'Forbidden overwrite'}, allow_redirects=False)
        assert response.status_code == 403
    page = live.request('GET', live.rp_url + f'/users/{live.user_id}/profile')
    assert page.status_code == 200
    assert 'name="full_name"' not in page.text
    assert f'{live.issuer}/admin/accounts/{live.subject}/profile' in page.text
    response = live.request('GET', live.rp_url + '/profile/photo/editor', allow_redirects=False)
    assert response.headers['Location'] == live.issuer + '/profile/photo/editor'
    with live.app.app_context():
        assert db.session.get(User, live.user_id).profile.full_name == 'Meglévő Munkatárs'


def test_profile_outage_keeps_login_and_original_hr_data(live):
    live.sign_in()
    with patch('app.shared_profile._request_json', side_effect=TimeoutError('secret details')):
        response = live.request('GET', live.rp_url + '/profile', allow_redirects=False)
        assert response.status_code == 503
        assert 'secret details' not in response.text
        response = live.request('GET', live.rp_url + '/users/profiles', allow_redirects=False)
        assert response.status_code == 200
    with live.app.app_context():
        assert db.session.get(User, live.user_id).profile.full_name == 'Meglévő Munkatárs'


def test_central_admin_edit_refreshes_another_employee_before_hr_report(live):
    other_subject = str(uuid4())
    with live.app.app_context():
        other = User(username='colleague', email='colleague@example.invalid', password_hash='unused')
        other.profile = UserProfile(full_name='Old colleague', date_of_birth=date(1980, 1, 1))
        db.session.add(other)
        db.session.flush()
        other_id = other.id
        db.session.add(ExternalIdentity(issuer=live.issuer, subject=other_subject, user_id=other.id))
        db.session.commit()
    with live.provider.app_context():
        from accounts.profile import FIELDS
        fields = dict.fromkeys(FIELDS)
        fields.update(full_name='Old colleague', date_of_birth='1980-01-01')
        accounts_db.session.add(Account(subject=other_subject, username='colleague',
                                        email='colleague@example.invalid', password_hash='unused'))
        accounts_db.session.flush()
        accounts_db.session.add_all([Membership(account_subject=other_subject, client_id='csupor'),
                                     AccountAdmin(account_subject=live.subject),
                                     AccountProfile(subject=other_subject, fields=fields,
                                                    revision='a'*32, legacy_imported=True)])
        accounts_db.session.commit()
    live.sign_in()
    path = live.issuer + f'/admin/accounts/{other_subject}/profile'
    page = live.request('GET', path)
    assert page.status_code == 200
    form = _HiddenInputs(page.text).values
    fields.update(full_name='Updated colleague', date_of_birth='1990-05-03')
    form.update({key: value or '' for key, value in fields.items()})
    response = live.request('POST', path, data=form, allow_redirects=False)
    assert response.status_code == 302
    response = live.request('GET', live.rp_url + '/users/profiles?status=all')
    assert response.status_code == 200
    assert 'Updated colleague' in response.text
    with live.app.app_context():
        profile = db.session.get(User, other_id).profile
        assert profile.date_of_birth == date(1990, 5, 3)
        assert profile.full_name == 'Updated colleague'
