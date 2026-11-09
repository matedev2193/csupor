"""Expose the account's full name as the standard, scope-limited OIDC claim."""
from hashlib import sha256

import pytest

from accounts.models import AccessToken, db
from accounts.profile_models import AccountProfile
from accounts.tests.test_end_session import app, pem, bearer, TOKENS, ISSUER


def set_profile(app, fields):
    with app.app_context():
        profile = db.session.get(AccountProfile, app.config['TEST_OWNER'])
        if profile is None:
            profile = AccountProfile(subject=app.config['TEST_OWNER'], fields=fields)
            db.session.add(profile)
        else:
            profile.fields = fields
        db.session.commit()


def userinfo(app, token='safety'):
    return app.test_client().get('/userinfo', base_url=ISSUER, headers=bearer(TOKENS[token]))


def test_safety_gets_current_full_name_without_private_profile_fields(app):
    set_profile(app, {'full_name': '  Teszt Máté  ', 'tax_number': '1234567890',
                      'disability': 'Private medical detail', 'bank_account_number': 'Private bank data'})
    response = userinfo(app)
    assert response.status_code == 200
    assert response.json == {'sub': app.config['TEST_OWNER'], 'preferred_username': 'owner',
                             'name': 'Teszt Máté'}
    # No new login/token is needed after a central name edit or deliberate clear.
    set_profile(app, {'full_name': 'Módosított Teljes Név'})
    assert userinfo(app).json['name'] == 'Módosított Teljes Név'
    set_profile(app, {'full_name': None})
    assert 'name' not in userinfo(app).json


@pytest.mark.parametrize('fields', [None, {}, {'full_name': ''}, {'full_name': '   '},
                                   {'full_name': 123}, {'full_name': 'Név\nMásik'},
                                   {'full_name': 'Név\u202eMásik'}, {'full_name': 'x' * 121}])
def test_missing_or_invalid_name_leaves_existing_identity_claims_unchanged(app, fields):
    if fields is not None:
        set_profile(app, fields)
    response = userinfo(app)
    assert response.status_code == 200
    assert response.json == {'sub': app.config['TEST_OWNER'], 'preferred_username': 'owner'}


def test_name_is_limited_to_profile_scope_and_authenticated_subject(app):
    set_profile(app, {'full_name': 'Tulajdonos Teljes Neve'})
    with app.app_context():
        token = db.session.get(AccessToken, sha256(TOKENS['safety'].encode()).hexdigest())
        token.scope = 'openid email'
        db.session.commit()
    response = userinfo(app)
    assert response.status_code == 200
    assert response.json == {'sub': app.config['TEST_OWNER'], 'email': 'owner@example.invalid',
                             'email_verified': False}
    other = userinfo(app, 'other')
    assert other.status_code == 200
    assert other.json == {'sub': app.config['TEST_OTHER'], 'preferred_username': 'other'}
    discovery = app.test_client().get('/.well-known/openid-configuration', base_url=ISSUER)
    assert 'name' in discovery.json['claims_supported']
