"""Shared-account OIDC client with independent, local training permissions."""
import base64
from datetime import datetime, timedelta, timezone
from functools import wraps
from hashlib import sha256
import logging
import os
import re
from secrets import token_urlsafe
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from flask import Blueprint, abort, current_app, g, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from .models import LoginSession, Member, db, now


auth = Blueprint('auth', __name__)
_HANDLE = re.compile(r'[A-Za-z0-9_-]{43}\Z')


def _cipher():
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode()
    return Fernet(base64.urlsafe_b64encode(sha256(b'safety:oidc:token:v1:' + secret).digest()))


def _client():
    client = current_app.extensions['safety_oauth'].create_client('accounts')
    metadata = client.load_server_metadata()
    issuer = current_app.config['SAFETY_OIDC_ISSUER']
    if metadata.get('issuer') != issuer:
        raise ValueError('Unexpected account issuer')
    for name, path in {
        'authorization_endpoint': '/authorize', 'token_endpoint': '/token',
        'userinfo_endpoint': '/userinfo', 'jwks_uri': '/jwks',
    }.items():
        if metadata.get(name) != issuer + path:
            raise ValueError('Unexpected account endpoint')
    if 'RS256' not in metadata.get('id_token_signing_alg_values_supported', []):
        raise ValueError('Unsupported signing algorithm')
    metadata['id_token_signing_alg_values_supported'] = ['RS256']
    return client


def _handle_hash():
    handle = session.get('safety_login', '')
    if isinstance(handle, str) and _HANDLE.fullmatch(handle):
        return sha256(handle.encode()).hexdigest()
    return None


def load_member():
    """Check live account and application membership for each protected request."""
    g.member = None
    if request.endpoint in {'static', 'health', 'healthz', 'auth.login', 'auth.callback'}:
        return
    key = _handle_hash()
    if not key:
        return
    saved = db.session.get(LoginSession, key)
    if saved is None or saved.expires_at <= now():
        session.pop('safety_login', None)
        return
    member = saved.member
    if not member or not member.active or member.issuer != current_app.config['SAFETY_OIDC_ISSUER']:
        return
    try:
        token = _cipher().decrypt(saved.access_token.encode()).decode()
        claims = _client().userinfo(token={'access_token': token, 'token_type': 'Bearer'}, timeout=5)
        if claims.get('sub') != member.subject:
            return
    except Exception:
        # No personal data or tokens in logs; an outage never grants access.
        current_app.logger.warning('Shared account session could not be validated.')
        return
    g.member = member


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get('member') is None:
            if request.method != 'GET' or request.is_json:
                abort(401, description='A munkamenet lejárt. Jelentkezz be újra.')
            return redirect(url_for('auth.login'))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @login_required
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.member.role != 'admin':
            abort(403, description='Ehhez a művelethez adminisztrátori jogosultság szükséges.')
        return view(*args, **kwargs)
    return wrapped


def _mapped_member(claims):
    subject = claims.get('sub')
    username = claims.get('preferred_username')
    email = claims.get('email')
    if (not isinstance(subject, str) or not 1 <= len(subject) <= 255
            or not isinstance(username, str) or not 1 <= len(username.strip()) <= 120
            or not isinstance(email, str) or not 1 <= len(email.strip()) <= 254):
        abort(403)
    issuer = current_app.config['SAFETY_OIDC_ISSUER']
    member = Member.query.filter_by(issuer=issuer, subject=subject).first()
    if member is None:
        # Never adopt an existing member or their role by a matching email/name.
        member = Member(issuer=issuer, subject=subject, name=username.strip(),
                        email=email.strip(), role='employee', active=True)
        db.session.add(member)
        db.session.flush()
    elif not member.active:
        abort(403, description='A hozzáférésed ehhez az alkalmazáshoz szünetel.')
    # A manually entered display name is retained. Only the same verified
    # stable identity may refresh the contact email from the account service.
    member.email = email.strip()
    return member


@auth.get('/auth/login')
def login():
    try:
        return _client().authorize_redirect(current_app.config['SAFETY_OIDC_REDIRECT_URI'])
    except Exception:
        current_app.logger.warning('Shared account sign-in could not be started.')
        return render_template('error.html', message='A közös fiókrendszer jelenleg nem érhető el. Próbáld újra később.'), 503


@auth.get('/auth/callback')
def callback():
    try:
        client = _client()
        token = client.authorize_access_token(claims_options={
            'iss': {'essential': True, 'value': current_app.config['SAFETY_OIDC_ISSUER']},
            'sub': {'essential': True}, 'exp': {'essential': True},
            'aud': {'essential': True, 'value': current_app.config['SAFETY_OIDC_CLIENT_ID']},
        })
        claims = token.get('userinfo')
        if (not token.get('id_token') or not claims or not token.get('access_token')
                or str(token.get('token_type', '')).lower() != 'bearer'):
            raise ValueError('Incomplete account response')
        live_claims = client.userinfo(token=token, timeout=5)
        if live_claims.get('sub') != claims.get('sub'):
            raise ValueError('Account subject mismatch')
        member = _mapped_member(live_claims)
        seconds = min(int(token.get('expires_in', 0)),
                      current_app.config['SAFETY_SESSION_TTL'],
                      int(claims['exp'] - datetime.now(timezone.utc).timestamp()))
        if seconds <= 0:
            raise ValueError('Expired account response')
        old_key = _handle_hash()
        if old_key:
            db.session.execute(db.delete(LoginSession).where(LoginSession.id_hash == old_key))
        db.session.execute(db.delete(LoginSession).where(LoginSession.expires_at <= now()))
        handle = token_urlsafe(32)
        db.session.add(LoginSession(id_hash=sha256(handle.encode()).hexdigest(), member_id=member.id,
                                    access_token=_cipher().encrypt(token['access_token'].encode()).decode(),
                                    expires_at=now() + timedelta(seconds=seconds)))
        db.session.commit()
        # Rotate the browser session and its CSRF token after authentication.
        session.clear()
        session['safety_login'] = handle
        return redirect(url_for('web.index'))
    except HTTPException:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        current_app.logger.warning('Shared account sign-in was rejected.')
        return render_template('error.html', message='A bejelentkezés nem sikerült. Indítsd újra a bejelentkezést.'), 400


@auth.post('/auth/logout')
def logout():
    key = _handle_hash()
    if key:
        db.session.execute(db.delete(LoginSession).where(LoginSession.id_hash == key))
        db.session.commit()
    session.clear()
    return redirect(current_app.config['SAFETY_OIDC_ISSUER'] + '/logout')


def init_auth(app):
    issuer = str(app.config.get('SAFETY_OIDC_ISSUER') or '').rstrip('/')
    callback_url = app.config.get('SAFETY_OIDC_REDIRECT_URI') or ''
    allow_http = app.config.get('SAFETY_ALLOW_HTTP', False)
    for value in (issuer, callback_url):
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme == 'https' or allow_http and parsed.scheme == 'http'
                     and parsed.hostname in {'localhost', '127.0.0.1', '::1'})
            if (not valid or not parsed.netloc or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or '\\' in value
                    or any(ord(c) < 33 or ord(c) == 127 for c in value)):
                raise ValueError()
        except (ValueError, TypeError):
            raise RuntimeError('Set HTTPS SAFETY_OIDC_ISSUER and SAFETY_OIDC_REDIRECT_URI; HTTP is restricted to loopback development.') from None
    if len(issuer) > 255 or urlsplit(issuer).path or urlsplit(callback_url).path != '/auth/callback':
        raise RuntimeError('The issuer must be an origin and the callback must use /auth/callback.')
    if not app.config.get('SAFETY_OIDC_CLIENT_ID') or not app.config.get('SAFETY_OIDC_CLIENT_SECRET'):
        raise RuntimeError('SAFETY_OIDC_CLIENT_ID and SAFETY_OIDC_CLIENT_SECRET are required.')
    if not isinstance(app.secret_key, str) or len(app.secret_key) < 32:
        raise RuntimeError('SAFETY_SECRET_KEY requires at least 32 random characters.')
    try:
        ttl = int(app.config.get('SAFETY_SESSION_TTL', 3600))
    except (TypeError, ValueError):
        raise RuntimeError('SAFETY_SESSION_TTL must be between 60 and 3600 seconds.') from None
    if not 60 <= ttl <= 3600:
        raise RuntimeError('SAFETY_SESSION_TTL must be between 60 and 3600 seconds.')
    app.config.update(SAFETY_OIDC_ISSUER=issuer, SAFETY_SESSION_TTL=ttl,
                      SESSION_COOKIE_NAME='micimacko_safety', SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_DOMAIN=None,
                      SESSION_COOKIE_SECURE=urlsplit(callback_url).scheme == 'https')
    if allow_http and urlsplit(issuer).scheme == 'http':
        os.environ['AUTHLIB_INSECURE_TRANSPORT'] = '1'
    logging.getLogger('authlib').setLevel(logging.WARNING)
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    oauth.register('accounts', client_id=app.config['SAFETY_OIDC_CLIENT_ID'],
                   client_secret=app.config['SAFETY_OIDC_CLIENT_SECRET'],
                   server_metadata_url=issuer + '/.well-known/openid-configuration',
                   client_kwargs={'scope': 'openid profile email', 'code_challenge_method': 'S256',
                                  'token_endpoint_auth_method': 'client_secret_basic', 'default_timeout': 5})
    app.extensions['safety_oauth'] = oauth
    app.register_blueprint(auth)
    # Resolve the symbol at request time so tests can replace only the external
    # authentication boundary without adding any runtime bypass configuration.
    app.before_request(lambda: load_member())
