"""Central profile editing, one-time migration and application boundaries."""
from base64 import b64encode
from hashlib import sha256
import time

import pytest

from accounts.admin import AccountAdmin
from accounts.models import Account, AccessToken, Client, Membership, db
from accounts.profile import FIELDS
from accounts.profile_models import AccountProfile
from accounts.tests.test_end_session import app, pem, authenticated_browser, bearer, TOKENS, ISSUER


def fields(**updates):
    return {**dict.fromkeys(FIELDS), **updates}


def imported(app, **updates):
    response = app.test_client().post('/api/csupor/profile/import', base_url=ISSUER,
                                     headers=bearer(TOKENS['csupor']), json={'fields': fields(**updates)})
    assert response.status_code == 200
    return response.json


def saved_fields(app, subject=None):
    with app.app_context():
        record = db.session.get(AccountProfile, subject or app.config['TEST_OWNER'])
        return dict(record.fields) if record else None


def service_header(app, secret='existing-csupor-client-secret-123456'):
    with app.app_context():
        db.session.get(Client, 'csupor').set_secret(secret)
        db.session.commit()
    return {'Authorization': 'Basic ' + b64encode(('csupor:' + secret).encode()).decode()}


def test_factory_registers_new_profile_tables_before_bootstrap(tmp_path):
    from accounts import create_app
    instance = create_app({'TESTING': True, 'ACCOUNTS_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'fresh.db'),
                           'ACCOUNTS_SECRET_KEY': 'fresh-profile-secret-at-least-32-characters',
                           'ACCOUNTS_ISSUER': ISSUER})
    with instance.app_context():
        assert db.session.execute(db.select(AccountProfile)).scalars().all() == []
        from accounts.photo_models import AccountPhoto
        assert db.session.execute(db.select(AccountPhoto)).scalars().all() == []
        db.session.remove()
        db.engine.dispose()


def test_profile_data_never_enters_generic_oidc_claims(app):
    imported(app, full_name='Meglévő dolgozó', social_security_number='123456789',
             bank_account_number='12345678-12345678', disability='Személyes adat')
    browser = app.test_client()
    for name in ('csupor', 'safety'):
        response = browser.get('/userinfo', base_url=ISSUER, headers=bearer(TOKENS[name]))
        assert response.status_code == 200
        assert not set(FIELDS).intersection(response.json)
        assert '123456789' not in response.get_data(as_text=True)


def test_only_subject_bound_csupor_bearer_can_read_or_import(app):
    browser = authenticated_browser(app)
    for path, method in (('/api/csupor/profile', browser.get), ('/api/csupor/profile/import', browser.post)):
        assert method(path, base_url=ISSUER).status_code == 401
        assert method(path + '?access_token=' + TOKENS['csupor'], base_url=ISSUER).status_code == 401
        assert method(path, base_url=ISSUER, headers=bearer(TOKENS['safety'])).status_code == 403
    owner = imported(app, full_name='Tulajdonos')
    response = browser.get('/api/csupor/profile?subject=' + app.config['TEST_OTHER'],
                           base_url=ISSUER, headers=bearer(TOKENS['csupor']))
    assert response.json['sub'] == app.config['TEST_OWNER']
    assert response.json['fields']['full_name'] == 'Tulajdonos'
    assert saved_fields(app, app.config['TEST_OTHER']) is None
    assert browser.post('/api/csupor/profile/import', base_url=ISSUER, headers=bearer(TOKENS['csupor']),
                        json={'fields': fields(), 'subject': app.config['TEST_OTHER']}).status_code == 400
    assert owner['legacy_imported']


def test_missing_profile_scope_and_revoked_membership_are_rejected(app):
    browser = app.test_client()
    with app.app_context():
        token = db.session.get(AccessToken, sha256(TOKENS['csupor'].encode()).hexdigest())
        token.scope = 'openid email'
        db.session.commit()
    assert browser.get('/api/csupor/profile', base_url=ISSUER, headers=bearer(TOKENS['csupor'])).status_code == 403
    with app.app_context():
        db.session.delete(db.session.get(Membership, (app.config['TEST_OWNER'], 'csupor')))
        db.session.commit()
    assert browser.get('/api/csupor/profile', base_url=ISSUER, headers=bearer(TOKENS['csupor'])).status_code == 401


def test_import_is_once_and_preserves_existing_central_values_and_clears(app):
    with app.app_context():
        db.session.add(AccountProfile(subject=app.config['TEST_OWNER'],
                                      fields={'full_name': 'Már javított név', 'phone_number': None},
                                      revision='a' * 32, legacy_imported=False))
        db.session.commit()
    first = imported(app, full_name='Régi név', phone_number='12345', permanent_residence='Régi cím')
    assert first['fields']['full_name'] == 'Már javított név'
    assert first['fields']['phone_number'] is None
    assert first['fields']['permanent_residence'] == 'Régi cím'
    second = imported(app, full_name='Újabb régi név', permanent_residence='Ne írja felül')
    assert second == first


def test_browser_cannot_replace_legacy_data_with_empty_form_before_import(app):
    browser = authenticated_browser(app)
    response = browser.get('/profile', base_url=ISSUER)
    assert response.status_code == 200
    assert 'Meglévő adataim betöltése'.encode() in response.data
    assert b'https://csupor.example.invalid/profile' in response.data
    assert b'name="full_name"' not in response.data
    assert browser.post('/profile', base_url=ISSUER,
                        data={'csrf_token': 'browser-csrf-token', 'profile_version': '', 'full_name': ''}).status_code == 409
    assert saved_fields(app) is None


def test_all_profile_fields_are_editable_and_cleared_values_stay_cleared(app):
    initial = imported(app, full_name='Régi név', phone_number='12345')
    browser = authenticated_browser(app)
    response = browser.get('/profile', base_url=ISSUER)
    assert response.status_code == 200
    for name in FIELDS:
        assert ('name="' + name + '"').encode() in response.data
    assert b'profile_photo_editor.js' in response.data
    assert b"form-action 'self'" in response.headers['Content-Security-Policy'].encode()
    form = {name: '' for name in FIELDS}
    form.update(csrf_token='browser-csrf-token', profile_version=initial['version'], full_name='Mentett név',
                date_of_birth='1995-01-02', gender='male', marital_status='civil partnership',
                social_security_number='123456789', tax_number='1234567890', education_number='12345678901')
    assert browser.post('/profile', base_url=ISSUER, data=form).status_code == 302
    saved = saved_fields(app)
    assert saved['full_name'] == 'Mentett név'
    assert saved['phone_number'] is None
    assert saved['date_of_birth'] == '1995-01-02'
    assert imported(app, phone_number='Régi szám')['fields']['phone_number'] is None


@pytest.mark.parametrize('field,value', [
    ('date_of_birth', '2026-02-30'), ('date_of_birth', '20260101'),
    ('gender', 'invalid'), ('marital_status', 'invalid'),
    ('full_name', 'x' * 121), ('phone_number', 'line\x00break'),
    ('social_security_number', '１２３４５６７８９'), ('tax_number', '123'),
])
def test_invalid_profile_update_is_atomic(app, field, value):
    initial = imported(app, full_name='Megmaradó név')
    browser = authenticated_browser(app)
    form = {name: '' for name in FIELDS}
    form.update(csrf_token='browser-csrf-token', profile_version=initial['version'], full_name='Ezt se mentse')
    form[field] = value
    assert browser.post('/profile', base_url=ISSUER, data=form).status_code == 400
    assert saved_fields(app)['full_name'] == 'Megmaradó név'


def test_csrf_stale_revision_and_submitted_other_owner_never_overwrite(app):
    initial = imported(app, full_name='Eredeti név')
    browser = authenticated_browser(app)
    form = {name: '' for name in FIELDS}
    form.update(profile_version=initial['version'], full_name='Mentett név')
    assert browser.post('/profile', base_url=ISSUER, data=form).status_code == 400
    form['csrf_token'] = 'browser-csrf-token'
    form['subject'] = app.config['TEST_OTHER']
    assert browser.post('/profile', base_url=ISSUER, data=form).status_code == 302
    assert saved_fields(app, app.config['TEST_OTHER']) is None
    form['full_name'] = 'Elavult ablak'
    assert browser.post('/profile', base_url=ISSUER, data=form).status_code == 409
    assert saved_fields(app)['full_name'] == 'Mentett név'


def test_central_admin_edit_requires_explicit_account_admin_authority(app):
    imported(app, full_name='Dolgozó neve')
    browser = authenticated_browser(app, app.config['TEST_OTHER'])
    path = '/admin/accounts/' + app.config['TEST_OWNER'] + '/profile'
    assert browser.get(path, base_url=ISSUER).status_code == 403
    with app.app_context():
        db.session.add(AccountAdmin(account_subject=app.config['TEST_OTHER']))
        db.session.commit()
    response = browser.get(path, base_url=ISSUER)
    assert response.status_code == 200
    assert b'name="full_name"' in response.data
    assert b'data-photo-editor-form' not in response.data


def test_batch_reader_only_returns_existing_imported_csupor_members(app):
    imported(app, full_name='Munkaügyi adat')
    browser = authenticated_browser(app)
    path = '/api/csupor/profiles/read'
    payload = {'subjects': [app.config['TEST_OWNER'], app.config['TEST_OTHER']]}
    assert browser.post(path, base_url=ISSUER, json=payload).status_code == 401
    assert browser.post(path, base_url=ISSUER, headers=bearer(TOKENS['csupor']), json=payload).status_code == 401
    headers = service_header(app)
    response = browser.post(path, base_url=ISSUER, headers=headers, json=payload)
    assert response.status_code == 200
    assert [person['sub'] for person in response.json['profiles']] == [app.config['TEST_OWNER']]
    with app.app_context():
        db.session.get(Account, app.config['TEST_OWNER']).active = False
        db.session.commit()
    assert browser.post(path, base_url=ISSUER, headers=headers, json=payload).json == {'profiles': []}
    assert browser.post(path, base_url=ISSUER, headers=headers,
                        json={'subjects': ['x'] * 101}).status_code == 400
    assert browser.post(path, base_url=ISSUER, headers=headers, json={'subjects': []}).status_code == 400


def test_profile_save_rechecks_account_revocation_after_initial_authentication(app, monkeypatch):
    initial = imported(app, full_name='Megmaradó név')
    browser = authenticated_browser(app)
    import accounts.profile as profile_module
    original = profile_module.validate_fields

    def revoke_after_validation(values, **kwargs):
        validated = original(values, **kwargs)
        db.session.execute(db.update(Account).where(Account.subject == app.config['TEST_OWNER'])
                           .values(version=Account.version + 1))
        db.session.commit()
        return validated

    monkeypatch.setattr(profile_module, 'validate_fields', revoke_after_validation)
    form = {name: '' for name in FIELDS}
    form.update(csrf_token='browser-csrf-token', profile_version=initial['version'], full_name='Ne mentse')
    assert browser.post('/profile', base_url=ISSUER, data=form).status_code == 401
    assert saved_fields(app)['full_name'] == 'Megmaradó név'


def test_import_rechecks_membership_revocation_after_bearer_validation(app, monkeypatch):
    import accounts.profile as profile_module
    original = profile_module.validate_fields

    def revoke_after_validation(values, **kwargs):
        validated = original(values, **kwargs)
        db.session.execute(db.delete(Membership).where(
            Membership.account_subject == app.config['TEST_OWNER'], Membership.client_id == 'csupor'))
        db.session.execute(db.delete(AccessToken).where(
            AccessToken.account_subject == app.config['TEST_OWNER'], AccessToken.client_id == 'csupor'))
        db.session.commit()
        return validated

    monkeypatch.setattr(profile_module, 'validate_fields', revoke_after_validation)
    response = app.test_client().post('/api/csupor/profile/import', base_url=ISSUER,
                                     headers=bearer(TOKENS['csupor']), json={'fields': fields(full_name='Ne mentse')})
    assert response.status_code in {401, 403}
    assert saved_fields(app) is None
