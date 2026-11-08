"""Central SMTP administration keeps credentials private and transport verified."""
from time import time
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from accounts.admin import AccountAdmin
from accounts.mail import send_reset_email
from accounts.mail_settings import AccountMailSettings, effective_settings, mail_settings
from accounts.models import Account, db
from accounts.tests.test_admin import app, pem, ISSUER


@pytest.fixture
def browser(app):
    if 'account_mail' not in app.blueprints:
        app.register_blueprint(mail_settings)
    with app.app_context():
        db.create_all()
        subject = str(uuid4())
        db.session.add(Account(subject=subject, username='mail-admin', email='admin@example.invalid',
                               password_hash='unused', active=True, version=1))
        db.session.add(AccountAdmin(account_subject=subject))
        db.session.commit()
    client = app.test_client()
    with client.session_transaction(base_url=ISSUER) as session:
        session.update(account_subject=subject, account_version=1, account_auth_time=int(time()), csrf_token='settings-test-csrf')
    return client


def form(**changes):
    values = dict(csrf_token='settings-test-csrf', revision='', action='save', host='smtp.example.invalid',
                  port='587', sender='accounts@example.invalid', username='accounts',
                  security='starttls', password='private-smtp-password')
    values.update(changes)
    return values


def post(browser, values):
    return browser.post('/admin/email-settings', base_url=ISSUER, data=values)


def revision(app):
    with app.app_context():
        return db.session.get(AccountMailSettings, 'recovery').revision


def test_settings_require_admin_and_csrf(app, browser):
    assert post(browser, form(csrf_token='wrong')).status_code == 400
    with browser.session_transaction(base_url=ISSUER) as session:
        owner = session['account_subject']
    with app.app_context():
        db.session.delete(db.session.get(AccountAdmin, owner))
        db.session.commit()
    assert browser.get('/admin/email-settings', base_url=ISSUER).status_code == 403
    assert post(browser, form()).status_code == 403
    assert app.test_client().post('/admin/email-settings', base_url=ISSUER, data=form()).status_code == 401


def test_password_encrypted_never_echoed_and_blank_keeps_it(app, browser):
    assert post(browser, form()).status_code == 302
    with app.app_context():
        saved = db.session.get(AccountMailSettings, 'recovery')
        assert saved.password_ciphertext != 'private-smtp-password'
        assert 'private-smtp-password' not in saved.password_ciphertext
        assert effective_settings()['password'] == 'private-smtp-password'
    page = browser.get('/admin/email-settings', base_url=ISSUER)
    assert b'private-smtp-password' not in page.data
    assert b'password_ciphertext' not in page.data
    assert post(browser, form(revision=revision(app), password='', port='465', security='ssl')).status_code == 302
    with app.app_context():
        assert effective_settings()['password'] == 'private-smtp-password'
        assert effective_settings()['security'] == 'ssl'


def test_explicit_password_clear_and_new_password_are_unambiguous(app, browser):
    assert post(browser, form()).status_code == 302
    assert post(browser, form(revision=revision(app), clear_password='1')).status_code == 400
    assert post(browser, form(revision=revision(app), password='', clear_password='1')).status_code == 302
    with app.app_context():
        assert effective_settings()['password'] == ''
        assert db.session.get(AccountMailSettings, 'recovery').password_ciphertext is None


def test_environment_fallback_and_existing_password_migration(app, browser):
    app.config.update(ACCOUNTS_SMTP_HOST='environment.example.invalid', ACCOUNTS_SMTP_PORT=2525,
                      ACCOUNTS_SMTP_FROM='env@example.invalid', ACCOUNTS_SMTP_USERNAME='env-user',
                      ACCOUNTS_SMTP_PASSWORD='environment-password', ACCOUNTS_SMTP_STARTTLS=False)
    with app.app_context():
        assert effective_settings()['host'] == 'environment.example.invalid'
        assert effective_settings()['security'] == 'plain'
    assert post(browser, form(password='')).status_code == 302
    with app.app_context():
        assert effective_settings()['password'] == 'environment-password'
    assert post(browser, dict(csrf_token='settings-test-csrf', revision=revision(app), action='use_environment')).status_code == 302
    with app.app_context():
        assert effective_settings()['host'] == 'environment.example.invalid'
        assert not db.session.get(AccountMailSettings, 'recovery').enabled
    assert b'environment-password' not in browser.get('/admin/email-settings', base_url=ISSUER).data


def test_stale_form_does_not_overwrite_new_settings(app, browser):
    assert post(browser, form()).status_code == 302
    assert post(browser, form(host='stale.example.invalid')).status_code == 409
    with app.app_context():
        assert effective_settings()['host'] == 'smtp.example.invalid'


@pytest.mark.parametrize('changes', [
    {'host': 'smtp.example.invalid\r\nDATA'}, {'host': 'https://smtp.example.invalid'},
    {'port': '0'}, {'port': '65536'}, {'port': 'wrong'}, {'sender': 'bad\r\n@example.invalid'},
    {'username': 'bad\nname'}, {'security': 'plain'}, {'password': 'bad\npassword'},
])
def test_invalid_configuration_does_not_save_or_echo_password(app, browser, changes):
    result = post(browser, form(**changes))
    assert result.status_code == 400
    assert b'private-smtp-password' not in result.data
    with app.app_context():
        assert db.session.get(AccountMailSettings, 'recovery') is None


@pytest.mark.parametrize('mode,port,transport', [('starttls', '587', 'SMTP'), ('ssl', '465', 'SMTP_SSL')])
def test_recovery_uses_saved_transport_with_certificate_verification(app, browser, mode, port, transport):
    assert post(browser, form(security=mode, port=port)).status_code == 302
    instance = MagicMock()
    factory = MagicMock()
    factory.return_value.__enter__.return_value = instance
    with app.app_context(), patch('accounts.mail.smtplib.' + transport, factory):
        send_reset_email('recipient@example.invalid', 'private-reset-token')
    assert factory.call_args.args == ('smtp.example.invalid', int(port))
    assert factory.call_args.kwargs['timeout'] == 15
    if mode == 'starttls':
        context = instance.starttls.call_args.kwargs['context']
    else:
        context = factory.call_args.kwargs['context']
        instance.starttls.assert_not_called()
    assert context.check_hostname
    instance.login.assert_called_once_with('accounts', 'private-smtp-password')
    message = instance.send_message.call_args.args[0]
    assert message['From'] == 'accounts@example.invalid'
    assert message['To'] == 'recipient@example.invalid'
    assert ISSUER + '/reset-password/private-reset-token' in message.get_content()


def test_modified_encrypted_secret_fails_without_plaintext_fallback(app, browser):
    assert post(browser, form()).status_code == 302
    app.config['ACCOUNTS_SMTP_PASSWORD'] = 'fallback-must-not-be-used'
    with app.app_context():
        db.session.get(AccountMailSettings, 'recovery').password_ciphertext = 'damaged'
        db.session.commit()
        with pytest.raises(RuntimeError, match='could not be opened'):
            effective_settings()
    assert b'damaged' not in browser.get('/admin/email-settings', base_url=ISSUER).data


def test_admin_revoked_during_save_cannot_commit(app, browser):
    from accounts.mail_settings import _validated_values
    original = _validated_values
    def revoke_after_validation():
        values = original()
        db.session.execute(db.update(Account).where(Account.username == 'mail-admin').values(version=2))
        db.session.commit()
        return values
    with patch('accounts.mail_settings._validated_values', side_effect=revoke_after_validation):
        assert post(browser, form()).status_code == 403
    with app.app_context():
        assert db.session.get(AccountMailSettings, 'recovery') is None


def test_failed_save_rolls_back_without_sensitive_error_output(app, browser):
    with patch.object(db.session, 'commit', side_effect=SQLAlchemyError('raw-secret-details')):
        result = post(browser, form())
    assert result.status_code == 503
    assert b'raw-secret-details' not in result.data
    assert b'private-smtp-password' not in result.data
    with app.app_context():
        assert db.session.get(AccountMailSettings, 'recovery') is None
