"""Browser session, CSRF and durable request limits for the account service."""
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlsplit

from flask import abort, current_app, g, request, session
from sqlalchemy.exc import IntegrityError

from .models import Account, RateLimit, db


def current_account():
    if hasattr(g, 'account'):
        return g.account
    subject = session.get('account_subject')
    account = db.session.get(Account, subject) if isinstance(subject, str) else None
    if (account is None or not account.active or account.version != session.get('account_version')
            or int(time.time()) - session.get('account_auth_time', 0) > 28800):
        g.account = None
    else:
        g.account = account
    return g.account


def login_account(account):
    reauth = session.get('reauth_request')
    session.clear()
    session['account_subject'] = account.subject
    session['account_version'] = account.version
    session['account_auth_time'] = int(time.time())
    if reauth:
        session['reauth_completed'] = reauth
    session.permanent = True
    g.account = account


def logout_account():
    session.clear()
    g.account = None


def csrf_token():
    return session.setdefault('csrf_token', secrets.token_urlsafe(32))


def require_csrf():
    expected, actual = session.get('csrf_token'), request.form.get('csrf_token', '')
    if not expected or not hmac.compare_digest(expected.encode(), actual.encode()):
        abort(400, description='A munkamenet lejárt. Frissítsd az oldalt, majd próbáld újra.')


def safe_next(value):
    if not isinstance(value, str) or not value.startswith('/') or value.startswith('//'):
        return '/account'
    if any(ord(c) < 32 for c in value) or '\\' in value:
        return '/account'
    parsed = urlsplit(value)
    return value if not parsed.netloc and not parsed.scheme else '/account'


def valid_email(value):
    return (isinstance(value, str) and len(value) <= 120
            and re.fullmatch(r'[^\s@.][^\s@]*@[^\s@.]+(?:\.[^\s@.]+)+', value) is not None
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and '..' not in value)


def throttle(scope, key, limit=10, window=900):
    """Count attempts atomically across workers; never store identifiers or IPs."""
    now = int(time.time())
    start = now - now % window
    digest = hmac.new(current_app.secret_key.encode(), f'{scope}:{key}:{start}'.encode(), hashlib.sha256).hexdigest()
    # Commit this security event before any business transaction starts.
    changed = db.session.execute(db.update(RateLimit).where(
        RateLimit.key_hash == digest,
    ).values(attempts=RateLimit.attempts + 1))
    if not changed.rowcount:
        try:
            db.session.add(RateLimit(key_hash=digest, window_start=start, attempts=1))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            db.session.execute(db.update(RateLimit).where(RateLimit.key_hash == digest).values(attempts=RateLimit.attempts + 1))
            db.session.commit()
    else:
        db.session.commit()
    return db.session.get(RateLimit, digest).attempts <= limit
