"""Explicit, offline migration of credentials; never copy personnel records."""

import json
import os
from pathlib import Path
from secrets import token_hex
from urllib.parse import urlsplit
from uuid import uuid4

import click
from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from . import db
from .models import User
from .shared_auth_models import ExternalIdentity


def _issuer(value):
    value = value.rstrip("/")
    parsed = urlsplit(value)
    local_http = (
        current_app.config.get("AUTH_ALLOW_HTTP")
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        len(value) > 255 or not parsed.netloc or parsed.username or parsed.password
        or parsed.query or parsed.fragment or not (parsed.scheme == "https" or local_http)
    ):
        raise click.ClickException("Use the exact HTTPS issuer URL (HTTP only for explicit loopback development).")
    return value


def _export_payload(issuer):
    """Build a locked snapshot and stage stable links; the caller commits it.

    The caller must freeze normal account mutations before using this helper.
    No password, role or personnel record is modified here.
    """
    if current_app.config.get("AUTH_MODE", "local") != "local":
        raise click.ClickException("Export requires local authentication.")
    issuer = _issuer(issuer)
    users = db.session.execute(db.select(User).order_by(User.id).with_for_update()).scalars().all()
    entries = []
    seen_emails, seen_usernames = set(), set()
    for user in users:
        email = user.email.strip().lower()
        username = user.username.strip()
        if (
            not email or not username or email in seen_emails
            or username.casefold() in seen_usernames
            or not user.password_hash.startswith(("scrypt:", "pbkdf2:"))
        ):
            raise click.ClickException(
                "Resolve duplicate/empty identifiers or unsupported/already-retired passwords before exporting."
            )
        seen_emails.add(email)
        seen_usernames.add(username.casefold())
        identity = db.session.execute(
            db.select(ExternalIdentity).where(ExternalIdentity.user_id == user.id)
        ).scalar_one_or_none()
        if identity is None:
            identity = ExternalIdentity(issuer=issuer, subject=str(uuid4()), user_id=user.id, disabled=False)
            db.session.add(identity)
        elif identity.issuer != issuer or identity.disabled:
            raise click.ClickException("A user already has another or disabled external identity; nothing was exported.")
        entries.append({
            "subject": identity.subject, "username": username,
            "email": email, "password_hash": user.password_hash,
        })
    payload = {"version": 1, "source": "csupor", "issuer": issuer, "users": entries}
    return payload


def _export_users(issuer, output):
    """Write an exclusive private file before committing stable identity links.

    Stop all app workers during export/import/cutover. No distributed transaction
    can freeze credential changes in a separate running application.
    """
    if current_app.config.get("AUTH_MODE", "local") != "local":
        raise click.ClickException("Export requires AUTH_MODE=local and stopped web/mail workers.")
    issuer = _issuer(issuer)
    destination = Path(output).absolute()
    if not destination.name.endswith(".accounts-export.json"):
        raise click.ClickException("The output filename must end in .accounts-export.json.")
    created = False
    try:
        payload = _export_payload(issuer)
        # O_EXCL also refuses symlink targets; umask can only make 0600 stricter.
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        db.session.commit()
    except Exception:
        db.session.rollback()
        if created:
            destination.unlink(missing_ok=True)
        raise
    return len(payload["users"])


def init_account_migration(app):
    @app.cli.group("accounts")
    def accounts_cli():
        """Export legacy identities and retire local authentication after cutover."""

    @accounts_cli.command("export")
    @click.option("--issuer", required=True, help="Final identity-service issuer URL; keep stable forever.")
    @click.option("--output", type=click.Path(path_type=Path), required=True)
    def export_command(issuer, output):
        """Run offline in local mode. Private output contains password hashes."""
        try:
            count = _export_users(issuer, output)
        except (OSError, SQLAlchemyError):
            raise click.ClickException("Export failed; check the private output path and database. No credentials were changed.") from None
        click.echo(f"Exported {count} identities. Keep the export private; import before enabling OIDC.")

    @accounts_cli.command("retire-local-passwords")
    @click.option("--confirm-imported-and-tested", is_flag=True, help="Confirm the import and real logins have been verified.")
    def retire_command(confirm_imported_and_tested):
        """Irreversibly remove local password copies after a verified migration."""
        if not confirm_imported_and_tested:
            raise click.ClickException("Verify the central import and real logins first, then pass --confirm-imported-and-tested.")
        if current_app.config.get("AUTH_MODE") != "oidc":
            raise click.ClickException("Enable AUTH_MODE=oidc before retiring local passwords.")
        issuer = current_app.config["OIDC_ISSUER"]
        try:
            users = db.session.execute(db.select(User).with_for_update()).scalars().all()
            for user in users:
                identity = db.session.execute(db.select(ExternalIdentity).where(
                    ExternalIdentity.user_id == user.id, ExternalIdentity.issuer == issuer,
                    ExternalIdentity.disabled.is_(False),
                )).scalar_one_or_none()
                if identity is None:
                    raise click.ClickException("An unmigrated user remains. No passwords were retired.")
            for user in users:
                user.password_hash = "!central-account-only!" + token_hex(24)
            from .password_reset_models import PasswordResetToken
            db.session.execute(db.delete(PasswordResetToken))
            db.session.commit()
        except click.ClickException:
            db.session.rollback()
            raise
        except SQLAlchemyError:
            db.session.rollback()
            raise click.ClickException("Password retirement failed; no changes were committed.") from None
        click.echo(f"Retired {len(users)} local password copies and all legacy reset links. Central accounts are unchanged.")
