"""Persistent mail encryption keys, without exposing or rotating existing keys."""

import errno
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from flask import current_app


_DEFAULT_KEYS = {"dev-secret-key-change-me", "change-me", "changeme", "secret"}
_LOCAL_KEY = re.compile(rb"[0-9a-f]{64}\Z")


class MailKeyError(Exception):
    """A safe error category; never include paths, credentials or OS messages."""

    def __init__(self, code="storage"):
        self.code = code
        super().__init__("The mail encryption key is unavailable.")


def _usable_secret(value):
    if not isinstance(value, (str, bytes)) or not value:
        return False
    if isinstance(value, bytes):
        return value not in {item.encode("utf-8") for item in _DEFAULT_KEYS}
    return value not in _DEFAULT_KEYS


def _explicit_secret():
    for value in (current_app.config.get("EMAIL_SECRET_KEY"), os.getenv("EMAIL_SECRET_KEY")):
        if _usable_secret(value):
            return value
    return None


def _key_path():
    return Path(current_app.instance_path) / "email-secret.key"


def _read_local_secret():
    path = _key_path()
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise MailKeyError("storage") from None
    if not stat.S_ISREG(before.st_mode):
        raise MailKeyError("invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            actual = os.fstat(handle.fileno())
            if not stat.S_ISREG(actual.st_mode) or (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
                raise MailKeyError("invalid")
            if os.name == "posix" and stat.S_IMODE(actual.st_mode) & 0o077:
                raise MailKeyError("invalid")
            secret = handle.read(65)
    except OSError as error:
        raise MailKeyError("invalid" if error.errno == errno.ELOOP else "storage") from None
    if not _LOCAL_KEY.fullmatch(secret):
        raise MailKeyError("invalid")
    return secret.decode("ascii")


def get_mail_secret():
    """Use an explicit mail key, a local key, or the existing stable app key.

    A broken local file is an error, not permission to fall back to another key.
    Read the file afresh so every process observes the same initialised key.
    """
    explicit = _explicit_secret()
    if explicit is not None:
        return explicit
    local = _read_local_secret()
    if local is not None:
        return local
    fallback = current_app.config.get("SECRET_KEY")
    if _usable_secret(fallback):
        return fallback
    raise MailKeyError("missing")


def key_status():
    """Return an intentionally small, secret-free status for the settings page."""
    try:
        get_mail_secret()
    except MailKeyError as error:
        return "missing" if error.code == "missing" else "error"
    return "ready"


def initialise_mail_secret():
    """Create a complete, private local key exactly once, without replacing files.

    Publishing with an exclusive hard link prevents concurrent workers from
    seeing partially written contents or replacing the winning worker's key.
    """
    try:
        get_mail_secret()
        return False
    except MailKeyError as error:
        if error.code != "missing":
            raise

    path = _key_path()
    temporary = None
    published = False
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".email-secret-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(secrets.token_hex(32).encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
            published = True
        except FileExistsError:
            # A parallel initialisation won. Verify its complete file before
            # treating this request as successful, including invalid symlinks.
            if _read_local_secret() is None:
                raise MailKeyError("storage")
        if published and os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise MailKeyError("storage") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                # A temporary copy must not be mistaken for another usable key.
                # Surface only a safe error if the private copy cannot be removed.
                raise MailKeyError("storage") from None
    return published
