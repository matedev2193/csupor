"""Real browser profile move, photo crop and CSUPOR return regression."""
from io import BytesIO

import pytest
from PIL import Image

pytest.importorskip('playwright.sync_api')
from tests.test_shared_accounts_browser import browser, case, page, sign_in
from accounts.models import db as account_db
from accounts.profile_models import AccountProfile
from app import db
from app.models import User


def test_complete_profile_editor_photo_and_return_to_csupor(page, case):
    violations = []
    page.on('console', lambda message: violations.append(message.text)
            if message.type == 'error' and 'Content Security Policy' in message.text else None)
    sign_in(page, case)
    page.goto(case.rp_url + '/profile')
    page.wait_for_url(case.issuer + '/profile')
    assert page.locator('#full_name').input_value() == 'Meglévő Munkatárs'
    assert page.locator('.personal-profile-form [name]').count() == 19
    page.locator('#full_name').fill('Központban Módosított Munkatárs')
    page.locator('#phone_number').fill('+36 30 123 4567')
    page.locator('.personal-profile-form button[type=submit]').click()
    assert page.locator('#full_name').input_value() == 'Központban Módosított Munkatárs'

    # Saving a photo must not submit or discard the unsaved personal form.
    page.locator('#full_name').fill('Még nem mentett név')
    data = BytesIO()
    Image.new('RGB', (600, 400), '#147d77').save(data, 'PNG')
    page.locator('input[name=photo]').set_input_files({
        'name': 'avatar.png', 'mimeType': 'image/png', 'buffer': data.getvalue(),
    })
    dialog = page.locator('#profile-photo-editor')
    assert dialog.is_visible()
    page.locator('[data-photo-save]').click()
    dialog.wait_for(state='hidden')
    assert page.locator('#full_name').input_value() == 'Még nem mentett név'
    assert page.locator('.profile-photo-preview img').is_visible()
    page.locator('[data-photo-edit]').click()
    page.locator('[data-photo-save]').wait_for(state='visible')
    assert page.locator('[data-photo-zoom]').input_value() == '1'
    page.locator('[data-photo-cancel]').click()
    page.goto(case.rp_url + '/dashboard')
    with case.app.app_context():
        person = db.session.get(User, case.user_id)
        assert person.profile.full_name == 'Központban Módosított Munkatárs'
        assert person.profile.phone_number == '+36 30 123 4567'
        assert person.photo is not None
    with case.provider.app_context():
        assert account_db.session.get(AccountProfile, case.subject).fields['full_name'] == 'Központban Módosított Munkatárs'
    assert not violations


def test_profile_editor_fits_mobile_viewport(page, case):
    sign_in(page, case)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto(case.rp_url + '/profile')
    page.wait_for_url(case.issuer + '/profile')
    assert page.locator('#full_name').is_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert page.locator('#bank_account_number').count() == 1
