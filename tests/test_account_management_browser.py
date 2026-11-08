"""Real browser checks for the central account navigation and admin forms."""
import pytest

pytest.importorskip('playwright.sync_api')
from tests.test_shared_accounts_browser import browser, case, page, sign_in
from accounts.admin import AccountAdmin
from accounts.models import Account, Membership, db


def make_admin(case):
    with case.provider.app_context():
        db.session.add(AccountAdmin(account_subject=case.subject))
        db.session.commit()


def test_application_link_and_central_account_creation(page, case):
    make_admin(case)
    sign_in(page, case)
    page.goto(case.issuer + '/account')
    link = page.locator('.application-link').filter(has_text='CSUPOR')
    assert link.get_attribute('href') == case.rp_url + '/'
    link.click()
    page.wait_for_url(lambda url: str(url).startswith(case.rp_url + '/'))
    assert page.locator('.sidebar').is_visible()

    page.goto(case.issuer + '/admin')
    page.get_by_role('link', name='Új közös fiók létrehozása').click()
    page.locator('#username').fill('browser-created-worker')
    page.locator('#email').fill('browser-created-worker@example.invalid')
    page.locator('#password').fill('Browser-new-account-password')
    page.locator('#password-confirm').fill('Browser-new-account-password')
    page.locator('[name=client_ids][value=csupor]').check()
    page.get_by_role('button', name='Fiók létrehozása', exact=True).click()
    page.wait_for_url(case.issuer + '/admin/accounts')
    with case.provider.app_context():
        created = Account.query.filter_by(username='browser-created-worker').one()
        subject = created.subject
        assert db.session.get(Membership, (subject, 'csupor')) is not None
        assert db.session.get(AccountAdmin, subject) is None
    page.locator(f'form[action="/admin/accounts/{subject}/status"] button').click()
    with case.provider.app_context():
        assert not db.session.get(Account, subject).active


def test_central_mail_settings_mobile_save_without_sending(page, case):
    make_admin(case)
    sign_in(page, case)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto(case.issuer + '/admin/email-settings')
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.locator('#host').fill('smtp.example.invalid')
    page.locator('#sender').fill('accounts@example.invalid')
    page.locator('#password').fill('Private-SMTP-test-password')
    page.get_by_role('button', name='Levélküldési beállítások mentése').click()
    assert page.locator('#host').input_value() == 'smtp.example.invalid'
    assert page.locator('#password').input_value() == ''
    assert 'Private-SMTP-test-password' not in page.content()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
