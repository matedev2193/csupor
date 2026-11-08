"""Real HTTP publication uses existing app credentials and independent databases."""
from unittest.mock import patch

import pytest
import requests

from accounts.models import db as accounts_db
from accounts.workplaces import WorkplaceCatalogue, validate_workplaces
from app import db
from app.models import LegalEntity, PlaceOfWork
from app.shared_workplaces import SharedWorkplaceExport, _allocate_snapshot, publish_workplaces
import test_shared_accounts_e2e as shared_fixture


@pytest.fixture
def live():
    fixture = shared_fixture.SharedAccountsEndToEndTests(methodName='runTest')
    fixture.setUp()
    try:
        with fixture.app.app_context():
            entity = LegalEntity(name='Óvoda', address='Private company address',
                                 om_id='123456', tax_number='12345678901')
            db.session.add(entity)
            db.session.flush()
            place = PlaceOfWork(legal_entity_id=entity.id, address='Első utca 1.')
            db.session.add(place)
            db.session.commit()
            fixture.entity_id, fixture.place_id = entity.id, place.id
        yield fixture
    finally:
        fixture.doCleanups()


def saved(live):
    with live.provider.app_context():
        row = accounts_db.session.get(WorkplaceCatalogue, 1)
        return (row.generation, row.revision, row.workplaces) if row else None


def test_first_authenticated_visit_and_saved_edits_publish_automatically(live):
    # No external request at factory/bootstrap time, nor from an anonymous visit.
    assert saved(live) is None
    live.request('GET', live.rp_url + '/login', allow_redirects=False)
    assert saved(live) is None
    live.sign_in()
    generation, revision, rows = saved(live)
    assert generation == 1
    assert rows == [{'id': live.place_id, 'name': 'Óvoda – Első utca 1.', 'address': 'Első utca 1.'}]
    assert '12345678901' not in repr(rows)
    assert 'Private company address' not in repr(rows)
    assert 'existing-worker' not in repr(rows)
    # Cooldown visits do not create revisions or repeat publication.
    with patch('app.shared_workplaces.requests.post', side_effect=AssertionError('Unexpected repeat')) as post:
        assert live.request('GET', live.rp_url + '/places-of-work').status_code == 200
        post.assert_not_called()
    result = live.request('POST', live.rp_url + f'/places-of-work/{live.place_id}/edit',
        data={'legal_entity_id': live.entity_id, 'address': 'Második utca 2.'}, allow_redirects=False)
    assert result.status_code == 302
    updated_generation, updated_revision, rows = saved(live)
    assert updated_generation == generation + 1
    assert updated_revision != revision
    assert rows[0]['address'] == 'Második utca 2.'
    result = live.request('POST', live.rp_url + f'/legal-entities/{live.entity_id}/edit',
        data={'name': 'Átnevezett óvoda', 'address': 'Company address', 'om_id': '123456',
              'tax_number': '12345678901'}, allow_redirects=False)
    assert result.status_code == 302
    assert saved(live)[2][0]['name'] == 'Átnevezett óvoda – Második utca 2.'


def test_outage_keeps_source_edits_and_last_catalogue_then_retries(live, caplog):
    live.sign_in()
    original = saved(live)
    with patch('app.shared_workplaces.requests.post', side_effect=requests.Timeout('secret db/password')):
        response = live.request('POST', live.rp_url + f'/places-of-work/{live.place_id}/edit',
            data={'legal_entity_id': live.entity_id, 'address': 'Megőrzött módosítás'}, allow_redirects=False)
        assert response.status_code == 302
    assert 'secret db/password' not in caplog.text
    assert saved(live) == original
    with live.app.app_context():
        assert db.session.get(PlaceOfWork, live.place_id).address == 'Megőrzött módosítás'
        assert db.session.get(SharedWorkplaceExport, 1).generation == 2
    live.app.extensions['shared_workplace_publisher']['next_attempt'] = 0
    assert live.request('GET', live.rp_url + '/places-of-work').status_code == 200
    assert saved(live)[0] == 2
    assert saved(live)[2][0]['address'] == 'Megőrzött módosítás'


def test_generated_labels_are_bounded_unique_and_snapshots_are_stable(live):
    with live.app.app_context():
        entity = db.session.get(LegalEntity, live.entity_id)
        entity.name = 'Hosszú óvodanév ' * 7
        db.session.add_all([
            PlaceOfWork(legal_entity_id=entity.id, address='Első utca 1.'),
            PlaceOfWork(legal_entity_id=entity.id, address='Hosszú cím ' * 20),
            PlaceOfWork(legal_entity_id=entity.id, address='Több\nsoros\tcím'),
        ])
        db.session.commit()
        first, revision = _allocate_snapshot()
        second, repeated_revision = _allocate_snapshot()
        assert first == second
        assert revision == repeated_revision
        assert first['generation'] == 1
        assert validate_workplaces(first['workplaces']) == first['workplaces']
        assert len({row['name'].casefold() for row in first['workplaces']}) == 4
        assert first['workplaces'][-1]['address'] == 'Több soros cím'


def test_local_auth_and_other_clients_never_publish(live):
    with live.app.app_context(), patch('app.shared_workplaces.requests.post') as post:
        live.app.config['AUTH_MODE'] = 'local'
        assert publish_workplaces(force=True) is None
        live.app.config.update(AUTH_MODE='oidc', OIDC_CLIENT_ID='another-client')
        assert publish_workplaces(force=True) is None
        post.assert_not_called()
        assert db.session.get(SharedWorkplaceExport, 1) is None
