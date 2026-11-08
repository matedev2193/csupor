"""A small organisational directory shared explicitly with the safety app.

Only CSUPOR can publish. No employee, contract or other HR data is accepted.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import unicodedata

from authlib.integrations.flask_oauth2 import current_token
from flask import abort, current_app, jsonify, request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .models import Client, db
from .security import throttle
from .integration_config import require_integration_client


class WorkplaceCatalogue(db.Model):
    __tablename__ = 'shared_workplace_catalogue'
    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    generation = db.Column(db.BigInteger, nullable=False)
    revision = db.Column(db.String(64), nullable=False)
    published_at = db.Column(db.DateTime, nullable=False)
    workplaces = db.Column(db.JSON, nullable=False)


def validate_workplaces(value):
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError('Invalid workplaces')
    result, ids, names = [], set(), set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {'id', 'name', 'address'}:
            raise ValueError('Invalid workplace fields')
        ident = item['id']
        if type(ident) is not int or not 1 <= ident <= 2147483647 or ident in ids:
            raise ValueError('Invalid workplace identifier')
        for key, limit in (('name', 120), ('address', 255)):
            text = item[key]
            if (not isinstance(text, str) or not 1 <= len(text) <= limit or text != text.strip()
                    or any(unicodedata.category(c).startswith('C') for c in text)):
                raise ValueError('Invalid workplace text')
        if item['name'].casefold() in names:
            raise ValueError('Duplicate workplace name')
        ids.add(ident)
        names.add(item['name'].casefold())
        result.append(dict(item))
    return sorted(result, key=lambda row: row['id'])


def catalogue_revision(workplaces):
    return sha256(json.dumps(workplaces, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode()).hexdigest()


def catalogue_payload(saved):
    return {'source': 'csupor', 'revision': saved.revision,
            'published_at': saved.published_at.replace(tzinfo=timezone.utc).isoformat(),
            'workplaces': saved.workplaces}


def catalogue_response(payload):
    # UTF-8 keeps the maximum 1,000-row Unicode catalogue below the 2 MiB
    # transport limit; JSON's optional ASCII escaping can exceed that bound.
    return current_app.response_class(json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                                      mimetype='application/json')


def register_workplace_api(bp, protect):
    @bp.post('/api/csupor/workplaces')
    def publish_workplaces():
        client_id = require_integration_client('csupor')
        if not throttle('workplace-publish', request.remote_addr or '', limit=120, window=60):
            return jsonify(error='temporarily_unavailable'), 429
        auth = request.authorization
        client = db.session.get(Client, client_id)
        if (auth is None or auth.type != 'basic' or auth.username != client_id
                or client is None or not client.check_client_secret(auth.password)):
            response = jsonify(error='invalid_client')
            response.status_code = 401
            response.headers['WWW-Authenticate'] = 'Basic realm="CSUPOR workplaces"'
            return response
        observed_hash = client.secret_hash
        data = request.get_json(silent=True)
        try:
            if not isinstance(data, dict) or set(data) != {'generation', 'workplaces'}:
                raise ValueError
            generation = data['generation']
            if type(generation) is not int or not 1 <= generation <= 9223372036854775807:
                raise ValueError
            workplaces = validate_workplaces(data['workplaces'])
        except ValueError:
            return jsonify(error='invalid_request'), 400
        revision = catalogue_revision(workplaces)
        try:
            # Recheck the client under a current locking read after parsing.
            permitted = db.session.execute(select(Client.client_id).where(
                Client.client_id == client_id, Client.active.is_(True),
                Client.secret_hash == observed_hash,
            ).with_for_update()).scalar_one_or_none()
            if permitted is None:
                db.session.rollback()
                return jsonify(error='invalid_client'), 401
            saved = db.session.execute(select(WorkplaceCatalogue).where(
                WorkplaceCatalogue.id == 1,
            ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
            if saved and generation <= saved.generation:
                if generation == saved.generation and revision == saved.revision:
                    payload = catalogue_payload(saved)
                    db.session.rollback()
                    return catalogue_response(payload)
                db.session.rollback()
                return jsonify(error='stale_catalogue'), 409
            values = dict(generation=generation, revision=revision, workplaces=workplaces,
                          published_at=datetime.now(timezone.utc).replace(tzinfo=None))
            if saved is None:
                db.session.add(WorkplaceCatalogue(id=1, **values))
            else:
                changed = db.session.execute(update(WorkplaceCatalogue).where(
                    WorkplaceCatalogue.id == 1, WorkplaceCatalogue.generation == saved.generation,
                ).values(**values).execution_options(synchronize_session=False))
                if changed.rowcount != 1:
                    db.session.rollback()
                    return jsonify(error='stale_catalogue'), 409
            db.session.commit()
            return catalogue_response(catalogue_payload(db.session.get(WorkplaceCatalogue, 1)))
        except IntegrityError:
            db.session.rollback()
            winner = db.session.get(WorkplaceCatalogue, 1)
            if winner and winner.generation == generation and winner.revision == revision:
                return catalogue_response(catalogue_payload(winner))
            return jsonify(error='stale_catalogue'), 409
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.warning('The shared workplace catalogue could not be saved.')
            return jsonify(error='temporarily_unavailable'), 503

    @bp.get('/api/workplaces')
    @protect('openid')
    def read_workplaces():
        client_id = require_integration_client('safety')
        if (current_token.client_id != client_id
                or 'profile' not in current_token.scope.split()):
            abort(403)
        saved = db.session.get(WorkplaceCatalogue, 1)
        if saved is None:
            return jsonify(error='catalogue_not_published'), 503
        return catalogue_response(catalogue_payload(saved))
