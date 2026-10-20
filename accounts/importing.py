"""Strict shared-account import; the caller owns the complete transaction."""
import base64
from hashlib import sha256
import json
import re
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from .models import Account, Membership, db
from .security import valid_email

TRANSFER_PREFIX = b'MICIMACKO-ACCOUNTS-TRANSFER-1\n'
MAX_PLAINTEXT_BYTES = 20 * 1024 * 1024
MAX_TRANSFER_BYTES = 28 * 1024 * 1024
TRANSFER_TTL = 24 * 60 * 60


def valid_password_hash(value):
    if not isinstance(value, str) or len(value) > 255:
        return False
    parts = value.split('$')
    if len(parts) != 3 or not re.fullmatch(r'[A-Za-z0-9]{1,64}', parts[1]):
        return False
    method, _, digest = parts
    if re.fullmatch(r'[0-9a-f]+', digest) is None:
        return False
    if method.startswith('scrypt:'):
        try:
            n, r, p = map(int, method.split(':')[1:])
            return 2 <= n <= 65536 and n & (n - 1) == 0 and 1 <= r <= 8 and 1 <= p <= 4 and len(digest) == 128
        except (ValueError, TypeError):
            return False
    if method.startswith('pbkdf2:'):
        try:
            _, algorithm, raw_iterations = method.split(':')
            iterations = int(raw_iterations)
            return algorithm in {'sha256', 'sha512'} and 1 <= iterations <= 5000000 and len(digest) == {'sha256': 64, 'sha512': 128}[algorithm]
        except (ValueError, TypeError):
            return False
    return False


def validate_payload(data, issuer):
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or data.get('source') != 'csupor' or data.get('issuer') != issuer):
        raise ValueError('Invalid import source or issuer.')
    rows = data.get('users')
    if not isinstance(rows, list) or len(rows) > 50000:
        raise ValueError('Invalid import records.')
    subjects, emails, names = set(), set(), set()
    identifiers = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'subject', 'username', 'email', 'password_hash'}:
            raise ValueError('Invalid import fields.')
        subject, username, email, password_hash = (row[k] for k in ('subject', 'username', 'email', 'password_hash'))
        if not isinstance(subject, str):
            raise ValueError('Invalid account subject.')
        try:
            if str(UUID(subject)) != subject:
                raise ValueError('Invalid account subject.')
        except (ValueError, AttributeError):
            raise ValueError('Invalid account subject.') from None
        if (not isinstance(username, str) or not 1 <= len(username) <= 50 or username != username.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in username)):
            raise ValueError('Invalid account username.')
        if not valid_email(email) or email != email.strip().lower() or not valid_password_hash(password_hash):
            raise ValueError('Invalid email or password hash.')
        if subject in subjects or email.casefold() in emails or username.casefold() in names:
            raise ValueError('Duplicate identity.')
        subjects.add(subject); emails.add(email.casefold()); names.add(username.casefold())
        for identifier in {email.casefold(), username.casefold()}:
            owner = identifiers.setdefault(identifier, subject)
            if owner != subject:
                raise ValueError('Ambiguous account identifiers.')
    return rows


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key.')
        value[key] = item
    return value


def decrypt_transfer(data, transfer_key):
    if (not isinstance(transfer_key, str) or not 32 <= len(transfer_key) <= 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in transfer_key)
            or not isinstance(data, bytes) or len(data) > MAX_TRANSFER_BYTES
            or not data.startswith(TRANSFER_PREFIX)):
        raise ValueError('Invalid transfer package.')
    key = base64.urlsafe_b64encode(sha256(b'micimacko-account-transfer-v1\0' + transfer_key.encode('utf-8')).digest())
    try:
        plaintext = Fernet(key).decrypt(data[len(TRANSFER_PREFIX):], ttl=TRANSFER_TTL)
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise ValueError('Transfer package is too large.')
        return json.loads(plaintext.decode('utf-8'), object_pairs_hook=_json_object)
    except (InvalidToken, UnicodeError, ValueError, TypeError, RecursionError):
        raise ValueError('The transfer key, package or its 24-hour validity is invalid.') from None


def apply_import(data, client_id, issuer):
    """Validate then import without commit; retain every existing record verbatim."""
    rows = validate_payload(data, issuer)
    existing = db.session.execute(select(Account).with_for_update()).scalars().all()
    by_subject = {a.subject: a for a in existing}
    by_email = {a.email.casefold(): a for a in existing}
    by_name = {a.username.casefold(): a for a in existing}
    identifiers = {}
    for account in existing:
        for identifier in {account.email.casefold(), account.username.casefold()}:
            if identifiers.setdefault(identifier, account.subject) != account.subject:
                raise ValueError('Ambiguous existing identity.')
    for row in rows:
        subject, email, name = row['subject'], row['email'], row['username']
        matches = {a.subject: a for a in (by_subject.get(subject), by_email.get(email.casefold()), by_name.get(name.casefold())) if a}
        if matches:
            account = matches.get(subject)
            if (len(matches) != 1 or account is None or account.email != email
                    or account.username != name or account.password_hash != row['password_hash']):
                raise ValueError('An imported account conflicts with an existing identity.')
        for identifier in {email.casefold(), name.casefold()}:
            if identifiers.setdefault(identifier, subject) != subject:
                raise ValueError('Ambiguous account identifiers.')
    imported = 0
    memberships = set(db.session.execute(select(Membership.account_subject).where(Membership.client_id == client_id)).scalars())
    for row in rows:
        if row['subject'] not in by_subject:
            db.session.add(Account(**row, version=1, active=True, email_verified=False))
            imported += 1
    db.session.flush()
    for row in rows:
        if row['subject'] not in memberships:
            db.session.add(Membership(account_subject=row['subject'], client_id=client_id))
    db.session.flush()
    return imported, len(rows)
