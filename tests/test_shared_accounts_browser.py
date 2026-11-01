"""Real Chromium regressions for CSP navigation and the single logout button.

Optional browser suite: install requirements-browser.txt, then
python -m playwright install chromium, and run this module with pytest.
"""
from urllib.parse import urlsplit

import pytest

from accounts.models import Account, Client, db as account_db
from app import db
from app.models import User
from app.page_access import PAGE_DEFINITIONS
from app.page_access_models import PageRolePermission
from app.shared_auth_models import ExternalLoginSession
from tests import test_shared_accounts_e2e as e2e

playwright = pytest.importorskip('playwright.sync_api', reason='Optional Chromium regression suite')


@pytest.fixture(scope='module')
def browser():
    with playwright.sync_playwright() as engine:
        instance = engine.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def case():
    value = e2e.SharedAccountsEndToEndTests('test_existing_password_and_identity_work_in_two_separate_systems')
    try:
        value.setUp()
        yield value
    finally:
        value.doCleanups()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    yield context.new_page()
    context.close()


def sign_in(page, case):
    page.goto(case.rp_url + '/login')
    assert page.url.startswith(case.issuer + '/login')
    page.locator('#login').fill('existing-worker')
    page.locator('#password').fill(case.password)
    page.locator('button[type=submit]').click()
    page.wait_for_url(lambda url: str(url).startswith(case.rp_url + '/'), timeout=15000)
    assert page.locator('.sidebar').is_visible()


def test_single_login_submit_returns_to_csupor_without_stale_form(page, case):
    violations = []
    page.on('console', lambda message: violations.append(message.text)
            if 'form-action' in message.text else None)
    sign_in(page, case)
    assert not violations
    assert 'A munkamenet lejárt' not in page.content()
    with case.app.app_context():
        assert User.query.count() == 1
        assert ExternalLoginSession.query.count() == 1


def test_one_logout_click_ends_both_sessions_and_login_works_again(page, case):
    sign_in(page, case)
    page.locator('.sidebar-logout-form button').click()
    page.wait_for_url(case.issuer + '/login')
    assert page.locator('#password').is_visible()
    assert 'Kijelentkezés a közös fiókból' not in page.content()
    with case.provider.app_context():
        assert account_db.session.get(Account, case.subject).version == 2
    with case.app.app_context():
        assert ExternalLoginSession.query.count() == 0
    # A new CSUPOR navigation must require credentials, not silently restore SSO.
    sign_in(page, case)


def test_registration_continues_across_origins_without_second_click(page, case):
    with case.provider.app_context():
        account_db.session.get(Client, 'csupor').allow_registration = True
        account_db.session.commit()
    page.goto(case.rp_url + '/register')
    assert urlsplit(page.url).path == '/register'
    page.locator('[name=username]').fill('new-browser-worker')
    page.locator('[name=email]').fill('new-browser-worker@example.invalid')
    page.locator('[name=password]').fill('New-browser-password-123')
    page.locator('[name=password_confirm]').fill('New-browser-password-123')
    page.locator('button[type=submit]').click()
    page.wait_for_url(lambda url: str(url).startswith(case.rp_url + '/'), timeout=15000)
    assert page.locator('.sidebar').is_visible()


def test_no_pages_screen_logs_out_with_one_click(page, case):
    with case.app.app_context():
        for definition in PAGE_DEFINITIONS:
            db.session.merge(PageRolePermission(page_key=definition['key'], role='hr', allowed=False))
        db.session.commit()
    sign_in(page, case)
    assert urlsplit(page.url).path == '/access-unavailable'
    page.locator('#main-content form[action="/logout"] button').click()
    page.wait_for_url(case.issuer + '/login')
    assert page.locator('#password').is_visible()
    with case.provider.app_context():
        assert account_db.session.get(Account, case.subject).version == 2


def test_login_handoff_has_working_javascript_disabled_fallback(browser, case):
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.goto(case.rp_url + '/login')
        page.locator('#login').fill('existing-worker')
        page.locator('#password').fill(case.password)
        page.locator('button[type=submit]').click()
        page.wait_for_url(lambda url: urlsplit(str(url)).path == '/login/continue')
        page.locator('#login-continuation').click()
        page.wait_for_url(lambda url: str(url).startswith(case.rp_url + '/'))
        assert page.locator('.sidebar').is_visible()
    finally:
        context.close()
