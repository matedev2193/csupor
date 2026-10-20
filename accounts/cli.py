"""Operator commands: no credentials, hashes, or personal records are printed."""
import json
import os
import re
import time
from pathlib import Path

import click
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .models import Account, AccessToken, AuthorizationCode, Client, Membership, PasswordReset, RateLimit, db
from .importing import MAX_PLAINTEXT_BYTES, apply_import, valid_password_hash as _valid_hash


def register_cli(app):
    @app.cli.command('init-db')
    def init_db():
        """Create only account-service tables in its configured database."""
        db.create_all()
        click.echo('Account database ready.')

    @app.cli.command('add-client')
    @click.argument('client_id')
    @click.option('--name', required=True)
    @click.option('--redirect-uri', multiple=True, required=True)
    @click.option('--allow-registration', is_flag=True, default=False)
    @click.option('--secret-env', default='ACCOUNTS_CLIENT_SECRET', show_default=True)
    def add_client(client_id, name, redirect_uri, allow_registration, secret_env):
        """Register an application. Put its private secret in SECRET_ENV."""
        from . import allowed_url
        secret = os.getenv(secret_env, '')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', client_id) or not 1 <= len(name) <= 120:
            raise click.ClickException('Invalid client ID or display name.')
        if not 32 <= len(secret) <= 1024:
            raise click.ClickException('Set the client secret environment variable to 32 or more random characters.')
        if not redirect_uri or not all(allowed_url(u, app.config['ACCOUNTS_ALLOW_HTTP']) for u in redirect_uri):
            raise click.ClickException('Use explicit HTTPS callback URLs without fragments. HTTP loopback requires development mode.')
        if db.session.get(Client, client_id):
            raise click.ClickException('Client already exists. No settings or secret were overwritten.')
        client = Client(client_id=client_id, name=name, redirect_uris=list(dict.fromkeys(redirect_uri)), allow_registration=allow_registration)
        client.set_secret(secret)
        db.session.add(client)
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            raise click.ClickException('Could not save the application.') from None
        click.echo('Application registered.')

    @app.cli.command('grant-access')
    @click.argument('client_id')
    @click.argument('subject')
    def grant_access(client_id, subject):
        """Grant SUBJECT access to exactly one application."""
        account, client = db.session.get(Account, subject), db.session.get(Client, client_id)
        if not account or not account.active or not client or not client.active:
            raise click.ClickException('Active account and application are required.')
        if not db.session.get(Membership, (subject, client_id)):
            db.session.add(Membership(account_subject=subject, client_id=client_id))
            db.session.commit()
        click.echo('Application access granted.')

    @app.cli.command('revoke-access')
    @click.argument('client_id')
    @click.argument('subject')
    def revoke_access(client_id, subject):
        """Remove one application's access; keep other applications available."""
        db.session.execute(db.delete(Membership).where(Membership.account_subject == subject, Membership.client_id == client_id))
        db.session.commit()
        click.echo('Application access revoked.')

    @app.cli.command('disable-user')
    @click.argument('subject')
    def disable_user(subject):
        """Deactivate the central account and invalidate all sessions/tokens."""
        changed = db.session.execute(db.update(Account).where(Account.subject == subject).values(active=False, version=Account.version + 1))
        db.session.commit()
        if not changed.rowcount:
            raise click.ClickException('Account not found.')
        click.echo('Account disabled. All application sessions must revalidate.')

    @app.cli.command('import-users')
    @click.argument('filename', type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option('--client-id', required=True)
    def import_users(filename, client_id):
        """Import an explicit CSUPOR export without changing IDs or passwords."""
        client = db.session.get(Client, client_id)
        if not client or not client.active:
            raise click.ClickException('Register an active target application first.')
        try:
            if filename.stat().st_size > MAX_PLAINTEXT_BYTES:
                raise ValueError('Export is too large.')
            data = json.loads(filename.read_text(encoding='utf-8'))
            imported, total = apply_import(data, client_id, app.config['ACCOUNTS_ISSUER'])
            db.session.commit()
        except (ValueError, TypeError, KeyError, OSError, SQLAlchemyError):
            db.session.rollback()
            # No row values, database details or hashes are leaked on failure.
            raise click.ClickException('Import rejected. Check issuer, fields, duplicate identities, supported hashes and conflicts. No accounts were imported or overwritten.') from None
        click.echo(f'Imported {imported} new accounts; {total} application memberships ready.')

    @app.cli.command('cleanup')
    def cleanup():
        """Remove expired token/security records; schedule daily."""
        now = int(time.time())
        db.session.execute(db.delete(AccessToken).where(AccessToken.expires_at < now))
        db.session.execute(db.delete(PasswordReset).where(PasswordReset.expires_at < now))
        # Retain consumed nonces for a full day to catch delayed browser replays.
        db.session.execute(db.delete(AuthorizationCode).where(AuthorizationCode.expires_at < now - 86400))
        db.session.execute(db.delete(RateLimit).where(RateLimit.window_start < now - 86400))
        db.session.commit()
        click.echo('Expired security records removed.')
