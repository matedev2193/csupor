"""A limited first-deploy app that needs neither a domain nor account data."""
import os

from flask import Flask, Response, render_template, request


def domain_setup_requested():
    return not (os.getenv('ACCOUNTS_ISSUER') or '').strip()


def create_domain_setup_app():
    # Deliberately independent of the account factory, DB, sessions and keys.
    # Never infer an issuer from an incoming Host or forwarded header.
    app = Flask(__name__)
    app.config['ACCOUNTS_DOMAIN_SETUP'] = True
    app.config['MAX_CONTENT_LENGTH'] = 16384

    @app.get('/healthz')
    def health():
        return {'status': 'setup_required', 'mode': 'domain_setup'}

    @app.get('/')
    @app.get('/setup')
    def index():
        return render_template('domain_setup.html')

    @app.before_request
    def only_setup_information():
        if request.method in {'GET', 'HEAD'} and request.endpoint in {'health', 'index', 'static'}:
            return None
        # This includes all authentication, discovery, token, import, admin and
        # write requests. Nothing can enable/configure accounts over HTTP here.
        return Response('A fiókkezelés a domain beállítása és az új telepítés után érhető el.',
                        status=503, content_type='text/plain; charset=utf-8',
                        headers={'Retry-After': '60'})

    @app.after_request
    def protect_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    return app
