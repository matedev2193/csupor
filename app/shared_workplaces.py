"""Publish only workplace labels and addresses to the shared account directory."""
from collections import Counter
from hashlib import sha256
import json
from threading import Lock
from time import monotonic
import unicodedata

from flask import current_app, g, request
from flask_login import current_user
import requests
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from urllib3.exceptions import TimeoutError as TransportTimeout

from . import db
from .models import LegalEntity, PlaceOfWork


_FAILURES = {
    'AUTH_REJECTED': 'A fiókkezelő elutasította a CSUPOR alkalmazás hitelesítését.',
    'ENDPOINT_MISSING': 'A fiókkezelő telephelyfogadó végpontja még nem érhető el; frissítsd a fiókkezelőt.',
    'STALE_SNAPSHOT': 'A fiókkezelő újabb telephelyváltozatot tárol; a régebbi lista nem írta felül.',
    'RATE_LIMITED': 'A fiókkezelő átmenetileg korlátozta a telephelylista küldését.',
    'UPSTREAM_UNAVAILABLE': 'A fiókkezelő telephelyfogadó szolgáltatása átmenetileg nem működik.',
    'CONFIG_MISSING': 'A fiókkezelőn hiányzik az OIDC_CLIENT_ID környezeti változó; állítsd a CSUPOR alkalmazás beállított azonosítójára.',
    'CONFIG_INVALID': 'A fiókkezelőn hibás az OIDC_CLIENT_ID környezeti változó; a CSUPOR alkalmazás beállított azonosítóját add meg.',
    'CONFIG_CONFLICT': 'A fiókkezelő OIDC_CLIENT_ID és SAFETY_OIDC_CLIENT_ID változói nem azonosíthatják ugyanazt az alkalmazást.',
    'UPSTREAM_REJECTED': 'A fiókkezelő nem fogadta el a telephelylista küldését.',
    'TIMEOUT': 'A fiókkezelő nem válaszolt időben a telephelylista küldésekor.',
    'NETWORK': 'A telephelylista küldésekor megszakadt vagy nem jött létre a kapcsolat.',
    'INVALID_SOURCE': 'A CSUPOR telephelyadataiból nem állítható össze érvényes lista.',
    'SOURCE_DATABASE': 'A CSUPOR telephelylistáját nem sikerült kiolvasni vagy előkészíteni az adatbázisban.',
    'SOURCE_CHANGED': 'A CSUPOR telephelylistája párhuzamos módosítás miatt még nem küldhető.',
    'INVALID_RESPONSE': 'A fiókkezelő nem adott érvényes visszaigazolást a telephelylistáról.',
    'INTERNAL': 'A telephelylista küldése váratlan hiba miatt nem sikerült.',
}


class WorkplacePublishError(ValueError):
    """A fixed diagnostic code, never a source row or a remote error body."""

    def __init__(self, code):
        self.code = code if code in _FAILURES else 'INTERNAL'
        super().__init__(self.code)


def _source_text(value, limit):
    if not isinstance(value, str):
        raise WorkplacePublishError('INVALID_SOURCE')
    # Pasted zero-width/bidi/control characters cannot enter the shared
    # directory. Whitespace controls still separate words. Only the exported
    # copy changes; the original CSUPOR fields remain untouched.
    clean = ''.join(' ' if char.isspace() else char
                    for char in value if char.isspace() or not unicodedata.category(char).startswith('C'))
    clean = ' '.join(clean.split())
    if not 1 <= len(clean) <= limit:
        raise WorkplacePublishError('INVALID_SOURCE')
    return clean


def _upstream_error(status):
    if status in {401, 403}:
        return 'AUTH_REJECTED'
    if status == 404:
        return 'ENDPOINT_MISSING'
    if status == 409:
        return 'STALE_SNAPSHOT'
    if status == 429:
        return 'RATE_LIMITED'
    if 500 <= status <= 599:
        return 'UPSTREAM_UNAVAILABLE'
    return 'UPSTREAM_REJECTED'


def _configuration_error(response):
    """Recognise only this integration's fixed, bounded configuration codes."""
    raw = bytearray()
    for chunk in response.iter_content(1024):
        raw.extend(chunk)
        if len(raw) > 4096:
            return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if (not isinstance(value, dict) or value.get('error') != 'integration_not_configured'
            or not isinstance(value.get('code'), str)):
        return None
    return {
        'ACCOUNTS_INTEGRATION_CSUPOR_MISSING': 'CONFIG_MISSING',
        'ACCOUNTS_INTEGRATION_CSUPOR_INVALID': 'CONFIG_INVALID',
        'ACCOUNTS_INTEGRATION_CSUPOR_CONFLICT': 'CONFIG_CONFLICT',
    }.get(value['code'])


class SharedWorkplaceExport(db.Model):
    __tablename__ = 'shared_workplace_exports'
    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    generation = db.Column(db.BigInteger, nullable=False)
    revision = db.Column(db.String(64), nullable=False)
    workplaces = db.Column(db.JSON, nullable=False)


def _catalogue_rows():
    # Locking reads bypass stale MySQL repeatable-read snapshots. The singleton
    # export row serialises generation allocation across Gunicorn workers.
    rows = db.session.execute(select(PlaceOfWork.id, LegalEntity.name, PlaceOfWork.address)
                              .join(LegalEntity, LegalEntity.id == PlaceOfWork.legal_entity_id)
                              .order_by(PlaceOfWork.id).with_for_update()).all()
    if (len(rows) > 1000 or any(type(ident) is not int or not 1 <= ident <= 2147483647
                               for ident, _name, _address in rows)):
        raise WorkplacePublishError('INVALID_SOURCE')
    addresses = {ident: _source_text(address, 255) for ident, _name, address in rows}
    bases = {ident: f'{_source_text(name, 120)} – {addresses[ident]}' for ident, name, _address in rows}
    counts = Counter(value.casefold() for value in bases.values())
    result, used = [], set()
    for ident, _name, address in rows:
        name = bases[ident]
        if len(name) > 120 or counts[name.casefold()] > 1 or name.casefold() in used:
            suffix = f' · {ident}'
            name = name[:120 - len(suffix)] + suffix
        # A deliberately matching original label may collide with a suffix.
        if name.casefold() in used:
            for counter in range(1, len(used) + 2):
                suffix = f' · {ident}.{counter}'
                name = bases[ident][:120 - len(suffix)] + suffix
                if name.casefold() not in used:
                    break
        used.add(name.casefold())
        result.append({'id': ident, 'name': name, 'address': addresses[ident]})
    return result


def _allocate_snapshot():
    for attempt in range(2):
        saved = db.session.execute(select(SharedWorkplaceExport).where(SharedWorkplaceExport.id == 1)
                                  .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        workplaces = _catalogue_rows()
        revision = sha256(json.dumps(workplaces, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()
        generation = saved.generation if saved else 0
        if saved is None or saved.revision != revision:
            generation += 1
            if saved is None:
                db.session.add(SharedWorkplaceExport(id=1, generation=generation,
                                                     revision=revision, workplaces=workplaces))
            else:
                changed = db.session.execute(update(SharedWorkplaceExport).where(
                    SharedWorkplaceExport.id == 1, SharedWorkplaceExport.generation == saved.generation,
                ).values(generation=generation, revision=revision, workplaces=workplaces)
                  .execution_options(synchronize_session=False))
                if changed.rowcount != 1:
                    db.session.rollback()
                    continue
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            if attempt == 0:
                continue
            raise
        return {'generation': generation, 'workplaces': workplaces}, revision
    raise WorkplacePublishError('SOURCE_CHANGED')


def publish_workplaces(*, force=False):
    if current_app.config.get('AUTH_MODE') != 'oidc' or not current_app.config.get('OIDC_CLIENT_ID'):
        return None
    state = current_app.extensions['shared_workplace_publisher']
    now = monotonic()
    if not force and now < state['next_attempt']:
        return state['last_success']
    if not state['lock'].acquire(blocking=False):
        return state['last_success']
    try:
        # A failed provider request does not roll back already-saved workplace
        # edits. Persist the monotonic snapshot before network access.
        state['next_attempt'] = now + 60
        payload, revision = _allocate_snapshot()
        with requests.post(current_app.config['OIDC_ISSUER'] + '/api/csupor/workplaces',
                           auth=(current_app.config['OIDC_CLIENT_ID'], current_app.config['OIDC_CLIENT_SECRET']),
                           data=json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
                           headers={'Accept': 'application/json', 'Content-Type': 'application/json'}, timeout=3,
                           allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                code = _upstream_error(response.status_code)
                if response.status_code == 503:
                    code = _configuration_error(response) or code
                raise WorkplacePublishError(code)
            raw = bytearray()
            for chunk in response.iter_content(16384):
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise WorkplacePublishError('INVALID_RESPONSE')
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise WorkplacePublishError('INVALID_RESPONSE') from None
            if (not isinstance(result, dict) or result.get('source') != 'csupor'
                    or result.get('revision') != revision):
                raise WorkplacePublishError('INVALID_RESPONSE')
        state['last_success'] = True
        state['next_attempt'] = monotonic() + 300
        return True
    except Exception as error:
        db.session.rollback()
        state['last_success'] = False
        if isinstance(error, WorkplacePublishError):
            code = error.code
        elif (isinstance(error, requests.Timeout)
              or (isinstance(error, requests.ConnectionError) and error.args
                  and isinstance(error.args[0], TransportTimeout))):
            code = 'TIMEOUT'
        elif isinstance(error, requests.RequestException):
            code = 'NETWORK'
        elif isinstance(error, SQLAlchemyError):
            code = 'SOURCE_DATABASE'
        else:
            code = 'INTERNAL'
        # Only allowlisted codes/messages: no exception text, HTTP bodies,
        # source fields, credentials or configured addresses enter the log.
        current_app.logger.warning('[CSUPOR_WORKPLACES_%s] %s A CSUPOR-adatok megmaradtak; később újrapróbáljuk.',
                                   code, _FAILURES[code])
        return False
    finally:
        state['lock'].release()


def init_shared_workplaces(app):
    app.extensions['shared_workplace_publisher'] = {'lock': Lock(), 'next_attempt': 0, 'last_success': None}

    @app.before_request
    def publish_directory_on_visit():
        if (request.endpoint in {None, 'static', 'login', 'register', 'logout', 'health',
                                'shared_auth.login', 'shared_auth.callback'}
                or app.config.get('AUTH_MODE') != 'oidc'):
            return None
        if current_user.is_authenticated:
            g.shared_workplaces_published = publish_workplaces()

