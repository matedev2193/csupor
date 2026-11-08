"""Central profile changes preserve legacy HR data and cannot cross identities."""
import base64
from datetime import date, datetime
from io import BytesIO
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import g
from PIL import Image

from app import create_app, db
from app.models import ProfilePhoto, ProfilePhotoSource, User, UserProfile
from app.shared_profile import FIELDS, _request_json, sync_own_profile, sync_report_profiles
from app.shared_auth_models import ExternalIdentity


def jpeg(size=(32, 32)):
    stream = BytesIO()
    Image.new('RGB', size, 'blue').save(stream, 'JPEG')
    return stream.getvalue()


class SharedProfileSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'DATABASE_URL': 'sqlite:///' + self.directory.name + '/test.db',
                                    'SECRET_KEY': 'profile-test-secret-' + 'x' * 32,
                                    'AUTH_MODE': 'local', 'EMAIL_WORKER_ENABLED': 'false'}):
            self.app = create_app()
        self.app.config.update(TESTING=True, OIDC_ISSUER='https://accounts.example.invalid',
                               OIDC_CLIENT_ID='csupor', OIDC_CLIENT_SECRET='client-secret')
        self.context = self.app.test_request_context('/profile')
        self.context.push()
        self.user = User(username='worker', email='worker@example.invalid', password_hash='unused')
        self.user.profile = UserProfile(full_name='Legacy name', date_of_birth=date(1980, 1, 1),
                                        tax_number='1234567890')
        db.session.add(self.user)
        db.session.commit()
        self.subject = '00000000-0000-0000-0000-000000000001'

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.directory.cleanup()

    def payload(self, **fields):
        data = dict.fromkeys(FIELDS)
        data.update(fields)
        return {'sub': self.subject, 'version': 'a' * 32, 'legacy_imported': True,
                'photo_version': '', 'fields': data}

    def test_first_import_preserves_existing_fields_and_updates_authoritative_blanks(self):
        absent = {'sub': self.subject, 'version': '', 'legacy_imported': False,
                  'photo_version': '', 'fields': {}}
        imported = self.payload(full_name='Legacy name', date_of_birth='1980-01-01', tax_number='1234567890')
        with patch('app.shared_profile._request_json', side_effect=[absent, imported]) as remote:
            self.assertTrue(sync_own_profile(self.user, self.subject, 'bearer'))
        sent = remote.call_args.kwargs['payload']['fields']
        self.assertEqual(set(sent), set(FIELDS))
        self.assertEqual(sent['date_of_birth'], '1980-01-01')
        self.assertEqual(sent['tax_number'], '1234567890')
        self.assertTrue(g.shared_profile_ready)
        with patch('app.shared_profile._request_json', return_value=self.payload(full_name='Central name')) as remote:
            self.assertTrue(sync_own_profile(self.user, self.subject, 'bearer'))
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(self.user.profile.full_name, 'Central name')
        self.assertIsNone(self.user.profile.tax_number)
        self.assertIsNone(self.user.profile.date_of_birth)

    def test_unavailable_or_untrusted_profile_never_overwrites_local_data(self):
        invalid = [self.payload(full_name='Changed') for _ in range(5)]
        invalid[0]['sub'] = 'another-subject'
        invalid[1]['fields'].pop('tax_number')
        invalid[2]['fields']['date_of_birth'] = 'not-a-date'
        invalid[3]['fields']['full_name'] = 'x' * 121
        invalid[4]['version'] = 'broken'
        for payload in invalid:
            with self.subTest(payload=payload), patch('app.shared_profile._request_json', return_value=payload):
                self.assertFalse(sync_own_profile(self.user, self.subject, 'bearer'))
                self.assertEqual(self.user.profile.full_name, 'Legacy name')
                self.assertEqual(self.user.profile.tax_number, '1234567890')
        with patch('app.shared_profile._request_json', side_effect=TimeoutError('not logged')):
            self.assertFalse(sync_own_profile(self.user, self.subject, 'bearer'))
        self.assertEqual(self.user.profile.full_name, 'Legacy name')

    def test_photo_import_preserves_source_and_new_avatar_is_synchronised(self):
        old = jpeg()
        self.user.photo = ProfilePhoto(version='b' * 32, data=old, mime_type='image/jpeg',
                                       width=32, height=32, size_bytes=len(old), updated_at=datetime.now())
        self.user.photo_source = ProfilePhotoSource(data=old, width=32, height=32, size_bytes=len(old),
                                                   center_x=0.5, center_y=0.5, zoom=1)
        db.session.commit()
        initial = self.payload(full_name='Legacy name')
        imported = {**initial, 'photo_version': 'b' * 32}
        with patch('app.shared_profile._request_json', side_effect=[initial, {'sub': self.subject}, imported]) as remote:
            self.assertTrue(sync_own_profile(self.user, self.subject, 'bearer'))
        self.assertEqual(base64.b64decode(remote.call_args_list[1].kwargs['payload']['source_data']), old)
        updated = {**initial, 'photo_version': 'c' * 32}
        new = jpeg((24, 24))
        photo = {'sub': self.subject, 'version': 'c' * 32, 'data': base64.b64encode(new).decode(),
                 'width': 24, 'height': 24}
        with patch('app.shared_profile._request_json', side_effect=[updated, photo]):
            self.assertTrue(sync_own_profile(self.user, self.subject, 'bearer'))
        self.assertEqual(self.user.photo.data, new)
        self.assertEqual(self.user.photo_source.data, old)

    def test_photo_failure_rolls_back_profile_and_preserves_original(self):
        payload = {**self.payload(full_name='Uncommitted'), 'photo_version': 'c' * 32}
        invalid_photo = {'sub': self.subject, 'version': 'c' * 32, 'data': 'not base64', 'width': 1, 'height': 1}
        with patch('app.shared_profile._request_json', side_effect=[payload, invalid_photo]):
            self.assertFalse(sync_own_profile(self.user, self.subject, 'bearer'))
        self.assertEqual(self.user.profile.full_name, 'Legacy name')
        self.assertIsNone(self.user.photo)

    def test_http_is_fixed_origin_bounded_and_never_follows_redirects(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [json.dumps({'ok': True}).encode()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch('app.shared_profile.requests.request', return_value=response) as remote:
            self.assertEqual(_request_json('GET', '/api/csupor/profile', token='private-token'), {'ok': True})
        self.assertEqual(remote.call_args.args[1], 'https://accounts.example.invalid/api/csupor/profile')
        self.assertEqual(remote.call_args.kwargs['headers']['Authorization'], 'Bearer private-token')
        self.assertFalse(remote.call_args.kwargs['allow_redirects'])
        self.assertEqual(remote.call_args.kwargs['timeout'], 5)
        response.iter_content.return_value = [b'x' * 101]
        with patch('app.shared_profile.requests.request', return_value=response):
            with self.assertRaises(ValueError):
                _request_json('GET', '/api/csupor/profile', token='private-token', limit=100)
        response.status_code = 302
        with patch('app.shared_profile.requests.request', return_value=response):
            with self.assertRaises(ValueError):
                _request_json('GET', '/api/csupor/profile', token='private-token')

    def test_report_batch_updates_only_existing_subjects_and_rejects_extra_subject(self):
        other = User(username='other', email='other@example.invalid', password_hash='unused')
        other.profile = UserProfile(full_name='Old colleague')
        db.session.add(other)
        db.session.flush()
        other_subject = '00000000-0000-0000-0000-000000000002'
        db.session.add(ExternalIdentity(issuer=self.app.config['OIDC_ISSUER'], subject=other_subject, user_id=other.id))
        db.session.commit()
        self.context.request.url_rule = Mock(endpoint='manage_user_profiles')
        payload = {**self.payload(full_name='New colleague', date_of_birth='1990-05-03'), 'sub': other_subject}
        with patch('app.shared_profile._request_json', return_value={'profiles': [payload]}) as remote:
            sync_report_profiles(self.user)
        self.assertEqual(other.profile.full_name, 'New colleague')
        self.assertEqual(remote.call_args.kwargs['payload']['subjects'], [other_subject])
        self.assertTrue(remote.call_args.kwargs['service'])
        payload['sub'] = self.subject
        with patch('app.shared_profile._request_json', return_value={'profiles': [payload]}):
            sync_report_profiles(self.user)
        self.assertTrue(g.shared_profile_cache_stale)
        self.assertEqual(other.profile.full_name, 'New colleague')
