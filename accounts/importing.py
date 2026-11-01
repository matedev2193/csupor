"""Strict shared-account import; the caller owns the complete transaction."""
import base64
from hashlib import sha256
import json
import re
import time
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from .models import Account, Membership, db
from .security import valid_email

TRANSFER_PREFIX = b'MICIMACKO-ACCOUNTS-TRANSFER-1\n'
MAX_PLAINTEXT_BYTES = 20 * 1024 * 1024
MAX_TRANSFER_BYTES = 28 * 1024 * 1024
TRANSFER_TTL = 24 * 60 * 60

_IMPORT_ERRORS = {
    'TRANSFER_KEY': 'Az export átviteli kulcsa hiányzik vagy érvénytelen. Az exportáláskor megadott, legalább 32 karakteres átviteli kulcsot add meg.',
    'TRANSFER_FORMAT': 'A feltöltött fájl nem támogatott titkosított CSUPOR-export, vagy túl nagy. A CSUPOR fiókexport oldaláról letöltött eredeti fájlt válaszd ki.',
    'TRANSFER_DECRYPT': 'Az export nem fejthető vissza ezzel az átviteli kulccsal, vagy a fájl megsérült. Az exporthoz használt átviteli kulcsot add meg; szükség esetén készíts új exportot.',
    'TRANSFER_EXPIRED': 'Az export 24 órás érvényessége lejárt. Készíts új exportot a CSUPOR-ban, majd azt töltsd fel az ahhoz tartozó átviteli kulccsal.',
    'TRANSFER_TIME': 'Az export időbélyege a jövőben van. Ellenőrizd a két kiszolgáló óráját, majd készíts új exportot.',
    'TRANSFER_CONTENT': 'Az export visszafejthető, de a tartalma hibás vagy túl nagy. Készíts új exportot a CSUPOR fiókexport oldalán.',
    'EXPORT_FORMAT': 'Az export adatformátuma nem támogatott. A frissített CSUPOR fiókexport oldalán készíts új exportot.',
    'EXPORT_ISSUER': 'Az exportban megadott fiókkezelő-cím eltér a fiókkezelő ACCOUNTS_ISSUER beállításától. Ugyanazzal a HTTPS-címmel készíts új exportot.',
    'EXPORT_IDENTITY': 'Az export hibás vagy egymással ütköző fiókazonosítókat tartalmaz. A CSUPOR fiókadatait kell ellenőrizni az új export előtt.',
    'EXPORT_EMAIL': 'Az export egyik e-mail-címe érvénytelen formátumú. Ellenőrizd a CSUPOR fiókadatait, majd készíts új exportot.',
    'EXPORT_PASSWORD': 'Az export egyik jelszólenyomatának formátuma nem támogatott. A CSUPOR és a fiókkezelő verziójának, illetve a jelszóformátumnak az ellenőrzése szükséges.',
    'ACCOUNT_CONFLICT': 'Az export egyik fiókja ütközik a központban már meglévő fiókadatokkal. Az érintett fiókok egyeztetése szükséges; a meglévő adatokat nem írjuk felül.',
}


class ImportValidationError(ValueError):
    """A safe operator diagnostic; never include raw uploaded or database values."""

    def __init__(self, code):
        self.code = code
        self.message = _IMPORT_ERRORS[code]
        super().__init__(self.message)


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
            or data.get('source') != 'csupor'):
        raise ImportValidationError('EXPORT_FORMAT')
    if data.get('issuer') != issuer:
        raise ImportValidationError('EXPORT_ISSUER')
    rows = data.get('users')
    if not isinstance(rows, list) or len(rows) > 50000:
        raise ImportValidationError('EXPORT_FORMAT')
    subjects, emails, names = set(), set(), set()
    identifiers = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'subject', 'username', 'email', 'password_hash'}:
            raise ImportValidationError('EXPORT_FORMAT')
        subject, username, email, password_hash = (row[k] for k in ('subject', 'username', 'email', 'password_hash'))
        if not isinstance(subject, str):
            raise ImportValidationError('EXPORT_IDENTITY')
        try:
            if str(UUID(subject)) != subject:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ImportValidationError('EXPORT_IDENTITY') from None
        if (not isinstance(username, str) or not 1 <= len(username) <= 50 or username != username.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in username)):
            raise ImportValidationError('EXPORT_IDENTITY')
        if not valid_email(email) or email != email.strip().lower():
            raise ImportValidationError('EXPORT_EMAIL')
        if not valid_password_hash(password_hash):
            raise ImportValidationError('EXPORT_PASSWORD')
        if subject in subjects or email.casefold() in emails or username.casefold() in names:
            raise ImportValidationError('EXPORT_IDENTITY')
        subjects.add(subject); emails.add(email.casefold()); names.add(username.casefold())
        for identifier in {email.casefold(), username.casefold()}:
            owner = identifiers.setdefault(identifier, subject)
            if owner != subject:
                raise ImportValidationError('EXPORT_IDENTITY')
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
            or any(ord(c) < 32 or ord(c) == 127 for c in transfer_key)):
        raise ImportValidationError('TRANSFER_KEY')
    if (not isinstance(data, bytes) or len(data) > MAX_TRANSFER_BYTES
            or not data.startswith(TRANSFER_PREFIX)):
        raise ImportValidationError('TRANSFER_FORMAT')
    try:
        key = base64.urlsafe_b64encode(sha256(b'micimacko-account-transfer-v1\0' + transfer_key.encode('utf-8')).digest())
    except UnicodeError:
        raise ImportValidationError('TRANSFER_KEY') from None
    cipher = Fernet(key)
    token = data[len(TRANSFER_PREFIX):]
    try:
        plaintext = cipher.decrypt(token, ttl=TRANSFER_TTL)
    except InvalidToken:
        # extract_timestamp verifies the signature: never trust an unauthenticated
        # timestamp to report expiry, and never decrypt without the original TTL.
        try:
            timestamp = cipher.extract_timestamp(token)
        except InvalidToken:
            raise ImportValidationError('TRANSFER_DECRYPT') from None
        now = int(time.time())
        if timestamp + TRANSFER_TTL < now:
            raise ImportValidationError('TRANSFER_EXPIRED') from None
        if timestamp > now:
            # Fernet has already rejected the timestamp using its clock-skew
            # allowance; this diagnostic does not change that acceptance policy.
            raise ImportValidationError('TRANSFER_TIME') from None
        raise ImportValidationError('TRANSFER_DECRYPT') from None
    try:
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise ValueError()
        return json.loads(plaintext.decode('utf-8'), object_pairs_hook=_json_object)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ImportValidationError('TRANSFER_CONTENT') from None


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
                raise ImportValidationError('ACCOUNT_CONFLICT')
    for row in rows:
        subject, email, name = row['subject'], row['email'], row['username']
        matches = {a.subject: a for a in (by_subject.get(subject), by_email.get(email.casefold()), by_name.get(name.casefold())) if a}
        if matches:
            account = matches.get(subject)
            if (len(matches) != 1 or account is None or account.email != email
                    or account.username != name or account.password_hash != row['password_hash']):
                raise ImportValidationError('ACCOUNT_CONFLICT')
        for identifier in {email.casefold(), name.casefold()}:
            if identifiers.setdefault(identifier, subject) != subject:
                raise ImportValidationError('ACCOUNT_CONFLICT')
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
