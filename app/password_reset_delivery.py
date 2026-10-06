"""Best-effort diagnostics for authorised email administrators.

Only a normalised recipient address and fixed categories are retained. Never
store tokens, message bodies, passwords, exception strings or provider replies.
Separate sessions keep diagnostics from committing or rolling back caller work.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from flask import current_app
from flask_babel import lazy_gettext as _
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from . import db
from .password_reset_delivery_models import PasswordResetDelivery


RETENTION = timedelta(days=7)
MAX_RECORDS = 1000
STATUS_LABELS = {
    "queued": _("Waiting to send"),
    "sent": _("Accepted by the email server"),
    "failed": _("Could not send"),
    "rate_limited": _("Request limit reached"),
    "not_found": _("No matching account"),
    "disabled": _("Email delivery is disabled"),
    "configuration": _("Email configuration needs attention"),
    "worker_unavailable": _("Email worker is unavailable"),
}
ERROR_LABELS = {
    "application_url": _("Enter a valid application URL in Email settings."),
    "delivery": _("The email server did not accept the message."),
    "authentication": _("The email server rejected authentication."),
    "connection": _("Could not connect to the email server."),
    "configuration": _("Check the email settings and encryption key."),
    "disabled": _("Email delivery is disabled."),
    "database": _("The recovery request could not be saved."),
    "queue_full": _("The email queue is full. Try again shortly."),
    "worker_start": _("The email worker could not start. Try again shortly."),
    "requester_limit": _("Too many recovery requests from this connection. Try again later."),
    "email_hour": _("The hourly recovery limit for this email address was reached."),
    "email_minute": _("A recovery email was requested for this address less than a minute ago."),
    "account_missing": _("No account has this email address."),
    "account_ambiguous": _("More than one account uses this email address."),
    "internal": _("An unexpected error prevented email delivery."),
}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_error(error_code):
    if error_code is None:
        return None
    return error_code if isinstance(error_code, str) and error_code in ERROR_LABELS else "internal"


def _report_failure():
    # No traceback: database errors can include bound recipient addresses.
    current_app.logger.warning("Password reset delivery diagnostics are unavailable.")


def record_reset_delivery(email, status="queued", error_code=None):
    """Record one validated request, returning an opaque ID or None on failure."""
    from .mail_settings import _valid_email

    if not isinstance(email, str) or len(email) > 120 or not _valid_email(email):
        return None
    if not isinstance(status, str) or status not in STATUS_LABELS:
        return None
    now = _now()
    delivery_id = uuid4().hex
    try:
        with Session(db.engine) as session:
            session.add(PasswordResetDelivery(
                id=delivery_id, created_at=now, updated_at=now,
                recipient_email=email.lower(), status=status,
                safe_error_code=_safe_error(error_code), attempts=0,
            ))
            session.flush()
            session.execute(delete(PasswordResetDelivery).where(
                PasswordResetDelivery.created_at < now - RETENTION,
            ))
            retained = session.scalars(select(PasswordResetDelivery.id).order_by(
                PasswordResetDelivery.created_at.desc(), PasswordResetDelivery.id.desc(),
            ).limit(MAX_RECORDS)).all()
            session.execute(delete(PasswordResetDelivery).where(
                PasswordResetDelivery.id.not_in(retained),
            ))
            session.commit()
        return delivery_id
    except Exception:
        _report_failure()
        return None


def update_reset_delivery(delivery_id, status, error_code=None, attempts=None):
    """Update fixed diagnostic fields without exposing or raising failures."""
    if not delivery_id or not isinstance(status, str) or status not in STATUS_LABELS:
        return False
    values = {"status": status, "safe_error_code": _safe_error(error_code), "updated_at": _now()}
    if attempts is not None:
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts <= 100:
            return False
        values["attempts"] = attempts
    try:
        with Session(db.engine) as session:
            result = session.execute(update(PasswordResetDelivery).where(
                PasswordResetDelivery.id == delivery_id,
            ).values(**values))
            session.commit()
            return result.rowcount == 1
    except Exception:
        _report_failure()
        return False


def recent_reset_deliveries(limit=20):
    """Return detached recent rows; unavailable diagnostics never block settings."""
    limit = min(max(limit, 1), 100) if isinstance(limit, int) else 20
    try:
        with Session(db.engine) as session:
            return session.scalars(select(PasswordResetDelivery).where(
                PasswordResetDelivery.created_at >= _now() - RETENTION,
            ).order_by(
                PasswordResetDelivery.created_at.desc(), PasswordResetDelivery.id.desc(),
            ).limit(limit)).all()
    except Exception:
        _report_failure()
        return []
