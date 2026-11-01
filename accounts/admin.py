"""Browser bootstrap and explicit central application access management."""
from functools import wraps
from secrets import compare_digest
import re
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash

from .importing import ImportValidationError, MAX_TRANSFER_BYTES, apply_import, decrypt_transfer, validate_payload
from .models import Account, AccessToken, AuthorizationCode, Client, Membership, db
from .security import current_account, login_account, require_csrf, throttle

admin = Blueprint('account_admin', __name__)
_SETUP_KEY = 'initial-admin'


class SetupValidationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _setup_failure(code, message, *, status=400, database_error=None):
    """Only fixed diagnostics may leave the bootstrap boundary, never raw errors."""
    diagnostic = 'ACCOUNTS_SETUP_' + code
    # MySQL's numeric error identifies permission/schema/constraint failures
    # without logging SQL, query parameters, accounts or credentials.
    number = getattr(getattr(database_error, 'orig', None), 'errno', None)
    if type(number) is int and 0 <= number <= 65535:
        current_app.logger.warning('[%s] database_errno=%d', diagnostic, number)
        message += f' Adatbázis-hibakód: {number}.'
    else:
        current_app.logger.warning('[%s]', diagnostic)
    return render_template('setup.html', error=(
        f'{message} Nem módosítottunk fiókot. Hibakód: {diagnostic}.'
    )), status


class AccountAdmin(db.Model):
    __tablename__ = 'account_admins'
    account_subject = db.Column(db.String(36), db.ForeignKey('accounts.subject'), primary_key=True)


class AccountSetupState(db.Model):
    __tablename__ = 'account_setup_state'
    key = db.Column(db.String(40), primary_key=True)
    completed = db.Column(db.Boolean, nullable=False, default=False)


def _state():
    value = db.session.get(AccountSetupState, _SETUP_KEY)
    if value is None:
        db.session.add(AccountSetupState(key=_SETUP_KEY, completed=False))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
        value = db.session.get(AccountSetupState, _SETUP_KEY)
        if value is None:
            raise RuntimeError('Account setup could not be prepared.')
    return value


def prepare_setup_state():
    _state()
    db.session.execute(update(AccountSetupState).where(AccountSetupState.key == _SETUP_KEY)
                       .values(completed=AccountSetupState.completed))
    if db.session.execute(select(AccountAdmin.account_subject).limit(1).with_for_update()).first():
        db.session.execute(update(AccountSetupState).where(AccountSetupState.key == _SETUP_KEY).values(completed=True))
    db.session.commit()


def setup_available():
    token = current_app.config.get('ACCOUNTS_SETUP_TOKEN', '')
    if not isinstance(token, str) or not 32 <= len(token) <= 1024:
        return False
    marker = db.session.get(AccountSetupState, _SETUP_KEY)
    return ((marker is None or not marker.completed)
            and db.session.execute(select(AccountAdmin.account_subject).limit(1)).first() is None)


def is_account_admin():
    account = current_account()
    return bool(account and db.session.get(AccountAdmin, account.subject))


@admin.app_context_processor
def admin_context():
    return {'is_account_admin': is_account_admin(), 'account_setup_available': setup_available()}


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_account() is None:
            if request.method == 'GET':
                return redirect(url_for('web.login', next=request.path))
            abort(401)
        if not is_account_admin():
            abort(403)
        if request.method != 'GET':
            require_csrf()
        return view(*args, **kwargs)
    return wrapped


def _client_values(client_id, *, existing=None):
    from . import allowed_url
    name = request.form.get('name', '').strip()
    uris = list(dict.fromkeys(line.strip() for line in request.form.get('redirect_uris', '').splitlines() if line.strip()))
    secret = request.form.get('client_secret', '')
    rotate = request.form.get('rotate_secret') == '1'
    active_raw = request.form.get('active', 'true' if existing is None else '')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', client_id or ''):
        raise ValueError('Az alkalmazásazonosító 1–80 betű, szám, pont, kötőjel vagy aláhúzás lehet.')
    if not 1 <= len(name) <= 120 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError('Adj meg legfeljebb 120 karakteres alkalmazásnevet.')
    if (not 1 <= len(uris) <= 10 or any(not allowed_url(uri, current_app.config['ACCOUNTS_ALLOW_HTTP'])
                                      or urlsplit(uri).query for uri in uris)):
        raise ValueError('Adj meg 1–10 teljes HTTPS-visszatérési címet, soronként egyet, lekérdezés és # rész nélkül.')
    if active_raw not in {'true', 'false'}:
        raise ValueError('Válassz érvényes alkalmazásállapotot.')
    if (existing is None or rotate) and not 32 <= len(secret) <= 1024:
        raise ValueError('Az alkalmazás kulcsa legalább 32, legfeljebb 1024 karakter legyen.')
    if existing is not None and secret and not rotate:
        raise ValueError('Új kulcs mentéséhez jelöld be a kulcscserét.')
    return dict(name=name, redirect_uris=uris, active=active_raw == 'true',
                allow_registration=request.form.get('allow_registration') == '1'), secret, rotate


@admin.route('/setup', methods=['GET', 'POST'])
def setup():
    if not setup_available():
        abort(404)
    if request.method == 'GET':
        return render_template('setup.html')
    require_csrf()
    # throttle commits its counters: keep every limit before the business
    # transaction, including the singleton claim and account/client inserts.
    identifier = request.form.get('admin_login', '').strip()
    allowed = throttle('setup-ip', request.remote_addr or 'unknown', limit=10, window=900)
    allowed = throttle('setup-owner', identifier.casefold()[:120], limit=10, window=900) and allowed
    if not allowed:
        return render_template('setup.html', error='Túl sok próbálkozás. Próbáld újra 15 perc múlva.'), 429
    supplied = request.form.get('setup_token', '')
    expected = current_app.config['ACCOUNTS_SETUP_TOKEN']
    if (not 32 <= len(supplied) <= 1024
            or not compare_digest(supplied.encode('utf-8'), expected.encode('utf-8'))):
        return _setup_failure('TOKEN', 'Az egyszeri beállítási kulcs nem egyezik a futó fiókkezelő '
                              'ACCOUNTS_SETUP_TOKEN értékével. Kulcscsere után várd meg az új '
                              'deployment befejezését, majd az új értéket másold ide.', status=403)
    try:
        from . import allowed_url
        uploaded = request.files.get('transfer_file')
        if uploaded is None:
            raise SetupValidationError('TRANSFER_MISSING',
                                       'Válaszd ki a CSUPOR fejlesztői felületéről letöltött titkosított exportfájlt.')
        data = decrypt_transfer(uploaded.stream.read(MAX_TRANSFER_BYTES + 1), request.form.get('transfer_key', ''))
        rows = validate_payload(data, current_app.config['ACCOUNTS_ISSUER'])
        matches = [row for row in rows if identifier.casefold() in {row['username'].casefold(), row['email'].casefold()}]
        password = request.form.get('admin_password', '')
        if (len(matches) != 1 or not 1 <= len(password) <= 1024
                or not check_password_hash(matches[0]['password_hash'], password)):
            raise SetupValidationError('OWNER_LOGIN', 'A belépési név vagy jelszó nem egyezik az exportban '
                                       'szereplő fiókéval. A meglévő CSUPOR-fiókod adatait add meg; '
                                       'az export után módosított jelszóhoz új export szükséges.')
        owner_subject = matches[0]['subject']
        callback = request.form.get('csupor_redirect_uri', '').strip()
        secret = request.form.get('csupor_client_secret', '')
        if (not allowed_url(callback, current_app.config['ACCOUNTS_ALLOW_HTTP'])
                or urlsplit(callback).path != '/auth/callback' or urlsplit(callback).query):
            raise SetupValidationError('CALLBACK', 'A CSUPOR-visszatérési cím teljes HTTPS-cím legyen, '
                                       '/auth/callback végződéssel, kérdőjel és # rész nélkül. '
                                       'Például: https://csupor.matedev.hu/auth/callback')
        if not 32 <= len(secret) <= 1024:
            raise SetupValidationError('CLIENT_SECRET', 'Az új CSUPOR-összekapcsolási kulcs 32–1024 karakter '
                                       'hosszú legyen. Használd a „Véletlen kulcs készítése” gombot, '
                                       'és mentsd el az értékét a későbbi OIDC_CLIENT_SECRET beállításhoz.')
        _state()
        claimed = db.session.execute(update(AccountSetupState).where(
            AccountSetupState.key == _SETUP_KEY, AccountSetupState.completed.is_(False),
        ).values(completed=True))
        if claimed.rowcount != 1:
            db.session.rollback()
            abort(404)
        if db.session.execute(select(AccountAdmin.account_subject).limit(1).with_for_update()).first():
            db.session.commit()
            abort(404)
        client = db.session.execute(select(Client).where(Client.client_id == 'csupor').with_for_update()).scalar_one_or_none()
        if client is None:
            client = Client(client_id='csupor', name='CSUPOR', redirect_uris=[callback], allow_registration=False, active=True)
            client.set_secret(secret)
            db.session.add(client)
            db.session.flush()
        elif not client.active or client.redirect_uris != [callback] or not client.check_client_secret(secret):
            raise SetupValidationError('CLIENT_CONFLICT', 'A fiókkezelőben már létezik CSUPOR-alkalmazás, '
                                       'amelynek állapota, visszatérési címe vagy összekapcsolási kulcsa eltér. '
                                       'A meglévő alkalmazás beállításaival folytasd az átvezetést.')
        apply_import(data, 'csupor', current_app.config['ACCOUNTS_ISSUER'])
        owner = db.session.get(Account, owner_subject)
        if owner is None or not owner.active:
            raise SetupValidationError('OWNER_INACTIVE', 'A kiválasztott fiók a központi fiókkezelőben '
                                       'nem aktív, ezért nem kaphat adminisztrátori hozzáférést. '
                                       'Aktív, az exportban is szereplő saját fiók szükséges.')
        db.session.add(AccountAdmin(account_subject=owner_subject))
        db.session.commit()
    except (SetupValidationError, ImportValidationError) as exc:
        db.session.rollback()
        return _setup_failure(exc.code, exc.message)
    except SQLAlchemyError as exc:
        db.session.rollback()
        return _setup_failure('DATABASE', 'A fiókadatbázis nem tudta elmenteni az átvezetést. '
                              'Ellenőrizni kell az adatbázis elérhetőségét, írási jogosultságait '
                              'és az esetleges adatütközést. A kulcsok cseréje ezt a hibát nem oldja meg.',
                              database_error=exc)
    except (ValueError, TypeError, KeyError):
        db.session.rollback()
        return _setup_failure('VALIDATION', 'Az átvezetés egyik adata nem dolgozható fel. '
                              'Készíts új exportot a CSUPOR aktuális verziójával; '
                              'ha megismétlődik, a hibakód alapján további vizsgálat szükséges.')
    login_account(owner)
    flash('Az átvezetés elkészült. A korábbi fiókok és jelszavak megmaradtak; a központi fiókkezelő készen áll.', 'success')
    return redirect(url_for('account_admin.index'))


@admin.get('/admin')
@admin_required
def index():
    return render_template('admin_index.html', clients=Client.query.order_by(Client.name).all(), account_count=Account.query.count())


@admin.route('/admin/clients/new', methods=['GET', 'POST'])
@admin_required
def client_new():
    error = None
    if request.method == 'POST':
        client_id = request.form.get('client_id', '').strip()
        try:
            values, secret, _rotate = _client_values(client_id)
            if db.session.get(Client, client_id):
                raise ValueError('Ez az alkalmazásazonosító már létezik. A meglévő beállítások nem változtak.')
            client = Client(client_id=client_id, **values)
            client.set_secret(secret)
            db.session.add(client)
            db.session.commit()
            flash('Az alkalmazást létrehoztuk. A választott kulcsot add meg az alkalmazás környezeti beállításánál is.', 'success')
            return redirect(url_for('account_admin.client_edit', client_id=client_id))
        except (ValueError, SQLAlchemyError) as exc:
            db.session.rollback()
            error = str(exc) if isinstance(exc, ValueError) else 'A mentés nem sikerült. A korábbi adatok megmaradtak.'
    return render_template('admin_client.html', client=None, error=error,
                           suggested_id='munkavedelem' if request.args.get('client_id') == 'munkavedelem' else '', env_values=[])


def _env_values(client):
    prefix = 'SAFETY_' if client.client_id == 'munkavedelem' else ''
    if client.client_id not in {'csupor', 'munkavedelem'}:
        return []
    values = [(prefix + 'OIDC_ISSUER', current_app.config['ACCOUNTS_ISSUER']),
              (prefix + 'OIDC_CLIENT_ID', client.client_id),
              (prefix + 'OIDC_REDIRECT_URI', client.redirect_uris[0])]
    if client.client_id == 'csupor':
        values.insert(0, ('AUTH_MODE', 'oidc'))
    return values


@admin.route('/admin/clients/<client_id>/edit', methods=['GET', 'POST'])
@admin_required
def client_edit(client_id):
    client = db.get_or_404(Client, client_id)
    error = None
    if request.method == 'POST':
        try:
            values, secret, rotate = _client_values(client_id, existing=client)
            invalidate = rotate or values['redirect_uris'] != client.redirect_uris or client.active and not values['active']
            for key, value in values.items():
                setattr(client, key, value)
            if rotate:
                client.set_secret(secret)
            if invalidate:
                db.session.execute(db.delete(AccessToken).where(AccessToken.client_id == client_id))
                db.session.execute(db.delete(AuthorizationCode).where(AuthorizationCode.client_id == client_id))
            db.session.commit()
            flash('Az alkalmazás beállításait mentettük.', 'success')
            return redirect(url_for('account_admin.client_edit', client_id=client_id))
        except (ValueError, SQLAlchemyError) as exc:
            db.session.rollback()
            error = str(exc) if isinstance(exc, ValueError) else 'A mentés nem sikerült. A korábbi adatok megmaradtak.'
    return render_template('admin_client.html', client=client, error=error, suggested_id='', env_values=_env_values(client))


@admin.get('/admin/accounts')
@admin_required
def accounts():
    return render_template('admin_accounts.html', accounts=Account.query.order_by(Account.username).all(),
                           clients=Client.query.order_by(Client.name).all(),
                           memberships={(m.account_subject, m.client_id) for m in Membership.query.all()})


@admin.post('/admin/accounts/<subject>/access')
@admin_required
def account_access(subject):
    account = db.get_or_404(Account, subject)
    chosen = set(request.form.getlist('client_ids'))
    clients = {client.client_id: client for client in Client.query.all()}
    if not chosen <= set(clients):
        abort(400, description='Ismeretlen alkalmazás nem adható a fiókhoz.')
    # Existing grants to inactive applications can be retained, but new grants
    # require both the account and the application to be active.
    existing = {m.client_id for m in Membership.query.filter_by(account_subject=subject).all()}
    if chosen - existing and (not account.active or any(not clients[c].active for c in chosen - existing)):
        abort(400, description='Új hozzáféréshez aktív fiók és aktív alkalmazás szükséges.')
    for client_id in existing - chosen:
        db.session.execute(db.delete(Membership).where(Membership.account_subject == subject, Membership.client_id == client_id))
        db.session.execute(db.delete(AccessToken).where(AccessToken.account_subject == subject, AccessToken.client_id == client_id))
        db.session.execute(db.delete(AuthorizationCode).where(AuthorizationCode.account_subject == subject, AuthorizationCode.client_id == client_id))
    for client_id in chosen - existing:
        db.session.add(Membership(account_subject=subject, client_id=client_id))
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        abort(409, description='A hozzáférések közben változtak. Frissítsd az oldalt és próbáld újra.')
    flash('A fiók alkalmazás-hozzáféréseit mentettük.', 'success')
    return redirect(url_for('account_admin.accounts'))
