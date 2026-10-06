"""Opt-in first administrator setup from private hosting environment variables."""

import os
import re
import unicodedata

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import db
from .models import User, UserPrivilege, UserProfile


_ENVIRONMENT_KEYS = (
    "INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_EMAIL", "INITIAL_ADMIN_PASSWORD",
)
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\Z"
)
_COLLISION_ERROR = (
    "CSUPOR could not initialise the first administrator because the supplied "
    "credentials conflict with an existing account. To use an existing account, "
    "supply that account's matching username, email address and current password. "
    "No existing account was changed."
)


def _has_developer():
    return db.session.execute(
        db.select(User.id).where(User.privilege == UserPrivilege.developer).limit(1)
    ).scalar_one_or_none() is not None


def _validated_credentials(values):
    if any(value is None or value == "" for value in values):
        raise RuntimeError(
            "To initialise the first CSUPOR administrator, set all three hosting "
            "variables: INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_EMAIL and "
            "INITIAL_ADMIN_PASSWORD."
        )
    username, email, password = values
    username, email = username.strip(), email.strip().lower()
    if not 1 <= len(username) <= 50 or any(
        unicodedata.category(character).startswith("C")
        for character in username
    ):
        raise RuntimeError(
            "INITIAL_ADMIN_USERNAME must contain 1 to 50 characters, without "
            "control characters."
        )
    if (
        not 1 <= len(email) <= 120 or _EMAIL.fullmatch(email) is None
        or email.startswith(".") or ".@" in email or ".." in email
    ):
        raise RuntimeError("INITIAL_ADMIN_EMAIL must be a valid email address of at most 120 characters.")
    # Count Python Unicode characters, not encoded bytes. Do not trim passwords.
    if not 12 <= len(password) <= 1024:
        raise RuntimeError("INITIAL_ADMIN_PASSWORD must contain 12 to 1024 characters.")
    return username, email, password


def initialise_initial_admin():
    """Create one developer, or promote its password-verified existing account.

    The hosting values opt in only when no developer exists. Subsequent starts
    ignore them, including changed or incomplete values, and never reset an
    account password. Password limits are 12–1024 Unicode characters.
    """
    values = tuple(os.getenv(key) for key in _ENVIRONMENT_KEYS)
    if all(value is None for value in values) or _has_developer():
        return

    try:
        username, email, password = _validated_credentials(values)
        matches = db.session.execute(
            db.select(User).where((User.username == username) | (User.email == email))
        ).scalars().all()
        if matches:
            # A partial or split collision must never adopt or reset somebody
            # else's account, even when the deployment controls its database.
            if len(matches) != 1:
                raise RuntimeError(_COLLISION_ERROR)
            user = matches[0]
            if user.username != username or user.email.lower() != email:
                raise RuntimeError(_COLLISION_ERROR)
            try:
                password_matches = user.check_password(password)
            except (ValueError, TypeError):
                password_matches = False
            if not password_matches:
                raise RuntimeError(_COLLISION_ERROR)
            user.privilege = UserPrivilege.developer
        else:
            user = User(username=username, email=email, privilege=UserPrivilege.developer)
            user.set_password(password)
            db.session.add(user)
        if user.profile is None:
            user.profile = UserProfile()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # MySQL startup is already serialised by the bootstrap lock. For an
        # equivalent SQLite race, only a verified winning developer is success.
        try:
            completed_by_another_worker = _has_developer()
        except SQLAlchemyError:
            # A failed recovery query must not reveal the original INSERT's
            # bound account details through its chained exception context.
            completed_by_another_worker = False
        db.session.rollback()
        if completed_by_another_worker:
            return
        raise RuntimeError(
            "CSUPOR could not save the first administrator. Check the initial "
            "administrator settings and the database configuration. No existing "
            "account was changed."
        ) from None
    except SQLAlchemyError:
        db.session.rollback()
        # SQLAlchemy errors can contain bound email addresses or password hashes.
        raise RuntimeError(
            "CSUPOR could not save the first administrator. Check the database "
            "configuration and permissions. No existing account was changed."
        ) from None
    except Exception:
        db.session.rollback()
        raise
