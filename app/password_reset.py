"""Public, single-use password recovery through the configured email server."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
import json
import re
from secrets import token_urlsafe
from threading import BoundedSemaphore

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_babel import force_locale, gettext as _
from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import generate_password_hash

from . import db, get_locale
from .mail_settings import MailDeliveryError, _valid_base_url, _valid_email, get_mail_settings, send_email
from .models import User
from .password_reset_models import PasswordResetThrottle, PasswordResetToken


password_reset = Blueprint("password_reset", __name__)
TOKEN_LIFETIME = timedelta(minutes=30)
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_delivery_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="csupor-password-reset")
_delivery_capacity = BoundedSemaphore(16)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _digest(value):
    secret = current_app.config["SECRET_KEY"]
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac_new(secret, value.encode("utf-8"), sha256).hexdigest()


def _credentials_hash(user):
    return _digest(json.dumps([user.id, user.email, user.password_hash], ensure_ascii=True))


def _csrf_token():
    return session.setdefault("password_reset_csrf", token_urlsafe(32))


def _check_csrf():
    expected = session.get("password_reset_csrf", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not compare_digest(expected.encode(), supplied.encode()):
        abort(400)


def _take_limit(kind, value, maximum, duration, now):
    """Use conditional database writes, including the first-row race."""
    key = _digest(kind + ":" + value)
    existing = db.session.get(PasswordResetThrottle, key)
    if existing is None:
        try:
            with db.session.begin_nested():
                db.session.add(PasswordResetThrottle(
                    key_hash=key, expires_at=now + duration, request_count=0,
                ))
                db.session.flush()
        except IntegrityError:
            # Another worker inserted the same bucket. The following updates
            # still arbitrate its capacity atomically.
            pass
    db.session.execute(update(PasswordResetThrottle).where(
        PasswordResetThrottle.key_hash == key,
        PasswordResetThrottle.expires_at <= now,
    ).values(expires_at=now + duration, request_count=0).execution_options(synchronize_session=False))
    claimed = db.session.execute(update(PasswordResetThrottle).where(
        PasswordResetThrottle.key_hash == key,
        PasswordResetThrottle.expires_at > now,
        PasswordResetThrottle.request_count < maximum,
    ).values(request_count=PasswordResetThrottle.request_count + 1).execution_options(synchronize_session=False))
    return claimed.rowcount == 1


def _allow_request(email, requester):
    now = _now()
    # IP limits use the server's remote address, never a user-supplied forwarded
    # header. Email limits apply equally to existing and unknown addresses.
    permitted = _take_limit("requester", requester, 20, timedelta(hours=1), now)
    if permitted:
        permitted = _take_limit("email-hour", email, 3, timedelta(hours=1), now)
    if permitted:
        permitted = _take_limit("email-minute", email, 1, timedelta(minutes=1), now)
    # Expired buckets and token hashes have no long-term purpose.
    db.session.execute(delete(PasswordResetThrottle).where(PasswordResetThrottle.expires_at < now - timedelta(days=1)))
    db.session.execute(delete(PasswordResetToken).where(PasswordResetToken.expires_at < now - timedelta(days=1)))
    db.session.commit()
    return permitted


def _deliver_reset(app, email, locale):
    """Resolve the account and contact SMTP outside the HTTP response path."""
    with app.app_context(), force_locale(locale):
        token_hash = None
        try:
            settings = get_mail_settings()
            if not settings.enabled or not _valid_base_url(settings.base_url):
                return
            # Refuse ambiguous case-only duplicate accounts on older databases.
            users = db.session.scalars(db.select(User).where(func.lower(User.email) == email).limit(2)).all()
            if len(users) != 1:
                return
            user = users[0]
            token = token_urlsafe(32)
            token_hash = sha256(token.encode("ascii")).hexdigest()
            now = _now()
            recipient = user.email
            base_url = settings.base_url.rstrip("/")
            db.session.add(PasswordResetToken(
                token_hash=token_hash, user_id=user.id,
                credentials_hash=_credentials_hash(user), created_at=now,
                expires_at=now + TOKEN_LIFETIME,
            ))
            db.session.commit()
            # The fragment is not sent to the HTTP server or as a Referer.
            # Host/X-Forwarded-Host headers never influence the reset URL.
            reset_url = base_url + "/reset-password#" + token
            subject = _("Reset your CSUPOR password")
            text_body = _("You requested a new password for your CSUPOR account.") + "\n\n" + reset_url + "\n\n" + _("This link is valid for 30 minutes and can be used once.") + "\n" + _("If you did not request this, ignore this email. Your password has not changed.")
            # Mail runs without an HTTP request. Do not invoke page context
            # processors, which require the browser's session/current user.
            html_body = app.jinja_env.get_template("password_reset_email.html").render(reset_url=reset_url, _=_)
            send_email(recipient, subject, text_body, html_body)
        except (MailDeliveryError, SQLAlchemyError):
            db.session.rollback()
            if token_hash:
                try:
                    db.session.execute(delete(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash))
                    db.session.commit()
                except SQLAlchemyError:
                    db.session.rollback()
            # Do not attach exception/provider details, addresses or secrets.
            app.logger.warning("Password reset email could not be delivered.")
        finally:
            db.session.remove()


def _dispatch_reset(app, email, locale):
    """Bound background work; a busy or restarting server permits a later retry."""
    if not _delivery_capacity.acquire(blocking=False):
        return

    def run():
        try:
            _deliver_reset(app, email, locale)
        finally:
            _delivery_capacity.release()

    try:
        _delivery_pool.submit(run)
    except RuntimeError:
        _delivery_capacity.release()


@password_reset.after_request
def _private_response(response):
    response.cache_control.no_store = True
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@password_reset.route("/forgot-password", methods=["GET", "POST"])
def request_reset():
    if request.method == "POST":
        _check_csrf()
        email = request.form.get("email", "").strip().lower()
        # Process every request through the same public response, including a
        # syntactically invalid address or a disabled/misconfigured mail server.
        if len(email) <= 120 and _valid_email(email):
            try:
                allowed = _allow_request(email, request.remote_addr or "unknown")
            except SQLAlchemyError:
                db.session.rollback()
                allowed = False
                current_app.logger.warning("Password reset request could not be processed.")
            if allowed:
                _dispatch_reset(current_app._get_current_object(), email, str(get_locale()))
        flash(_("If this email address belongs to an account and email delivery is available, a password reset link will arrive shortly. If needed, try again in a few minutes."), "success")
        return redirect(url_for("password_reset.request_reset"))
    return render_template("forgot_password.html", csrf_token=_csrf_token())


def _consume_token(token, new_password):
    """Claim the token and change unchanged credentials in one transaction."""
    if not _TOKEN_PATTERN.fullmatch(token):
        return False
    now = _now()
    token_hash = sha256(token.encode("ascii")).hexdigest()
    saved = db.session.get(PasswordResetToken, token_hash)
    if saved is None or saved.consumed_at is not None or saved.expires_at <= now:
        return False
    user = db.session.get(User, saved.user_id)
    if user is None or not compare_digest(saved.credentials_hash, _credentials_hash(user)):
        return False
    # Snapshot values guard a concurrent email/password edit as well as a
    # second reset using a different still-valid token for this account.
    old_email, old_hash, user_id = user.email, user.password_hash, user.id
    new_hash = generate_password_hash(new_password)
    claimed = db.session.execute(update(PasswordResetToken).where(
        PasswordResetToken.token_hash == token_hash,
        PasswordResetToken.consumed_at.is_(None),
        PasswordResetToken.expires_at > _now(),
    ).values(consumed_at=_now()).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        db.session.rollback()
        return False
    changed = db.session.execute(update(User).where(
        User.id == user_id, User.email == old_email, User.password_hash == old_hash,
    ).values(password_hash=new_hash).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        db.session.rollback()
        return False
    db.session.commit()
    return True


@password_reset.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    error = None
    if request.method == "POST":
        _check_csrf()
        password = request.form.get("new_password", "")
        if not 12 <= len(password) <= 1024:
            error = _("Choose a password between 12 and 1024 characters.")
        elif password != request.form.get("new_password_confirm", ""):
            error = _("The new passwords do not match.")
        else:
            try:
                consumed = _consume_token(request.form.get("token", ""), password)
            except SQLAlchemyError:
                db.session.rollback()
                consumed = False
                current_app.logger.warning("Password reset could not be completed.")
            if consumed:
                # Recovery does not sign the account in automatically. Clear
                # this browser's previous authentication and recovery forms.
                locale = session.get("locale")
                session.clear()
                if locale:
                    session["locale"] = locale
                flash(_("Your password has been reset. Log in with your new password."), "success")
                return redirect(url_for("login"))
            error = _("This password reset link is invalid or has expired. Request a new link.")
        flash(error, "error")
    # Preserve only a well-formed submitted token after password validation;
    # neither passwords nor arbitrary user input are echoed into the form.
    supplied_token = request.form.get("token", "") if request.method == "POST" else ""
    return render_template(
        "reset_password.html", csrf_token=_csrf_token(),
        reset_token=supplied_token if _TOKEN_PATTERN.fullmatch(supplied_token) else "",
    ), 400 if error else 200
