"""Console-free account-service preparation followed by Gunicorn."""
import os
import sqlite3
import sys
import time

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from . import create_app
from .models import db
from .bootstrap import prepare_database
from .errors import StartupError, diagnostic
from .domain_setup import create_domain_setup_app, domain_setup_requested


def _retryable(error):
    if not isinstance(error, DBAPIError):
        return False
    # A rejected certificate or credential will not recover by waiting. Some
    # MySQL TLS failures are InterfaceError, just like transient disconnects.
    errno = getattr(error.orig, 'errno', None)
    if errno in {1044, 1045, 1049, 1142, 1143, 1227, 1698, 2026, 2061, 3159}:
        return False
    if error.connection_invalidated or isinstance(error, InterfaceError):
        return True
    if getattr(error.orig, 'errno', None) in {1040, 1205, 1213, 2002, 2003, 2005, 2006, 2013, 2055}:
        return True
    return (isinstance(error, OperationalError) and isinstance(error.orig, sqlite3.OperationalError)
            and any(value in str(error.orig).lower() for value in ('database is locked', 'unable to open database file')))


def prepare_app(*, retry_seconds=30):
    if domain_setup_requested():
        return create_domain_setup_app()
    deadline = time.monotonic() + retry_seconds
    while True:
        app = None
        try:
            app = create_app()
            with app.app_context():
                # Also needed with an explicit legacy signing-key file, whose
                # factory path intentionally does not require database access.
                prepare_database()
                from .admin import prepare_setup_state
                prepare_setup_state()
                db.session.remove()
                db.engine.dispose()
            return app
        except Exception as error:
            if app is not None:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()
            remaining = deadline - time.monotonic()
            if not _retryable(error) or remaining <= 5:
                raise
            print('A fiókrendszer adatbázisa még nem érhető el; rövidesen újrapróbáljuk.', file=sys.stderr, flush=True)
            time.sleep(min(2, remaining - 5))


def _port():
    try:
        port = int(os.environ.get('PORT', '8080'))
    except (TypeError, ValueError):
        raise StartupError('PORT', 'PORT must be an integer from 1 to 65535.') from None
    if not 1 <= port <= 65535:
        raise StartupError('PORT', 'PORT must be an integer from 1 to 65535.')
    return port


def worker_app():
    # Gunicorn creates its workers after exec; main's exception handler is no
    # longer present. Suppress driver exception chains at this boundary too.
    try:
        if domain_setup_requested():
            return create_domain_setup_app()
        return create_app()
    except Exception as error:
        raise RuntimeError(diagnostic(error)) from None


def main():
    try:
        port = _port()
        app = prepare_app()
        if app is not None and app.config.get('ACCOUNTS_DOMAIN_SETUP'):
            print('[ACCOUNTS_DOMAIN_SETUP] A kiszolgáló domainbeállítási módban indul. Add hozzá a domaint, majd állítsd be az ACCOUNTS_ISSUER értékét, és indíts új deployt. A fiókkezelés még nincs bekapcsolva.', flush=True)
        else:
            print('A fiókrendszer előkészítése kész; a webkiszolgáló indul.', flush=True)
        os.execv(sys.executable, [sys.executable, '-m', 'gunicorn', '--bind', f'0.0.0.0:{port}',
                                '--workers', '2', '--worker-tmp-dir', '/dev/shm', 'accounts.start:worker_app()'])
    except Exception as error:
        # Only allowlisted messages reach the log, never raw exception text,
        # SQL, connection URLs, keys or environment-variable values.
        print(diagnostic(error), file=sys.stderr, flush=True)
        print('A fiókrendszer előkészítése sikertelen. A webkiszolgáló nem indult el.',
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
