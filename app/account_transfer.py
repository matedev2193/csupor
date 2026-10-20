"""Developer-only browser transfer of legacy accounts during explicit maintenance."""
import base64
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
import json
import os
from secrets import compare_digest, token_urlsafe
from urllib.parse import urlsplit

import click
from cryptography.fernet import Fernet
from flask import Blueprint, Response, current_app, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, login_required, login_user
from sqlalchemy import func, or_, select

from . import db
from .account_migration import _export_payload
from .models import User, UserPrivilege
from .password_reset import _now, _take_limit

account_transfer = Blueprint('account_transfer', __name__)
TRANSFER_PREFIX = b'MICIMACKO-ACCOUNTS-TRANSFER-1\n'
MAX_PLAINTEXT_BYTES = 20 * 1024 * 1024
_ALLOWED = frozenset({'login', 'logout', 'static', 'account_transfer.settings', 'account_transfer.health'})
_ERROR = 'Az átvitel nem sikerült. Ellenőrizd a megadott adatokat és a fiókok átállíthatóságát. A meglévő fiókadatok nem változtak.'


def migration_active():
    return bool(current_app.config.get('ACCOUNTS_MIGRATION_MODE')) and current_app.config.get('AUTH_MODE', 'local') == 'local'


def _csrf():
    return session.setdefault('account_transfer_csrf', token_urlsafe(32))


def _valid_csrf():
    expected, supplied = session.get('account_transfer_csrf'), request.form.get('csrf_token')
    return isinstance(expected, str) and isinstance(supplied, str) and compare_digest(expected.encode(), supplied.encode())


def _render(error=None, status=200, *, maintenance_login=False):
    # Secrets are never redisplayed after validation errors.
    count = None if maintenance_login else db.session.query(func.count(User.id)).scalar()
    return render_template('account_transfer.html', csrf_token=_csrf(), transfer_error=error,
                           migration_enabled=migration_active(), account_count=count,
                           maintenance_login=maintenance_login), status


def _valid_issuer(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value) or '\\' in value:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
        local_http = (current_app.config.get('AUTH_ALLOW_HTTP') and parsed.scheme == 'http'
                      and parsed.hostname in {'localhost', '127.0.0.1', '::1'})
        return ((parsed.scheme == 'https' or local_http) and bool(parsed.hostname) and not parsed.username
                and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment
                and (port is None or 1 <= port <= 65535))
    except ValueError:
        return False


def _valid_key(value):
    return isinstance(value, str) and 32 <= len(value) <= 1024 and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _proof_allowance(user_id):
    allowed = _take_limit('account-transfer-proof', str(user_id), 5, timedelta(minutes=15), _now())
    db.session.commit()  # A failed password/export must not refund its allowance.
    return allowed


def _maintenance_login():
    if request.method != 'POST':
        if current_user.is_authenticated and current_user.privilege == UserPrivilege.developer:
            return redirect(url_for('account_transfer.settings'))
        return _render(maintenance_login=True)
    if not _valid_csrf():
        return _render('A munkamenet lejárt. Frissítsd az oldalt.', 400, maintenance_login=True)
    identifier, password = request.form.get('login', ''), request.form.get('password', '')
    if not 1 <= len(identifier) <= 255 or not 1 <= len(password) <= 1024:
        return _render('A fejlesztői bejelentkezés nem sikerült.', 400, maintenance_login=True)
    try:
        now = _now()
        allowed = _take_limit('account-transfer-login-ip', request.remote_addr or '', 30, timedelta(minutes=15), now)
        allowed = _take_limit('account-transfer-login-user', identifier.strip().casefold(), 5, timedelta(minutes=15), now) and allowed
        db.session.commit()
        if not allowed:
            return _render('Túl sok próbálkozás. Próbáld újra 15 perc múlva.', 429, maintenance_login=True)
        candidates = db.session.execute(select(User).where(or_(
            func.lower(User.email) == identifier.strip().lower(), User.username == identifier.strip())).limit(2)).scalars().all()
        if len(candidates) != 1 or candidates[0].privilege != UserPrivilege.developer or not candidates[0].check_password(password):
            return _render('A fejlesztői bejelentkezés nem sikerült.', 400, maintenance_login=True)
        login_user(candidates[0])
        session['account_transfer_csrf'] = token_urlsafe(32)
        return redirect(url_for('account_transfer.settings'))
    except Exception:
        db.session.rollback()
        return Response('A bejelentkezés átmenetileg nem érhető el.', status=503, content_type='text/plain; charset=utf-8')


@account_transfer.get('/healthz')
def health():
    return {'status': 'ok'}


@account_transfer.route('/developer/shared-accounts', methods=['GET', 'POST'])
@login_required
def settings():
    if current_user.privilege != UserPrivilege.developer:
        return Response('Ez a felület kizárólag fejlesztői jogosultsággal érhető el.', status=403)
    if current_app.config.get('AUTH_MODE', 'local') != 'local':
        return Response('A helyi fiókexport az átállás után nem érhető el.', status=404)
    if request.method == 'GET':
        return _render()
    if not _valid_csrf():
        return _render('A munkamenet lejárt. Frissítsd az oldalt.', 400)
    if not migration_active():
        return _render('A letöltéshez előbb kapcsold be és telepítsd a fiókátállási karbantartási módot.', 409)
    issuer = request.form.get('issuer', '')
    transfer_key = request.form.get('transfer_key', '')
    password = request.form.get('current_password', '')
    if (not _valid_issuer(issuer) or not _valid_key(transfer_key) or not 1 <= len(password) <= 1024
            or request.form.get('maintenance_confirm') != '1'):
        return _render('Ellenőrizd a HTTPS fiókcímet, a legalább 32 karakteres átviteli kulcsot, a jelszót és a karbantartás megerősítését.', 400)
    try:
        actor_id = current_user.id
        if not _proof_allowance(actor_id):
            return _render('Túl sok próbálkozás. Próbáld újra 15 perc múlva.', 429)
        actor = db.session.execute(select(User).where(User.id == actor_id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if actor is None or actor.privilege != UserPrivilege.developer or not actor.check_password(password):
            db.session.rollback()
            return _render('A jelenlegi fejlesztői jelszó ellenőrzése nem sikerült.', 400)
        payload = _export_payload(issuer)
        plaintext = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise ValueError('Transfer size exceeded.')
        key = base64.urlsafe_b64encode(sha256(b'micimacko-account-transfer-v1\0' + transfer_key.encode('utf-8')).digest())
        encrypted = TRANSFER_PREFIX + Fernet(key).encrypt(plaintext)
        # Persist stable identity links only after the complete encrypted file is
        # ready. A download retry reuses those links and cannot retire passwords.
        db.session.commit()
        response = send_file(BytesIO(encrypted), mimetype='application/octet-stream', as_attachment=True,
                             download_name='csupor.accounts-transfer.enc', max_age=0, conditional=False)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
        return response
    except (click.ClickException, ValueError):
        db.session.rollback()
        return _render(_ERROR, 400)
    except Exception:
        db.session.rollback()
        return Response(_ERROR, status=503, content_type='text/plain; charset=utf-8')


def init_account_transfer(app):
    app.config['ACCOUNTS_MIGRATION_MODE'] = os.getenv('ACCOUNTS_MIGRATION_MODE', 'false').lower() in {'1', 'true', 'yes', 'on'}
    if app.config['ACCOUNTS_MIGRATION_MODE'] and os.getenv('AUTH_MODE', 'local').lower() == 'local':
        app.config['EMAIL_WORKER_ENABLED'] = False
    app.register_blueprint(account_transfer)
    @app.before_request
    def freeze_local_accounts():
        if request.endpoint == 'account_transfer.settings' and current_app.config.get('AUTH_MODE', 'local') != 'local':
            return Response('A helyi fiókexport az átállás után nem érhető el.', status=404)
        if not migration_active():
            return None
        if request.endpoint == 'login':
            return _maintenance_login()
        if request.endpoint not in _ALLOWED:
            response = Response('A CSUPOR a közös fiókok átállítása miatt átmenetileg karbantartás alatt áll. Kérjük, próbáld később. Fejlesztői belépés: /login',
                                status=503, content_type='text/plain; charset=utf-8')
            response.headers['Retry-After'] = '300'
            return response

    app.context_processor(lambda: {'accounts_migration_active': migration_active(),
                                  'accounts_transfer_available': current_app.config.get('AUTH_MODE', 'local') == 'local'})

    @app.after_request
    def private_transfer_response(response):
        if request.endpoint in {'account_transfer.settings', 'login'} or migration_active():
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
