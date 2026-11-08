"""Publish only workplace labels and addresses to the shared account directory."""
from collections import Counter
from hashlib import sha256
import json
from threading import Lock
from time import monotonic

from flask import current_app, g, request
from flask_login import current_user
import requests
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import db
from .models import LegalEntity, PlaceOfWork


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
    if len(rows) > 1000:
        raise ValueError('Workplace catalogue limit reached')
    addresses = {ident: ' '.join(address.split()) for ident, _name, address in rows}
    bases = {ident: f'{" ".join(name.split())} – {addresses[ident]}' for ident, name, _address in rows}
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
    raise ValueError('Workplace snapshot changed concurrently')


def publish_workplaces(*, force=False):
    if current_app.config.get('AUTH_MODE') != 'oidc' or current_app.config.get('OIDC_CLIENT_ID') != 'csupor':
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
                raise ValueError('Workplace publication unavailable')
            raw = bytearray()
            for chunk in response.iter_content(16384):
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('Workplace response too large')
            result = json.loads(raw)
            if (not isinstance(result, dict) or result.get('source') != 'csupor'
                    or result.get('revision') != revision):
                raise ValueError('Invalid workplace response')
        state['last_success'] = True
        state['next_attempt'] = monotonic() + 300
        return True
    except Exception:
        db.session.rollback()
        state['last_success'] = False
        current_app.logger.warning('A telephelylista központi frissítése nem sikerült; a CSUPOR-adatok megmaradtak.')
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

