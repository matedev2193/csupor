"""Independent Micimackó-fiók application factory.

Run: gunicorn 'accounts:create_app()'. It never connects to the CSUPOR database.
"""
import atexit
import logging
import os
import ssl
import tempfile
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, Request, abort, request
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from werkzeug.middleware.proxy_fix import ProxyFix
from joserfc.jwk import RSAKey

from .models import db
from .errors import StartupError
from .database_tls import tls_connect_args


def _bool(value):
    return str(value).lower() in {'1', 'true', 'yes', 'on'}


def _integer_environment(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        raise StartupError(name, f'{name} must be an integer.') from None


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


@lru_cache(maxsize=4)
def _mysql_ca_file(pem):
    pem = pem.replace('\\n', '\n').strip() + '\n'
    try:
        if len(pem) > 65536 or '-----BEGIN CERTIFICATE-----' not in pem:
            raise ValueError('invalid certificate')
        ssl.create_default_context(cadata=pem)
    except (ssl.SSLError, ValueError):
        raise StartupError('ACCOUNTS_DATABASE_CA_CERT', 'ACCOUNTS_DATABASE_CA_CERT must contain a valid database CA certificate in PEM format.') from None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', prefix='accounts-db-ca-', suffix='.pem', delete=False) as certificate:
            certificate.write(pem)
    except (OSError, UnicodeError):
        raise StartupError('DATABASE_CA_FILE', 'The database CA certificate file could not be written.') from None
    path = Path(certificate.name)
    atexit.register(path.unlink, missing_ok=True)
    return str(path)


def _require_mysql_tls(connection, record):
    cursor = connection.cursor()
    try:
        cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row or len(row) < 2 or not row[1]:
        connection.close()
        raise StartupError('DATABASE_TLS', 'The account database requires an encrypted TLS connection.')


def _configure_database(app):
    value = app.config['SQLALCHEMY_DATABASE_URI']
    try:
        url = make_url(value)
        if url.drivername == 'mysql':
            url = url.set(drivername='mysql+mysqlconnector')
        if url.drivername in {'sqlite', 'sqlite+pysqlite'}:
            return
        if url.drivername not in {'mysql+mysqlconnector', 'mariadb+mysqlconnector'}:
            raise ValueError('Unsupported database driver')
        if not url.host or not url.database:
            raise ValueError('Database host and name are required')
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError('Invalid database port')
        mode = url.query.get('ssl-mode')
        if mode is not None and mode != 'REQUIRED':
            raise ValueError('Unsupported SSL mode')
        url = url.difference_update_query(['ssl-mode'])
    except (ValueError, TypeError, ArgumentError):
        raise StartupError('ACCOUNTS_DATABASE_URL', 'Check ACCOUNTS_DATABASE_URL; mysql:// and mysql+mysqlconnector:// with optional ssl-mode=REQUIRED are supported.') from None
    options = dict(app.config.get('SQLALCHEMY_ENGINE_OPTIONS', {}))
    connect_args = dict(options.get('connect_args', {}))
    connect_args.setdefault('connection_timeout', 5)
    ca = (app.config.get('ACCOUNTS_DATABASE_CA_CERT') or '').strip()
    if ca:
        connect_args['ssl_ca'] = _mysql_ca_file(ca)
    # A plain mysql:// URL is enough. No provider-specific TLS flags or CA file
    # are needed when the server presents a certificate trusted by the OS.
    connect_args.update(tls_connect_args())
    options.update(connect_args=connect_args)
    options.setdefault('pool_pre_ping', True)
    options.setdefault('hide_parameters', True)
    app.config['SQLALCHEMY_DATABASE_URI'] = url.render_as_string(hide_password=False)
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = options
    app.config['ACCOUNTS_DATABASE_REQUIRE_TLS'] = True


class AccountRequest(Request):
    @property
    def max_content_length(self):
        # Flask 3.0 reads this property when parsing the request body. A later
        # before_request assignment would not reliably cover multipart parsing.
        if self.endpoint == 'account_admin.setup':
            return 30 * 1024 * 1024
        return super().max_content_length


def create_app(test_config=None):
    app = Flask(__name__)
    app.request_class = AccountRequest
    for key in (
        'ACCOUNTS_DATABASE_URL', 'ACCOUNTS_SECRET_KEY', 'ACCOUNTS_ISSUER',
        'ACCOUNTS_SIGNING_KEY_FILE', 'ACCOUNTS_SMTP_HOST', 'ACCOUNTS_SMTP_PORT',
        'ACCOUNTS_SMTP_USERNAME', 'ACCOUNTS_SMTP_PASSWORD', 'ACCOUNTS_SMTP_FROM',
        'ACCOUNTS_DATABASE_CA_CERT', 'ACCOUNTS_SETUP_TOKEN',
    ):
        app.config[key] = os.getenv(key)
    app.config.update(
        ACCOUNTS_ALLOW_HTTP=_bool(os.getenv('ACCOUNTS_ALLOW_HTTP', 'false')),
        ACCOUNTS_SMTP_STARTTLS=_bool(os.getenv('ACCOUNTS_SMTP_STARTTLS', 'true')),
        ACCOUNTS_TOKEN_TTL=_integer_environment('ACCOUNTS_TOKEN_TTL', 3600),
        ACCOUNTS_PROXY_HOPS=_integer_environment('ACCOUNTS_PROXY_HOPS', 0),
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
        raise StartupError('ACCOUNTS_ISSUER', 'ACCOUNTS_ISSUER must be an HTTPS origin (HTTP loopback only with ACCOUNTS_ALLOW_HTTP=true).')
    app.config['ACCOUNTS_ISSUER'] = issuer
    if not isinstance(app.secret_key, str) or len(app.secret_key) < 32:
        raise StartupError('ACCOUNTS_SECRET_KEY', 'ACCOUNTS_SECRET_KEY must contain at least 32 random characters.')
    if not app.config['SQLALCHEMY_DATABASE_URI']:
        raise StartupError('ACCOUNTS_DATABASE_URL', 'Set ACCOUNTS_DATABASE_URL to a separate account database.')
    setup_token = app.config.get('ACCOUNTS_SETUP_TOKEN') or ''
    if setup_token and (not isinstance(setup_token, str) or not 32 <= len(setup_token) <= 1024):
        raise StartupError('ACCOUNTS_SETUP_TOKEN', 'ACCOUNTS_SETUP_TOKEN must contain 32–1024 random characters.')
    app.config['ACCOUNTS_SETUP_TOKEN'] = setup_token
    _configure_database(app)
    if not 60 <= app.config['ACCOUNTS_TOKEN_TTL'] <= 3600:
        raise StartupError('ACCOUNTS_TOKEN_TTL', 'ACCOUNTS_TOKEN_TTL must be between 60 and 3600 seconds.')
    if app.config['ACCOUNTS_PROXY_HOPS'] not in {0, 1, 2}:
        raise StartupError('ACCOUNTS_PROXY_HOPS', 'ACCOUNTS_PROXY_HOPS must be 0, 1 or 2 trusted reverse proxies.')
    if app.config['ACCOUNTS_PROXY_HOPS']:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=app.config['ACCOUNTS_PROXY_HOPS'], x_proto=app.config['ACCOUNTS_PROXY_HOPS'], x_host=0, x_port=0, x_prefix=0)
    app.config['SESSION_COOKIE_SECURE'] = urlsplit(issuer).scheme == 'https'
    app.config['OAUTH2_TOKEN_EXPIRES_IN'] = {'authorization_code': app.config['ACCOUNTS_TOKEN_TTL']}
    # Import every independent account model before preparing fresh schemas.
    from .admin import admin
    from .bootstrap import key_id, prepare_database
    db.init_app(app)
    if app.config.get('ACCOUNTS_DATABASE_REQUIRE_TLS'):
        with app.app_context():
            event.listen(db.engine, 'connect', _require_mysql_tls)
    key_file = app.config.get('ACCOUNTS_SIGNING_KEY_FILE')
    if key_file:
        try:
            private_key = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
            if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2048:
                raise ValueError('RSA 2048-bit or larger required')
        except (OSError, ValueError, TypeError):
            raise StartupError('ACCOUNTS_SIGNING_KEY_FILE', 'ACCOUNTS_SIGNING_KEY_FILE must contain a valid private RSA key of at least 2048 bits.') from None
    else:
        with app.app_context():
            try:
                private_key = prepare_database(with_signing_key=True)
            except Exception:
                db.engine.dispose()
                raise
    kid = key_id(private_key)
    key = RSAKey.import_key(private_key, parameters={'kid': kid, 'use': 'sig', 'alg': 'RS256'})
    app.extensions['accounts_signing_key'] = key
    app.extensions['accounts_public_key'] = key.as_dict(private=False)
    app.extensions['accounts_key_id'] = kid
    # Authlib's DEBUG messages can include bearer tokens; never enable them here.
    logging.getLogger('authlib').setLevel(logging.WARNING)
    from .provider import init_provider
    init_provider(app)
    from .views import web
    app.register_blueprint(web)
    app.register_blueprint(admin)
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
