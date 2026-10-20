"""Operator commands: no credentials, hashes, or personal records are printed."""
import json
import os
import re
import time
from pathlib import Path
from uuid import UUID

import click
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .models import Account, AccessToken, AuthorizationCode, Client, Membership, PasswordReset, RateLimit, db
from .security import valid_email


def _valid_hash(value):
    if not isinstance(value, str) or len(value) > 255:
        return False
    parts = value.split('$')
    if len(parts) != 3 or not re.fullmatch(r'[A-Za-z0-9]{1,64}', parts[1]):
        return False
    method, _, digest = parts
    if re.fullmatch(r'[0-9a-f]+', digest) is None:
        return False
    if method.startswith('scrypt:'):
        try:
            n, r, p = map(int, method.split(':')[1:])
            return 2 <= n <= 65536 and n & (n - 1) == 0 and 1 <= r <= 8 and 1 <= p <= 4 and len(digest) == 128
        except (ValueError, TypeError):
            return False
    if method.startswith('pbkdf2:'):
        try:
            _, algorithm, iterations = method.split(':')
            return algorithm in {'sha256', 'sha512'} and 1 <= int(iterations) <= 5000000 and len(digest) == {'sha256': 64, 'sha512': 128}[algorithm]
        except (ValueError, TypeError):
            return False
    return False


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
            if filename.stat().st_size > 20 * 1024 * 1024:
                raise ValueError('Export is too large.')
            data = json.loads(filename.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('version') != 1 or data.get('source') != 'csupor' or data.get('issuer') != app.config['ACCOUNTS_ISSUER']:
                raise ValueError('Export version, source or issuer does not match.')
            rows = data.get('users')
            if not isinstance(rows, list) or len(rows) > 50000:
                raise ValueError('Invalid user list.')
            subjects, emails, names = set(), set(), set()
            imported = 0
            for index, row in enumerate(rows, 1):
                if not isinstance(row, dict) or set(row) != {'subject', 'username', 'email', 'password_hash'}:
                    raise ValueError(f'Record {index}: invalid fields.')
                subject, username, email, password_hash = (row[k] for k in ('subject', 'username', 'email', 'password_hash'))
                if not isinstance(subject, str) or str(UUID(subject)) != subject:
                    raise ValueError(f'Record {index}: invalid subject.')
                if (not isinstance(username, str) or not 1 <= len(username) <= 50 or username != username.strip()
                        or any(ord(c) < 32 or ord(c) == 127 for c in username)):
                    raise ValueError(f'Record {index}: invalid username.')
                if not valid_email(email) or email != email.strip().lower() or not _valid_hash(password_hash):
                    raise ValueError(f'Record {index}: invalid email or unsupported password hash.')
                if subject in subjects or email.casefold() in emails or username.casefold() in names:
                    raise ValueError(f'Record {index}: duplicate identity.')
                subjects.add(subject); emails.add(email.casefold()); names.add(username.casefold())
                existing = db.session.execute(db.select(Account).where(
                    (Account.subject == subject) | (func.lower(Account.email) == email.lower()) | (func.lower(Account.username) == username.lower())
                )).scalars().all()
                if existing:
                    if (len(existing) != 1 or existing[0].subject != subject or existing[0].email != email
                            or existing[0].username != username or existing[0].password_hash != password_hash):
                        raise ValueError(f'Record {index}: conflicts with an existing identity. Nothing was overwritten.')
                else:
                    db.session.add(Account(subject=subject, username=username, email=email, password_hash=password_hash, version=1, active=True, email_verified=False))
                    db.session.flush()
                    imported += 1
                if not db.session.get(Membership, (subject, client_id)):
                    db.session.add(Membership(account_subject=subject, client_id=client_id))
            # A username may legitimately equal its own email in legacy CSUPOR.
            # Only collisions across different identities make login ambiguous.
            all_accounts = db.session.execute(db.select(Account)).scalars().all()
            identifiers = {}
            for account in all_accounts:
                for identifier in {account.email.casefold(), account.username.casefold()}:
                    owner = identifiers.setdefault(identifier, account.subject)
                    if owner != account.subject:
                        raise ValueError('Ambiguous login identifiers across accounts.')
            db.session.commit()
        except (ValueError, TypeError, KeyError, OSError, SQLAlchemyError):
            db.session.rollback()
            # No row values, database details or hashes are leaked on failure.
            raise click.ClickException('Import rejected. Check issuer, fields, duplicate identities, supported hashes and conflicts. No accounts were imported or overwritten.') from None
        click.echo(f'Imported {imported} new accounts; {len(rows)} application memberships ready.')

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
