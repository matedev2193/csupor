"""Authenticated transfer diagnostics retain expiry and payload validation."""
import base64
from hashlib import sha256
import json

from cryptography.fernet import Fernet
import pytest
from werkzeug.security import generate_password_hash

from accounts import importing
from accounts.importing import ImportValidationError, TRANSFER_PREFIX, TRANSFER_TTL, decrypt_transfer, validate_payload

NOW = 1800000000
KEY = 'test-export-transfer-key-over-thirty-two-characters'
ISSUER = 'https://accounts.example.invalid'


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(importing.time, 'time', lambda: NOW)


@pytest.fixture
def payload():
    return {'version': 1, 'source': 'csupor', 'issuer': ISSUER, 'users': [{
        'subject': 'dc693287-ae48-41e9-b5b1-f0bd56bb8421',
        'username': 'private-user', 'email': 'private-user@example.invalid',
        'password_hash': generate_password_hash('private-password', method='pbkdf2:sha256:1000'),
    }]}


def encrypt(payload, timestamp=NOW, *, plaintext=None):
    key = base64.urlsafe_b64encode(sha256(b'micimacko-account-transfer-v1\0' + KEY.encode()).digest())
    token = Fernet(key).encrypt_at_time(json.dumps(payload).encode() if plaintext is None else plaintext, timestamp)
    return TRANSFER_PREFIX + token


def failure(call, code, *private_values):
    with pytest.raises(ImportValidationError) as caught:
        call()
    error = caught.value
    assert isinstance(error, ValueError)  # Existing CLI callers retain compatibility.
    assert error.code == code
    assert str(error) == error.message
    for value in (KEY, *private_values):
        assert value not in str(error)
        assert value not in repr(error)


def test_authenticated_expiry_is_distinct_and_boundary_is_unchanged(payload):
    assert decrypt_transfer(encrypt(payload, NOW - TRANSFER_TTL), KEY) == payload
    failure(lambda: decrypt_transfer(encrypt(payload, NOW - TRANSFER_TTL - 1), KEY), 'TRANSFER_EXPIRED')


def test_wrong_key_on_expired_file_does_not_claim_authenticated_expiry(payload):
    wrong_key = 'wrong-export-key-over-thirty-two-characters'
    failure(lambda: decrypt_transfer(encrypt(payload, NOW - TRANSFER_TTL - 1), wrong_key),
            'TRANSFER_DECRYPT', wrong_key)


def test_tampered_timestamp_is_not_reported_as_expiry(payload):
    token = encrypt(payload)[len(TRANSFER_PREFIX):]
    raw = bytearray(base64.urlsafe_b64decode(token))
    raw[1:9] = (NOW - TRANSFER_TTL - 10).to_bytes(8, 'big')
    tampered = TRANSFER_PREFIX + base64.urlsafe_b64encode(raw)
    failure(lambda: decrypt_transfer(tampered, KEY), 'TRANSFER_DECRYPT')


def test_future_clock_skew_policy_is_unchanged(payload):
    assert decrypt_transfer(encrypt(payload, NOW + 60), KEY) == payload
    failure(lambda: decrypt_transfer(encrypt(payload, NOW + 61), KEY), 'TRANSFER_TIME')


@pytest.mark.parametrize('invalid', [b'{}', b'', TRANSFER_PREFIX + b'broken-token'])
def test_wrong_file_and_damaged_cipher_have_actionable_codes(invalid):
    code = 'TRANSFER_DECRYPT' if invalid.startswith(TRANSFER_PREFIX) else 'TRANSFER_FORMAT'
    failure(lambda: decrypt_transfer(invalid, KEY), code)


@pytest.mark.parametrize('key', ['', 'short', 'long-but-control-character-key-' + '\n' + 'x' * 32, '\ud800' * 32])
def test_malformed_transfer_key_never_reaches_decryption(payload, key):
    failure(lambda: decrypt_transfer(encrypt(payload), key), 'TRANSFER_KEY')


@pytest.mark.parametrize('plaintext', [b'{"private-token":"secret-value",', b'{"version":1,"version":1}', b'\xff'])
def test_authenticated_invalid_content_is_distinct_from_key_error(payload, plaintext):
    failure(lambda: decrypt_transfer(encrypt(payload, plaintext=plaintext), KEY),
            'TRANSFER_CONTENT', 'secret-value')


def test_oversized_packages_still_rejected_before_and_after_decryption(payload, monkeypatch):
    package = encrypt(payload)
    monkeypatch.setattr(importing, 'MAX_TRANSFER_BYTES', len(package) - 1)
    failure(lambda: decrypt_transfer(package, KEY), 'TRANSFER_FORMAT')
    monkeypatch.setattr(importing, 'MAX_TRANSFER_BYTES', len(package))
    monkeypatch.setattr(importing, 'MAX_PLAINTEXT_BYTES', 1)
    failure(lambda: decrypt_transfer(package, KEY), 'TRANSFER_CONTENT')


def test_wrong_issuer_has_specific_safe_message(payload):
    payload['issuer'] = 'https://private-other-host.example.invalid'
    failure(lambda: validate_payload(payload, ISSUER), 'EXPORT_ISSUER', payload['issuer'])


@pytest.mark.parametrize('field,value,code', [
    ('subject', 'invalid-subject-private-value', 'EXPORT_IDENTITY'),
    ('username', ' invalid-private-user ', 'EXPORT_IDENTITY'),
    ('email', 'invalid-private-email', 'EXPORT_EMAIL'),
    ('password_hash', 'private-unsupported-hash', 'EXPORT_PASSWORD'),
])
def test_bad_identity_diagnostics_do_not_include_uploaded_values(payload, field, value, code):
    payload['users'][0][field] = value
    failure(lambda: validate_payload(payload, ISSUER), code, value)


def test_cross_identifier_conflict_is_preserved(payload):
    second = dict(payload['users'][0], subject='cd74d1e7-91df-441d-9f55-b560d196a049',
                  username='other-user', email='other-user@example.invalid')
    second['username'] = payload['users'][0]['email']
    payload['users'].append(second)
    failure(lambda: validate_payload(payload, ISSUER), 'EXPORT_IDENTITY', second['username'])
