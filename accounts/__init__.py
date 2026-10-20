"""Independent Micimackó-fiók application factory.

Run: gunicorn 'accounts:create_app()'. It never connects to the CSUPOR database.
"""
import hashlib
import logging
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, abort, request
from werkzeug.middleware.proxy_fix import ProxyFix
from joserfc.jwk import RSAKey

from .models import db


def _bool(value):
    return str(value).lower() in {'1', 'true', 'yes', 'on'}


def allowed_url(value, allow_http=False):
    try:
        parsed = urlsplit(value)
        return (bool(parsed.hostname) and not parsed.username and not parsed.password
                and not parsed.fragment and len(value) <= 2048
                and not any(ord(c) < 33 or ord(c) == 127 for c in value)
                and '\\' not in value
                and (parsed.scheme == 'https' or allow_http and parsed.scheme == 'http'
                     and parsed.hostname in {'localhost', '127.0.0.1', '::1'}))
    except (ValueError, TypeError):
        return False


def create_app(test_config=None):
    app = Flask(__name__)
    for key in (
        'ACCOUNTS_DATABASE_URL', 'ACCOUNTS_SECRET_KEY', 'ACCOUNTS_ISSUER',
        'ACCOUNTS_SIGNING_KEY_FILE', 'ACCOUNTS_SMTP_HOST', 'ACCOUNTS_SMTP_PORT',
        'ACCOUNTS_SMTP_USERNAME', 'ACCOUNTS_SMTP_PASSWORD', 'ACCOUNTS_SMTP_FROM',
    ):
        app.config[key] = os.getenv(key)
    app.config.update(
        ACCOUNTS_ALLOW_HTTP=_bool(os.getenv('ACCOUNTS_ALLOW_HTTP', 'false')),
        ACCOUNTS_SMTP_STARTTLS=_bool(os.getenv('ACCOUNTS_SMTP_STARTTLS', 'true')),
        ACCOUNTS_TOKEN_TTL=int(os.getenv('ACCOUNTS_TOKEN_TTL', '3600')),
        ACCOUNTS_PROXY_HOPS=int(os.getenv('ACCOUNTS_PROXY_HOPS', '0')),
        SESSION_COOKIE_NAME='micimacko_accounts', SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=16384, SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={'hide_parameters': True},
    )
    if test_config:
        app.config.update(test_config)
    app.config['SECRET_KEY'] = app.config.get('SECRET_KEY') or app.config['ACCOUNTS_SECRET_KEY']
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config.get('SQLALCHEMY_DATABASE_URI') or app.config['ACCOUNTS_DATABASE_URL']
    issuer = (app.config.get('ACCOUNTS_ISSUER') or '').rstrip('/')
    if (not allowed_url(issuer, app.config['ACCOUNTS_ALLOW_HTTP'])
            or urlsplit(issuer).path or urlsplit(issuer).query):
        raise RuntimeError('ACCOUNTS_ISSUER must be an HTTPS origin (HTTP loopback only with ACCOUNTS_ALLOW_HTTP=true).')
    app.config['ACCOUNTS_ISSUER'] = issuer
    if not isinstance(app.secret_key, str) or len(app.secret_key) < 32:
        raise RuntimeError('ACCOUNTS_SECRET_KEY must contain at least 32 random characters.')
    if not app.config['SQLALCHEMY_DATABASE_URI']:
        raise RuntimeError('Set ACCOUNTS_DATABASE_URL to a separate account database.')
    if not 60 <= app.config['ACCOUNTS_TOKEN_TTL'] <= 3600:
        raise RuntimeError('ACCOUNTS_TOKEN_TTL must be between 60 and 3600 seconds.')
    if app.config['ACCOUNTS_PROXY_HOPS'] not in {0, 1, 2}:
        raise RuntimeError('ACCOUNTS_PROXY_HOPS must be 0, 1 or 2 trusted reverse proxies.')
    if app.config['ACCOUNTS_PROXY_HOPS']:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=app.config['ACCOUNTS_PROXY_HOPS'], x_proto=app.config['ACCOUNTS_PROXY_HOPS'], x_host=0, x_port=0, x_prefix=0)
    app.config['SESSION_COOKIE_SECURE'] = urlsplit(issuer).scheme == 'https'
    app.config['OAUTH2_TOKEN_EXPIRES_IN'] = {'authorization_code': app.config['ACCOUNTS_TOKEN_TTL']}
    key_file = app.config.get('ACCOUNTS_SIGNING_KEY_FILE')
    if not key_file:
        raise RuntimeError('Set ACCOUNTS_SIGNING_KEY_FILE to a private RSA PEM file.')
    try:
        private_key = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2048:
            raise ValueError('RSA 2048-bit or larger required')
    except (OSError, ValueError, TypeError):
        raise RuntimeError('ACCOUNTS_SIGNING_KEY_FILE must contain a valid private RSA key of at least 2048 bits.') from None
    public_der = private_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    kid = hashlib.sha256(public_der).hexdigest()[:32]
    key = RSAKey.import_key(private_key, parameters={'kid': kid, 'use': 'sig', 'alg': 'RS256'})
    app.extensions['accounts_signing_key'] = key
    app.extensions['accounts_public_key'] = key.as_dict(private=False)
    app.extensions['accounts_key_id'] = kid
    # Authlib's DEBUG messages can include bearer tokens; never enable them here.
    logging.getLogger('authlib').setLevel(logging.WARNING)
    db.init_app(app)
    from .provider import init_provider
    init_provider(app)
    from .views import web
    app.register_blueprint(web)
    from .cli import register_cli
    register_cli(app)

    @app.get('/healthz')
    def health():
        # Safe for internal load-balancer checks: no identity or configuration.
        return {'status': 'ok'}

    @app.before_request
    def require_canonical_origin():
        if request.endpoint == 'health':
            return None
        origin = urlsplit(app.config['ACCOUNTS_ISSUER'])
        if request.scheme != origin.scheme or request.host.lower() != origin.netloc.lower():
            abort(400, description='Use the configured account-service origin.')

    @app.after_request
    def secure_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        if app.config['SESSION_COOKIE_SECURE']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    return app
