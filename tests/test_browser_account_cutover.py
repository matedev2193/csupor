"""Real HTTP: browser export/import, old-password CSUPOR login and second app."""
from urllib.parse import parse_qs, urlsplit

from accounts.admin import AccountAdmin, prepare_setup_state
from accounts.models import Account, Client, Membership, db as account_db
from app import db
from app.models import User, UserPrivilege, UserProfile
from app.shared_auth_models import ExternalIdentity
from tests import test_shared_accounts_e2e as e2e


def _form(response):
    assert response.status_code == 200, response.text[:300]
    values = e2e._HiddenInputs(response.text).values
    assert 'csrf_token' in values
    return values


def test_browser_cutover_preserves_existing_users_and_grants_second_app():
    # Reuse the established real HTTP fixture, then start from its local user
    # database and an empty, separately hosted central account database.
    case = e2e.SharedAccountsEndToEndTests('test_existing_password_and_identity_work_in_two_separate_systems')
    try:
        case.setUp()
        case.app.config.update(AUTH_MODE='local', ACCOUNTS_MIGRATION_MODE=True, AUTH_ALLOW_HTTP=True)
        setup_token = 'one-time-browser-setup-' + 's' * 40
        transfer_key = 'browser-transfer-key-' + 't' * 40
        owner_password = 'Existing-owner-password-123'
        csupor_secret = 'csupor-client-secret-' + 'a' * 32
        safety_secret = 'safety-client-secret-' + 'b' * 32
        with case.app.app_context():
            owner = User(username='existing-owner', email='owner@example.invalid', privilege=UserPrivilege.developer)
            owner.set_password(owner_password)
            owner.profile = UserProfile(full_name='Existing owner HR record', bank_account_number='private-HR-bank')
            db.session.add(owner)
            db.session.commit()
            owner_id = owner.id
            before = {u.id: (u.password_hash, u.privilege, u.profile.full_name) for u in User.query.all()}
        case.provider.config['ACCOUNTS_SETUP_TOKEN'] = setup_token
        with case.provider.app_context():
            account_db.drop_all()
            account_db.create_all()
            prepare_setup_state()

        login = _form(case.request('GET', case.rp_url + '/login'))
        login.update(login='existing-owner', password=owner_password)
        response = case.request('POST', case.rp_url + '/login', data=login)
        assert urlsplit(response.url).path == '/developer/shared-accounts'
        export = _form(response)
        export.update(current_password=owner_password, issuer=case.issuer,
                      transfer_key=transfer_key, maintenance_confirm='1')
        response = case.request('POST', case.rp_url + '/developer/shared-accounts', data=export)
        assert response.status_code == 200
        assert response.content.startswith(b'MICIMACKO-ACCOUNTS-TRANSFER-1\n')
        assert b'private-HR-bank' not in response.content
        encrypted = response.content
        assert case.request('POST', case.rp_url + '/register', data={}).status_code == 503

        setup = _form(case.request('GET', case.issuer + '/setup'))
        setup.update(setup_token=setup_token, transfer_key=transfer_key,
                     admin_login='existing-owner', admin_password=owner_password,
                     csupor_redirect_uri=case.callback, csupor_client_secret=csupor_secret)
        response = case.request('POST', case.issuer + '/setup', data=setup,
                                files={'transfer_file': ('migration.enc', encrypted, 'application/octet-stream')},
                                allow_redirects=False)
        assert response.status_code == 302, response.text[:400]
        assert response.headers['Location'] == '/admin'
        assert case.request('GET', case.issuer + '/setup').status_code == 404
        with case.app.app_context():
            identities = {link.user_id: link.subject for link in ExternalIdentity.query.all()}
            assert len(identities) == 2
            assert {u.id: (u.password_hash, u.privilege, u.profile.full_name) for u in User.query.all()} == before
            assert db.session.get(User, owner_id).profile.bank_account_number == 'private-HR-bank'
        with case.provider.app_context():
            assert Account.query.count() == 2
            assert AccountAdmin.query.count() == 1
            assert account_db.session.get(AccountAdmin, identities[owner_id]) is not None
            assert account_db.session.get(Account, case.subject).check_password(case.password)
            assert Membership.query.count() == 2

        client = _form(case.request('GET', case.issuer + '/admin/clients/new'))
        client.update(client_id='munkavedelem', name='Tűz- és munkavédelem',
                      redirect_uris=case.other_callback, client_secret=safety_secret, active='true')
        response = case.request('POST', case.issuer + '/admin/clients/new', data=client)
        assert response.status_code == 200
        assert 'SAFETY_OIDC_ISSUER' in response.text
        assert safety_secret not in response.text
        access = _form(case.request('GET', case.issuer + '/admin/accounts'))
        access['client_ids'] = ['csupor', 'munkavedelem']
        response = case.request('POST', case.issuer + '/admin/accounts/' + case.subject + '/access', data=access)
        assert response.status_code == 200

        case.app.config.update(AUTH_MODE='oidc', ACCOUNTS_MIGRATION_MODE=False)
        case.browser.cookies.clear()
        case.sign_in()  # A real signed OIDC round trip with the old worker password.
        with case.app.app_context():
            worker = db.session.get(User, case.user_id)
            assert worker.privilege == UserPrivilege.hr
            assert worker.profile.full_name == 'Meglévő Munkatárs'
            assert User.query.count() == 2
        response, verifier, _ = case.other_authorization(client_id='munkavedelem')
        assert response.status_code == 302
        assert response.headers['Location'].startswith(case.other_callback)
        code = parse_qs(urlsplit(response.headers['Location']).query)['code'][0]
        token = case.request('POST', case.issuer + '/token', auth=('munkavedelem', safety_secret),
                             data={'grant_type': 'authorization_code', 'code': code,
                                   'redirect_uri': case.other_callback, 'code_verifier': verifier})
        assert token.status_code == 200, token.text[:300]
        info = case.request('GET', case.issuer + '/userinfo',
                            headers={'Authorization': 'Bearer ' + token.json()['access_token']})
        assert info.status_code == 200
        assert info.json()['sub'] == case.subject
        assert 'password_hash' not in info.json()
    finally:
        case.doCleanups()
