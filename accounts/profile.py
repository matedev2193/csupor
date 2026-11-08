"""Personal profile editing and narrowly scoped CSUPOR migration/cache API.

No HR fields are added to OIDC claims. Bearer access is limited to its owner;
the separately authenticated CSUPOR service can read explicit member batches.
Other connected applications cannot access either API.
"""
from datetime import date
from secrets import token_hex
import unicodedata
from urllib.parse import urlsplit

from authlib.integrations.flask_oauth2 import current_token
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .admin import AccountAdmin, admin_required
from .models import Account, AccessToken, Client, Membership, db
from .profile_models import AccountProfile
from .security import current_account, require_csrf, throttle
from .views import authenticated


profile = Blueprint('account_profile', __name__)
FIELDS = {
    'full_name': ('Teljes név', 120),
    'name_at_birth': ('Születési név', 120),
    'date_of_birth': ('Születési idő', 10),
    'place_of_birth': ('Születési hely', 120),
    'gender': ('Nem', 6),
    'mothers_maiden_name': ('Anyja születési neve', 120),
    'citizenships': ('Állampolgárság(ok)', 255),
    'social_security_number': ('TAJ-szám', 9),
    'tax_number': ('Adóazonosító jel', 10),
    'education_number': ('Oktatási azonosító', 11),
    'teacher_id_card_number': ('Pedagógusigazolvány száma', 64),
    'permanent_residence': ('Állandó lakcím', 255),
    'temporary_address': ('Tartózkodási hely', 255),
    'phone_number': ('Telefonszám', 40),
    'bank_account_number': ('Bankszámlaszám', 64),
    'marital_status': ('Családi állapot', 32),
    'disability': ('Fogyatékosság, tartós betegség', 255),
}
GENDERS = [('male', 'Férfi'), ('female', 'Nő'), ('other', 'Egyéb')]
MARITAL_STATUSES = [('single', 'Nőtlen / hajadon'), ('married', 'Házas'),
                   ('divorced', 'Elvált'), ('widowed', 'Özvegy'),
                   ('civil partnership', 'Bejegyzett élettársi kapcsolat')]


def validate_fields(values, *, complete=False):
    """Validate the complete update before mutating any persisted field."""
    if not isinstance(values, dict) or set(values) - set(FIELDS):
        raise ValueError('A profil mezői nem megfelelőek. Frissítsd az oldalt.')
    result = {}
    for name in (FIELDS if complete else values):
        value = values.get(name)
        label, maximum = FIELDS[name]
        if value is not None and not isinstance(value, str):
            raise ValueError(f'{label}: érvényes szöveges értéket adj meg.')
        value = value.strip() if isinstance(value, str) else None
        value = value or None
        if value is not None:
            if len(value) > maximum:
                raise ValueError(f'{label}: legfeljebb {maximum} karakter adható meg.')
            if any(unicodedata.category(character).startswith('C')
                   and not (name in {'permanent_residence', 'temporary_address'} and character in '\r\n')
                   for character in value):
                raise ValueError(f'{label}: az érték nem megengedett karaktert tartalmaz.')
            if name in {'social_security_number', 'tax_number', 'education_number'}:
                if len(value) != maximum or not value.isascii() or not value.isdigit():
                    raise ValueError(f'{label}: pontosan {maximum} számjegyet adj meg.')
            if name == 'date_of_birth':
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise ValueError('Születési idő: érvényes dátumot adj meg.') from None
            if name == 'gender' and value not in dict(GENDERS):
                raise ValueError('Válassz érvényes nemet.')
            if name == 'marital_status' and value not in dict(MARITAL_STATUSES):
                raise ValueError('Válassz érvényes családi állapotot.')
        result[name] = value
    return result


def csupor_profile_url():
    """Use only an active, administrator-registered callback origin."""
    from . import allowed_url
    client = db.session.get(Client, 'csupor')
    if client and client.active:
        for callback in client.redirect_uris or []:
            if allowed_url(callback, current_app.config.get('ACCOUNTS_ALLOW_HTTP', False)):
                parsed = urlsplit(callback)
                return f'{parsed.scheme}://{parsed.netloc}/profile'
    return None


def _needs_import(subject, saved):
    return (db.session.get(Membership, (subject, 'csupor')) is not None
            and (saved is None or not saved.legacy_imported))


def _save_fields(subject, values, revision):
    # The owner lock serialises first inserts on MySQL; the conditional update
    # and unique key additionally protect SQLite and stale browser forms.
    actor_subject = session.get('account_subject')
    owners = db.session.execute(select(Account).where(Account.subject.in_({actor_subject, subject}))
                                .order_by(Account.subject).with_for_update()
                                .execution_options(populate_existing=True)).scalars().all()
    actor = next((owner for owner in owners if owner.subject == actor_subject), None)
    if actor is None or not actor.active or actor.version != session.get('account_version'):
        db.session.rollback()
        abort(401)
    if actor_subject != subject and db.session.execute(select(AccountAdmin.account_subject).where(
            AccountAdmin.account_subject == actor_subject).with_for_update()).scalar_one_or_none() is None:
        db.session.rollback()
        abort(403)
    saved = db.session.execute(select(AccountProfile).where(AccountProfile.subject == subject)
                               .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if (saved.revision if saved else '') != revision:
        db.session.rollback()
        return False
    if saved is None:
        db.session.add(AccountProfile(subject=subject, fields=values, revision=token_hex(16)))
    else:
        changed = db.session.execute(update(AccountProfile).where(
            AccountProfile.subject == subject, AccountProfile.revision == revision,
        ).values(fields=values, revision=token_hex(16)).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.session.rollback()
            return False
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False
    return True


def _editor(owner, *, manager=False):
    from .profile_photos import browser_template_context
    saved = db.session.get(AccountProfile, owner.subject)
    pending = _needs_import(owner.subject, saved)
    values = dict(saved.fields) if saved else {}
    error, status = None, 200
    if request.method == 'POST':
        require_csrf()
        if pending:
            error = 'A korábbi profiladatok betöltése még nem történt meg. Előbb nyisd meg a CSUPOR profiloldalát.'
            status = 409
        else:
            values = {name: request.form.get(name, '') for name in FIELDS}
            try:
                validated = validate_fields(values, complete=True)
            except ValueError as failure:
                error, status = str(failure), 400
            else:
                if not _save_fields(owner.subject, validated, request.form.get('profile_version', '')):
                    error = 'A profil időközben megváltozott. Frissítsd az oldalt, majd ellenőrizd az adatokat.'
                    status = 409
                else:
                    flash('A profil adatait elmentettük.', 'success')
                    return redirect(url_for('account_profile.admin_edit', subject=owner.subject)
                                    if manager else url_for('account_profile.edit'))
    photo_context = browser_template_context(owner.subject) if not manager else {}
    displayed_revision = saved.revision if saved else ''
    if status == 409 and request.method == 'POST':
        # Keep the stale form stale until an actual refresh. Merely pressing
        # Save twice must not silently overwrite a newer edit.
        displayed_revision = request.form.get('profile_version', '')
    return render_template('profile.html', owner=owner, manager_mode=manager, profile_values=values,
                           profile_version=displayed_revision, pending_import=pending,
                           csupor_url=csupor_profile_url(), fields=FIELDS, genders=GENDERS,
                           marital_statuses=MARITAL_STATUSES, error=error, **photo_context), status


@profile.route('/profile', methods=['GET', 'POST'])
@authenticated
def edit():
    return _editor(current_account())


@profile.route('/admin/accounts/<subject>/profile', methods=['GET', 'POST'])
@admin_required
def admin_edit(subject):
    owner = db.session.get(Account, subject)
    if owner is None:
        abort(404)
    return _editor(owner, manager=True)


def profile_payload(subject):
    from .profile_photos import photo_version
    saved = db.session.get(AccountProfile, subject)
    return {'sub': subject, 'version': saved.revision if saved else '',
            'fields': dict(saved.fields) if saved else {},
            'legacy_imported': bool(saved and saved.legacy_imported),
            'photo_version': photo_version(subject)}


def require_csupor_profile_token():
    if current_token.client_id != 'csupor' or 'profile' not in current_token.scope.split():
        abort(403)
    return current_token.account_subject


def import_fields(subject, fields):
    owner = db.session.execute(select(Account).where(Account.subject == subject).with_for_update()
                               .execution_options(populate_existing=True)).scalar_one()
    if not owner.active or owner.version != current_token.version or current_token.is_expired():
        db.session.rollback()
        abort(401)
    permitted = db.session.execute(select(Membership.account_subject).join(
        Client, Client.client_id == Membership.client_id,
    ).where(Membership.account_subject == subject, Membership.client_id == 'csupor',
            Client.active.is_(True)).with_for_update()).scalar_one_or_none()
    token_exists = db.session.execute(select(AccessToken.token_hash).where(
        AccessToken.token_hash == current_token.token_hash).with_for_update()).scalar_one_or_none()
    if permitted is None or token_exists is None:
        db.session.rollback()
        abort(403)
    saved = db.session.execute(select(AccountProfile).where(AccountProfile.subject == subject)
                               .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if saved and saved.legacy_imported:
        return
    if saved is None:
        db.session.add(AccountProfile(subject=subject, fields=fields, legacy_imported=True,
                                      revision=token_hex(16)))
    else:
        # Existing centrally edited values, including deliberate clears, win.
        merged = {**fields, **saved.fields}
        changed = db.session.execute(update(AccountProfile).where(
            AccountProfile.subject == subject, AccountProfile.revision == saved.revision,
            AccountProfile.legacy_imported.is_(False),
        ).values(fields=merged, revision=token_hex(16), legacy_imported=True)
          .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.session.rollback()
            abort(409, description='A profil időközben megváltozott. Próbáld újra.')
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # A concurrent first import may win. Never replace its committed data.
        winner = db.session.get(AccountProfile, subject)
        if winner is None or not winner.legacy_imported:
            abort(409, description='A profil időközben megváltozott. Próbáld újra.')


def register_profile_api(bp, protect):
    @bp.get('/api/csupor/profile')
    @protect('openid')
    def get_profile():
        return jsonify(profile_payload(require_csupor_profile_token()))

    @bp.post('/api/csupor/profile/import')
    @protect('openid')
    def import_profile():
        subject = require_csupor_profile_token()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {'fields'}:
            return jsonify(error='Érvénytelen profiladatok.'), 400
        try:
            fields = validate_fields(payload['fields'])
        except ValueError as failure:
            return jsonify(error=str(failure)), 400
        import_fields(subject, fields)
        return jsonify(profile_payload(subject))

    @bp.post('/api/csupor/profiles/read')
    def read_csupor_profiles():
        # This server-only read keeps CSUPOR's existing employee records current
        # after central admin edits. It cannot enumerate or modify identities.
        auth = request.authorization
        if not throttle('profile-service-ip', request.remote_addr or '', limit=600, window=60):
            return jsonify(error='temporarily_unavailable'), 429
        client = db.session.get(Client, 'csupor')
        if (auth is None or auth.type != 'basic' or auth.username != 'csupor'
                or client is None or not client.active or not client.check_client_secret(auth.password)):
            response = jsonify(error='invalid_client')
            response.status_code = 401
            response.headers['WWW-Authenticate'] = 'Basic realm="CSUPOR profile sync"'
            return response
        payload = request.get_json(silent=True)
        subjects = payload.get('subjects') if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or set(payload) != {'subjects'}
                or not isinstance(subjects, list) or not 1 <= len(subjects) <= 100
                or any(not isinstance(subject, str) or not 1 <= len(subject) <= 36 for subject in subjects)
                or len(set(subjects)) != len(subjects)):
            return jsonify(error='invalid_request'), 400
        permitted = db.session.execute(select(Account.subject).join(
            Membership, Membership.account_subject == Account.subject,
        ).join(AccountProfile, AccountProfile.subject == Account.subject).where(
            Account.subject.in_(subjects), Account.active.is_(True),
            Membership.client_id == 'csupor', AccountProfile.legacy_imported.is_(True),
        )).scalars().all()
        return jsonify(profiles=[profile_payload(subject) for subject in permitted])
