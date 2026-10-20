"""Independent fire and occupational safety learning application."""
import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, g, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from .models import db


def _bool(value):
    return str(value).lower() in {'1', 'true', 'yes', 'on'}


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv('SAFETY_SECRET_KEY'),
        SQLALCHEMY_DATABASE_URI=os.getenv('SAFETY_DATABASE_URL'),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={'hide_parameters': True},
        SAFETY_UPLOAD_DIR=os.getenv('SAFETY_UPLOAD_DIR'),
        SAFETY_OIDC_ISSUER=os.getenv('SAFETY_OIDC_ISSUER', '').rstrip('/'),
        SAFETY_OIDC_CLIENT_ID=os.getenv('SAFETY_OIDC_CLIENT_ID', 'munkavedelem'),
        SAFETY_OIDC_CLIENT_SECRET=os.getenv('SAFETY_OIDC_CLIENT_SECRET', ''),
        SAFETY_OIDC_REDIRECT_URI=os.getenv('SAFETY_OIDC_REDIRECT_URI', ''),
        SAFETY_SESSION_TTL=int(os.getenv('SAFETY_SESSION_TTL', '3600')),
        SAFETY_ALLOW_HTTP=_bool(os.getenv('SAFETY_ALLOW_HTTP', 'false')),
        SAFETY_PROXY_HOPS=int(os.getenv('SAFETY_PROXY_HOPS', '0')),
        MAX_CONTENT_LENGTH=26 * 1024 * 1024,
        SESSION_COOKIE_NAME='micimacko_safety', SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_DOMAIN=None,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=1),
    )
    if test_config:
        app.config.update(test_config)
    if not isinstance(app.secret_key, str) or len(app.secret_key) < 32:
        raise RuntimeError('SAFETY_SECRET_KEY must contain at least 32 random characters.')
    if not app.config['SQLALCHEMY_DATABASE_URI']:
        raise RuntimeError('Set SAFETY_DATABASE_URL to the separate training database.')
    if not app.config['SAFETY_UPLOAD_DIR']:
        raise RuntimeError('Set SAFETY_UPLOAD_DIR to a private persistent directory outside static files.')
    upload_dir = Path(app.config['SAFETY_UPLOAD_DIR']).resolve()
    static_dir = Path(app.static_folder).resolve()
    if upload_dir == static_dir or static_dir in upload_dir.parents:
        raise RuntimeError('SAFETY_UPLOAD_DIR must not be inside the public static directory.')
    upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    app.config['SAFETY_UPLOAD_DIR'] = str(upload_dir)
    hops = app.config['SAFETY_PROXY_HOPS']
    if hops not in {0, 1, 2}:
        raise RuntimeError('SAFETY_PROXY_HOPS must be 0, 1 or 2 trusted proxies.')
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=0, x_port=0, x_prefix=0)
    db.init_app(app)

    @app.before_request
    def protect_request():
        if request.endpoint == 'health':
            return None
        # Fixed origin prevents poisoned redirects/host headers. OIDC validates
        # this configuration during startup. Tests use explicit loopback URLs.
        origin = urlsplit(app.config['SAFETY_OIDC_REDIRECT_URI'])
        if request.scheme != origin.scheme or request.host.lower() != origin.netloc.lower():
            abort(400, description='A rendszer a beállított saját címén érhető el.')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
            expected = session.get('safety_csrf')
            if not isinstance(supplied, str) or not isinstance(expected, str) or not secrets.compare_digest(supplied.encode(), expected.encode()):
                abort(400, description='A munkamenet lejárt. Frissítsd az oldalt, majd próbáld újra.')

    from .auth import init_auth
    init_auth(app)
    from .views import web
    app.register_blueprint(web)
    from .cli import register_cli
    register_cli(app)

    def csrf_token():
        if 'safety_csrf' not in session:
            session['safety_csrf'] = secrets.token_urlsafe(32)
        return session['safety_csrf']

    @app.template_filter('local_datetime')
    def local_datetime(value):
        if value is None:
            return '—'
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo('Europe/Budapest')).strftime('%Y.%m.%d. %H:%M')

    app.context_processor(lambda: {
        'csrf_token': csrf_token,
        'current_year': datetime.now(ZoneInfo('Europe/Budapest')).year,
        'kind_labels': {'fire': 'Tűzvédelem', 'work': 'Munkavédelem'},
        'account_url': app.config['SAFETY_OIDC_ISSUER'] + '/account',
    })

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.after_request
    def secure_response(response):
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Pragma'] = 'no-cache'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "font-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; "
            "object-src 'none'; frame-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'none'"
        )
        if app.config.get('SESSION_COOKIE_SECURE'):
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    from werkzeug.exceptions import HTTPException

    @app.errorhandler(HTTPException)
    def show_error(error):
        if request.is_json:
            return {'error': error.description}, error.code
        return render_template('error.html', message=error.description, code=error.code), error.code

    return app
