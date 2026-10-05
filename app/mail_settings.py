"""Developer-only mail configuration and a private, TLS-verified SMTP transport."""

import base64
import hashlib
import ipaddress
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from hmac import compare_digest
from secrets import token_urlsafe
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from . import db
from .mail_settings_models import MailServerSettings
from .mail_key import MailKeyError, get_mail_secret, initialise_mail_secret, key_status
from .models import UserPrivilege


mail_settings = Blueprint("mail_settings", __name__)
SMTP_TIMEOUT = 12
SECURITY_MODES = {"starttls", "ssl", "none"}
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class MailDeliveryError(Exception):
    """Safe error category, with no provider response or credential details."""

    def __init__(self, code="delivery"):
        self.code = code
        super().__init__("Email delivery is unavailable." if code == "disabled" else "Email could not be sent.")


def get_mail_settings():
    """Reading settings does not create a row or commit unrelated work."""
    return db.session.get(MailServerSettings, 1) or MailServerSettings(
        id=1, enabled=False, host="", port=587, security="starttls", username="",
        encrypted_password="", sender_email="", sender_name="CSUPOR", base_url="", revision="",
    )


def is_mail_enabled():
    saved = get_mail_settings()
    return bool(saved.enabled and saved.host and saved.sender_email and saved.base_url)


def _cipher():
    try:
        secret = get_mail_secret()
    except MailKeyError:
        raise MailDeliveryError("configuration") from None
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def _check_settings_csrf():
    token = session.get("mail_settings_csrf_token", "")
    if not token or not compare_digest(token.encode(), request.form.get("csrf_token", "").encode()):
        abort(400)


@mail_settings.post("/settings/email-key")
@login_required
def initialise_key():
    if current_user.privilege != UserPrivilege.developer:
        abort(403)
    _check_settings_csrf()
    # A missing key must not silently replace the key of existing credentials.
    if get_mail_settings().encrypted_password and key_status() != "ready":
        flash(_("A server password is already saved. Restore its encryption key, or disable emails and remove the saved password before creating a new key."), "error")
        return redirect(url_for("mail_settings.settings"))
    try:
        created = initialise_mail_secret()
    except MailKeyError:
        flash(_("The server could not securely save the encryption key. Ask your hosting provider to make the application's private instance folder writable."), "error")
    else:
        flash(_("Encryption key created. You can now save the email server details; no restart is needed.")
              if created else _("Email encryption is already set up. The existing key was kept."), "success")
    return redirect(url_for("mail_settings.settings"))


def _has_control(value):
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _valid_host(value):
    if not value or len(value) > 255 or _has_control(value):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return all(_DNS_LABEL.fullmatch(part) for part in value.rstrip(".").split("."))


def _valid_email(value):
    if not value or len(value) > 254 or _has_control(value) or not value.isascii():
        return False
    try:
        address = Address(addr_spec=value)
        return bool(
            address.addr_spec == value and address.username and len(address.username) <= 64
            and address.domain and _valid_host(address.domain)
        )
    except (ValueError, IndexError, HeaderParseError):
        return False


def _valid_base_url(value):
    if not value or len(value) > 500 or _has_control(value) or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in {"http", "https"} and parsed.hostname and _valid_host(parsed.hostname)
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and "\\" not in value
        )
    except ValueError:
        return False


def _form_values(form):
    return {
        "enabled": form.get("enabled") == "1",
        "host": form.get("host", "").strip(),
        "port": form.get("port", "").strip(),
        "security": form.get("security", ""),
        "username": form.get("username", "").strip(),
        "sender_email": form.get("sender_email", "").strip(),
        "sender_name": form.get("sender_name", "").strip(),
        "base_url": form.get("base_url", "").strip().rstrip("/"),
    }


def _validate_values(values, form):
    # Reject header/control characters before trimming, so pasted newlines never
    # silently become a different SMTP address or host.
    if any(_has_control(form.get(field, "")) for field in (
        "host", "port", "security", "username", "sender_email", "sender_name", "base_url",
    )):
        return _("Email settings must not contain line breaks or control characters.")
    if values["host"] and not _valid_host(values["host"]):
        return _("Enter a valid email server hostname or IP address.")
    if len(values["port"]) > 5 or not values["port"].isascii() or not values["port"].isdigit() or not 1 <= int(values["port"]) <= 65535:
        return _("Enter a port between 1 and 65535.")
    values["port"] = int(values["port"])
    if values["security"] not in SECURITY_MODES:
        return _("Choose a supported connection security option.")
    if len(values["username"]) > 255 or len(values["sender_name"]) > 120 or len(form.get("password", "")) > 4096:
        return _("An email setting is too long.")
    if values["sender_email"] and not _valid_email(values["sender_email"]):
        return _("Enter a valid sender email address.")
    if values["base_url"] and not _valid_base_url(values["base_url"]):
        return _("Enter the full application URL starting with https:// or http://, without a query or fragment.")
    if values["enabled"] and not all(values[key] for key in ("host", "sender_email", "base_url")):
        return _("Enter the server, sender email and application URL before enabling emails.")
    if form.get("clear_password") == "1" and form.get("password", ""):
        return _("Either enter a new password or remove the stored password.")
    return None


@mail_settings.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if current_user.privilege != UserPrivilege.developer:
        abort(403)
    saved = get_mail_settings()
    status = 200
    values = {key: getattr(saved, key) for key in (
        "enabled", "host", "port", "security", "username", "sender_email", "sender_name", "base_url",
    )}
    if request.method == "POST":
        _check_settings_csrf()
        if not compare_digest(saved.revision.encode(), request.form.get("revision", "").encode()):
            flash(_("Email settings were changed in another session. Review the current settings and try again."), "error")
            return redirect(url_for("mail_settings.settings"))
        values = _form_values(request.form)
        error = _validate_values(values, request.form)
        encrypted_password = saved.encrypted_password
        if not error:
            try:
                if values["enabled"] or request.form.get("password", ""):
                    cipher = _cipher()
                    if request.form.get("password", ""):
                        encrypted_password = cipher.encrypt(request.form["password"].encode("utf-8")).decode("ascii")
                    elif encrypted_password and request.form.get("clear_password") != "1":
                        # Detect a changed key before enabling an unusable setup.
                        cipher.decrypt(encrypted_password.encode("ascii"))
                if request.form.get("clear_password") == "1":
                    encrypted_password = ""
            except MailDeliveryError:
                if key_status() == "missing":
                    error = (_("A server password is already saved. Restore its encryption key, or disable emails and remove the saved password before creating a new key.")
                             if encrypted_password else
                             _("First use the Create encryption key button on this page, then save the email settings."))
                else:
                    error = _("The server encryption key is unavailable. Restore the existing key or check access to the private instance folder.")
            except (InvalidToken, ValueError, UnicodeError):
                error = _("The saved server password cannot be opened with the current encryption key. Please enter the password again.")
        if error:
            flash(error, "error")
            status = 400
        else:
            for key, value in values.items():
                setattr(saved, key, value)
            saved.encrypted_password = encrypted_password
            saved.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            saved.updated_by_id = current_user.id
            db.session.add(saved)
            try:
                db.session.commit()
            except (StaleDataError, IntegrityError):
                db.session.rollback()
                flash(_("Email settings were changed in another session. Review the current settings and try again."), "error")
                return redirect(url_for("mail_settings.settings"))
            if saved.enabled:
                from .notification_delivery import wake_notifications

                wake_notifications()
            flash(_("Email settings saved."), "success")
            return redirect(url_for("mail_settings.settings"))
    session.setdefault("mail_settings_csrf_token", token_urlsafe(32))
    response = current_app.make_response((render_template(
        "mail_settings.html", values=values, revision=saved.revision,
        password_saved=bool(saved.encrypted_password), csrf_token=session["mail_settings_csrf_token"],
        encryption_status=key_status(),
    ), status))
    response.headers["Cache-Control"] = "no-store"
    return response


def send_email(recipient_email, subject, text_body, html_body, message_id=None):
    """Send one message to one account address; never leak raw SMTP failures."""
    saved = get_mail_settings()
    if not saved.enabled:
        raise MailDeliveryError("disabled")
    if not (
        _valid_host(saved.host) and _valid_email(saved.sender_email) and _valid_email(recipient_email)
        and isinstance(saved.port, int) and 1 <= saved.port <= 65535
        and saved.security in SECURITY_MODES and not _has_control(saved.sender_name)
        and not _has_control(saved.username) and not _has_control(subject)
        and (message_id is None or not _has_control(message_id))
    ):
        raise MailDeliveryError("configuration")
    try:
        cipher = _cipher()
        password = cipher.decrypt(saved.encrypted_password.encode("ascii")).decode("utf-8") if saved.encrypted_password else ""
    except (MailDeliveryError, InvalidToken, ValueError, UnicodeError):
        raise MailDeliveryError("configuration") from None

    message = EmailMessage()
    message["From"] = Address(display_name=saved.sender_name, addr_spec=saved.sender_email)
    message["To"] = Address(addr_spec=recipient_email)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = message_id or make_msgid(domain=saved.sender_email.rsplit("@", 1)[-1])
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    try:
        context = ssl.create_default_context()
        if saved.security == "ssl":
            connection = smtplib.SMTP_SSL(saved.host, saved.port, timeout=SMTP_TIMEOUT, context=context)
        else:
            connection = smtplib.SMTP(saved.host, saved.port, timeout=SMTP_TIMEOUT)
        with connection as smtp:
            smtp.ehlo()
            if saved.security == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()
            if saved.username:
                smtp.login(saved.username, password)
            smtp.send_message(message, from_addr=saved.sender_email, to_addrs=[recipient_email])
    except smtplib.SMTPAuthenticationError:
        raise MailDeliveryError("authentication") from None
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected):
        raise MailDeliveryError("connection") from None
    except (smtplib.SMTPException, ValueError, UnicodeError):
        raise MailDeliveryError("delivery") from None
    except OSError:
        raise MailDeliveryError("connection") from None
