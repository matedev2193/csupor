"""Account recovery SMTP settings, independent of CSUPOR notification mail."""
import base64
from hashlib import sha256
import re
from secrets import token_hex

from cryptography.fernet import Fernet, InvalidToken
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .admin import AccountAdmin, admin_required
from .models import Account, db
from .security import current_account, valid_email


mail_settings = Blueprint('account_mail', __name__)


class AccountMailSettings(db.Model):
    __tablename__ = 'account_mail_settings'
    key = db.Column(db.String(40), primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    host = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    sender = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(255), nullable=False, default='')
    password_ciphertext = db.Column(db.Text, nullable=True)
    security = db.Column(db.String(10), nullable=False, default='starttls')
    revision = db.Column(db.String(32), nullable=False)


def _cipher():
    # Domain separation prevents interchange with other encrypted data using
    # the same stable account-service secret. No additional operator key.
    secret = current_app.config.get('ACCOUNTS_SECRET_KEY') or current_app.secret_key
    key = sha256(b'micimacko-account-mail-v1\0' + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _environment_settings():
    config = current_app.config
    return {
        'host': config.get('ACCOUNTS_SMTP_HOST') or '',
        'port': config.get('ACCOUNTS_SMTP_PORT') or 587,
        'sender': config.get('ACCOUNTS_SMTP_FROM') or '',
        'username': config.get('ACCOUNTS_SMTP_USERNAME') or '',
        'password': config.get('ACCOUNTS_SMTP_PASSWORD') or '',
        'security': 'starttls' if config.get('ACCOUNTS_SMTP_STARTTLS', True) else 'plain',
    }


def effective_settings():
    """Private transport-only settings; never pass this dictionary to a page."""
    stored = db.session.get(AccountMailSettings, 'recovery')
    if stored is None or not stored.enabled:
        return _environment_settings()
    try:
        password = _cipher().decrypt(stored.password_ciphertext.encode()).decode() if stored.password_ciphertext else ''
    except (InvalidToken, ValueError, UnicodeError):
        raise RuntimeError('Account email credentials could not be opened.') from None
    return dict(host=stored.host, port=stored.port, sender=stored.sender,
                username=stored.username, password=password, security=stored.security)


def _display_values(stored):
    # Read public configuration without opening any saved SMTP password.
    if stored and stored.enabled:
        return dict(host=stored.host, port=stored.port, sender=stored.sender,
                    username=stored.username, security=stored.security)
    values = _environment_settings()
    return {key: value for key, value in values.items() if key != 'password'}


def _validated_values():
    values = {name: request.form.get(name, '').strip() for name in ('host', 'sender', 'username', 'security')}
    if (not re.fullmatch(r'[A-Za-z0-9.-]{1,255}', values['host'])
            or values['host'].startswith('.') or '..' in values['host']):
        raise ValueError('Adj meg érvényes SMTP-kiszolgálónevet, például smtp.example.hu.')
    try:
        port = int(request.form.get('port', ''))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError('A port 1 és 65535 közötti egész szám legyen.') from None
    if not valid_email(values['sender']):
        raise ValueError('Adj meg érvényes feladói e-mail-címet.')
    if len(values['username']) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in values['username']):
        raise ValueError('Az SMTP-felhasználónév legfeljebb 255 karakter lehet, vezérlőkarakter nélkül.')
    if values['security'] not in {'starttls', 'ssl'}:
        raise ValueError('Válassz STARTTLS vagy TLS kapcsolatot.')
    password = request.form.get('password', '')
    if len(password) > 4096 or '\x00' in password or '\r' in password or '\n' in password:
        raise ValueError('Az SMTP-jelszó legfeljebb 4096 karakter lehet, sortörés nélkül.')
    if password and request.form.get('clear_password') == '1':
        raise ValueError('Válassz új jelszó megadása vagy a mentett jelszó törlése között.')
    values['port'] = port
    return values, password


def _lock_admin():
    actor = current_account()
    if db.session.execute(select(Account.subject).join(
        AccountAdmin, AccountAdmin.account_subject == Account.subject,
    ).where(Account.subject == actor.subject, Account.active.is_(True),
            Account.version == session.get('account_version')).with_for_update()).scalar_one_or_none() is None:
        abort(403)


@mail_settings.route('/admin/email-settings', methods=['GET', 'POST'])
@admin_required
def settings():
    stored = db.session.get(AccountMailSettings, 'recovery')
    values = _display_values(stored)
    revision = stored.revision if stored else ''
    using_database = bool(stored and stored.enabled)
    password_saved = bool(stored.password_ciphertext if using_database
                          else current_app.config.get('ACCOUNTS_SMTP_PASSWORD'))
    error, status = None, 200
    if request.method == 'POST':
        action = request.form.get('action', 'save')
        expected_revision = request.form.get('revision', '')
        try:
            if action not in {'save', 'use_environment'}:
                raise ValueError('Válassz érvényes műveletet.')
            if action == 'save':
                values, password = _validated_values()
            _lock_admin()
            stored = db.session.execute(select(AccountMailSettings).where(
                AccountMailSettings.key == 'recovery',
            ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
            if (stored.revision if stored else '') != expected_revision:
                db.session.rollback()
                error, status = 'A beállítás időközben megváltozott. Frissítsd az oldalt, majd ellenőrizd az adatokat.', 409
            else:
                if action == 'use_environment':
                    if stored:
                        changed = db.session.execute(update(AccountMailSettings).where(
                            AccountMailSettings.key == 'recovery', AccountMailSettings.revision == expected_revision,
                        ).values(enabled=False, revision=token_hex(16)).execution_options(synchronize_session=False))
                        if changed.rowcount != 1:
                            raise IntegrityError('', None, Exception('Concurrent settings change'))
                else:
                    clear = request.form.get('clear_password') == '1'
                    if clear:
                        encrypted = None
                    elif password:
                        encrypted = _cipher().encrypt(password.encode()).decode()
                    elif stored and stored.enabled:
                        encrypted = stored.password_ciphertext
                    else:
                        inherited = _environment_settings()['password']
                        encrypted = _cipher().encrypt(inherited.encode()).decode() if inherited else None
                    changes = dict(**values, password_ciphertext=encrypted, enabled=True, revision=token_hex(16))
                    if stored is None:
                        db.session.add(AccountMailSettings(key='recovery', **changes))
                    else:
                        changed = db.session.execute(update(AccountMailSettings).where(
                            AccountMailSettings.key == 'recovery', AccountMailSettings.revision == expected_revision,
                        ).values(**changes).execution_options(synchronize_session=False))
                        if changed.rowcount != 1:
                            raise IntegrityError('', None, Exception('Concurrent settings change'))
                db.session.commit()
                flash('A fiókkezelő levélküldési beállításait mentettük.' if action == 'save'
                      else 'A fiókkezelő ismét a környezeti levélküldési beállításokat használja.', 'success')
                return redirect(url_for('account_mail.settings'))
        except ValueError as failure:
            db.session.rollback()
            error, status = str(failure), 400
        except IntegrityError:
            db.session.rollback()
            error, status = 'A beállítás időközben megváltozott. Frissítsd az oldalt, majd ellenőrizd az adatokat.', 409
        except SQLAlchemyError:
            db.session.rollback()
            error, status = 'A beállításokat nem sikerült menteni. A korábbi beállítások megmaradtak.', 503
        revision = expected_revision
    return render_template('mail_settings.html', values=values, revision=revision,
                           using_database=using_database, password_saved=password_saved,
                           error=error), status
