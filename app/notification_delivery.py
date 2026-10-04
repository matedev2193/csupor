"""Budapest-time scheduling and retryable, concurrency-safe SMTP delivery."""

from __future__ import annotations

import os
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import click
from flask import current_app
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from . import db
from .notification_models import LeaveNotification, MailBatch


BUDAPEST = ZoneInfo("Europe/Budapest")
CLAIM_LEASE = timedelta(minutes=10)
EVENT_LABELS = {
    "approved": "A távolléti igényt elfogadták.",
    "rejected": "A távolléti igényt elutasították.",
    "modified": "A távolléti igényt módosították.",
    "cancelled": "A távolléti igényt visszavonták.",
    "partially_approved": "A távolléti igény egyik jóváhagyása megtörtént.",
    "cancellation_requested": "A távolléti igény visszavonását kérték.",
    "cancellation_undone": "A visszavonási kérelmet visszavonták.",
    "cancellation_rejected": "A távolléti igény visszavonását elutasították.",
    "submitted": "Új távolléti igény érkezett.",
    "approval_policy_changed": "A távolléti igény jóváhagyásával kapcsolatban teendő keletkezett.",
    "reviewer_changed": "A távolléti igénnyel kapcsolatban teendőd van.",
}
STATUS_LABELS = {
    "pending approval": "Jóváhagyásra vár", "approved": "Elfogadva",
    "rejected": "Elutasítva", "pending cancellation": "Visszavonásra vár",
    "cancelled": "Visszavonva",
}


def _aware_utc(value=None):
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def notification_is_urgent(start_date: date, now=None) -> bool:
    """Leave starts at local midnight; an exactly 24-hour horizon is urgent."""
    start = datetime.combine(start_date, time.min, BUDAPEST).astimezone(timezone.utc)
    return start <= _aware_utc(now) + timedelta(hours=24)


def notification_due_at(start_date: date, now=None) -> datetime:
    now = _aware_utc(now)
    if notification_is_urgent(start_date, now):
        return now.replace(tzinfo=None)
    local_now = now.astimezone(BUDAPEST)
    due_day = local_now.date() + timedelta(days=local_now.time() > time(20))
    return datetime.combine(due_day, time(20), BUDAPEST).astimezone(timezone.utc).replace(tzinfo=None)


def _batch_key(notification):
    if notification.urgent:
        return f"urgent:{notification.recipient_id}:{notification.event_key}"
    day = _aware_utc(notification.due_at).astimezone(BUDAPEST).date()
    return f"digest:{notification.recipient_id}:{day.isoformat()}"


def _group_due_notifications(now):
    """Freeze batch membership before claiming, with uniqueness across workers."""
    rows = LeaveNotification.query.filter(
        LeaveNotification.status == "pending", LeaveNotification.batch_id.is_(None),
        LeaveNotification.due_at <= now,
    ).order_by(LeaveNotification.id).all()
    buckets = defaultdict(list)
    for row in rows:
        buckets[_batch_key(row)].append(row)
    grouped = 0
    for key, notifications in buckets.items():
        first = notifications[0]
        batch = MailBatch.query.filter_by(batch_key=key).with_for_update().first()
        if batch is None:
            try:
                with db.session.begin_nested():
                    batch = MailBatch(
                        id=uuid.uuid4().hex, batch_key=key, recipient_id=first.recipient_id,
                        kind="urgent" if first.urgent else "digest", created_at=now,
                        due_at=min(row.due_at for row in notifications),
                    )
                    db.session.add(batch)
                    db.session.flush()
            except IntegrityError:
                batch = MailBatch.query.filter_by(batch_key=key).with_for_update().one()
        ids = [row.id for row in notifications]
        unassigned = LeaveNotification.query.filter(
            LeaveNotification.id.in_(ids), LeaveNotification.batch_id.is_(None),
            LeaveNotification.status == "pending",
        )
        if batch.status == "pending" and batch.attempts == 0:
            grouped += unassigned.update({"batch_id": batch.id}, synchronize_session=False)
        elif batch.kind == "digest":
            # A transaction committed at the digest boundary after its batch was
            # frozen. Retain it for tomorrow instead of creating a second digest.
            day = _aware_utc(batch.due_at).astimezone(BUDAPEST).date() + timedelta(days=1)
            due = datetime.combine(day, time(20), BUDAPEST).astimezone(timezone.utc).replace(tzinfo=None)
            unassigned.update({"due_at": due}, synchronize_session=False)
        db.session.commit()
    return grouped


def _claimable(now):
    return and_(
        MailBatch.due_at <= now,
        or_(MailBatch.next_attempt_at.is_(None), MailBatch.next_attempt_at <= now),
        or_(MailBatch.status.in_(("pending", "failed")), and_(
            MailBatch.status == "sending", MailBatch.claimed_at < now - CLAIM_LEASE,
        )),
    )


def _claim_batch(batch_id, now):
    token = uuid.uuid4().hex
    updated = MailBatch.query.filter(MailBatch.id == batch_id, _claimable(now)).update({
        "status": "sending", "claim_token": token, "claimed_at": now,
        "attempts": MailBatch.attempts + 1,
    }, synchronize_session=False)
    db.session.commit()
    return token if updated else None


def _selected_rows(batch, now):
    from .notification_events import notification_is_actionable

    rows = LeaveNotification.query.filter_by(batch_id=batch.id, status="pending").order_by(
        LeaveNotification.created_at, LeaveNotification.id,
    ).all()
    recipient = batch.recipient
    selected, skipped = [], []
    latest_tasks = {}
    request_ids = {row.leave_request_id for row in rows if row.is_task}
    if request_ids:
        # A newer urgent notification may already have been delivered, or a
        # later digest may hold the current task. Never repeat its stale version.
        for request_id, notification_id in db.session.query(
            LeaveNotification.leave_request_id, LeaveNotification.id,
        ).filter(
            LeaveNotification.recipient_id == batch.recipient_id,
            LeaveNotification.leave_request_id.in_(request_ids),
            LeaveNotification.is_task.is_(True),
        ).order_by(LeaveNotification.created_at.desc(), LeaveNotification.id.desc()):
            latest_tasks.setdefault(request_id, notification_id)
    for row in rows:
        request = row.leave_request
        owner = bool(recipient and request and row.is_owner and request.user_id == recipient.id)
        task = bool(recipient and request and row.is_task and latest_tasks[row.leave_request_id] == row.id
                    and notification_is_actionable(row, recipient, now=_aware_utc(now)))
        if owner or task:
            selected.append((row, owner, task))
        else:
            skipped.append(row)
    return selected, skipped


def _period(payload):
    start = str(payload.get("start_date") or "–")
    end = payload.get("end_date")
    return start if end == start else f"{start} – {end or 'nyitott végű'}"


def _mail_content(batch, selected):
    recipient = batch.recipient
    name = (recipient.profile.full_name if recipient.profile else None) or recipient.username
    tasks, changes = [], []
    for row, owner, task in selected:
        payload = row.payload or {}
        # Render a strict allowlist, never a request note or its medical category.
        label = EVENT_LABELS.get(row.event_type, "A távolléti igény változott.")
        identity = f"#{row.leave_request_id} · {_period(payload)}"
        status = STATUS_LABELS.get(str(payload.get("status") or ""), "")
        if task:
            current_status = getattr(row.leave_request.status, "value", row.leave_request.status)
            task_label = "Visszavonás elbírálása szükséges." if current_status == "pending cancellation" else "Jóváhagyás szükséges."
            applicant = str(payload.get("applicant_name") or payload.get("employee_name") or "Munkavállaló")
            tasks.append(f"{applicant} · {identity}\n{task_label}")
        if owner:
            lines = [identity, label]
            previous_start, previous_end = payload.get("previous_start_date"), payload.get("previous_end_date")
            if previous_start and (previous_start != payload.get("start_date") or previous_end != payload.get("end_date")):
                lines.append("Korábbi időszak: " + _period({"start_date": previous_start, "end_date": previous_end}))
            if status:
                lines.append("Állapot: " + status)
            changes.append("\n".join(lines))
    subject = "CSUPOR – sürgős távolléti értesítés" if batch.kind == "urgent" else "CSUPOR – napi távolléti összesítő"
    intro = "A távolléti igényekkel kapcsolatos értesítéseid:"
    sections = []
    if tasks:
        sections.append(("Elintézendő feladatok", tasks, "/leaves/manage"))
    if changes:
        sections.append(("Saját távolléti igényeid", changes, "/leaves"))
    text_parts = [f"Kedves {name}!", intro]
    html_parts = ["<!doctype html><html lang=\"hu\"><body style=\"margin:0;background:#f4f6fb;font-family:Arial,sans-serif;color:#243247\">",
                  "<main style=\"max-width:640px;margin:24px auto;padding:28px;background:#fff;border-radius:16px\">",
                  "<p style=\"font-weight:bold;color:#3f5ab5;letter-spacing:2px\">CSUPOR</p>",
                  f"<h1 style=\"font-size:22px\">{escape(subject.split(' – ', 1)[1].capitalize())}</h1>",
                  f"<p>Kedves {escape(name)}!</p><p>{intro}</p>"]
    from .mail_settings import get_mail_settings
    settings = get_mail_settings()
    base_url = (getattr(settings, "base_url", None) or "").rstrip("/")
    for title, items, path in sections:
        text_parts.append(title + "\n\n" + "\n\n".join(items))
        html_parts.append(f"<h2 style=\"font-size:18px;margin-top:28px\">{title}</h2>")
        for item in items:
            html_parts.append("<p style=\"padding:14px;background:#f4f6fb;border-radius:8px;line-height:1.6\">" + escape(item).replace("\n", "<br>") + "</p>")
        if base_url:
            url = base_url + path
            text_parts.append("Megnyitás: " + url)
            html_parts.append(f'<p><a href="{escape(url, quote=True)}" style="color:#3f5ab5">Megnyitás a CSUPOR-ban</a></p>')
    footer = "Ez a CSUPOR automatikus értesítése. Az aktuális állapotot a felületen ellenőrizheted."
    text_parts.append(footer)
    html_parts.append(f'<p style="font-size:12px;color:#667085;margin-top:28px">{footer}</p></main></body></html>')
    return subject, "\n\n".join(text_parts), "".join(html_parts)


def _finish(batch_id, token, values):
    return MailBatch.query.filter_by(id=batch_id, claim_token=token, status="sending").update(values, synchronize_session=False)


def _deliver_claimed(batch_id, token, now):
    from .mail_settings import MailDeliveryError, send_email

    batch = db.session.get(MailBatch, batch_id, populate_existing=True)
    if not batch or batch.claim_token != token or batch.status != "sending":
        return "skipped"
    try:
        selected, skipped = _selected_rows(batch, now)
        if not selected:
            if _finish(batch.id, token, {"status": "skipped", "claim_token": None, "last_error": None}):
                for row in skipped:
                    row.status = "skipped"
            db.session.commit()
            return "skipped"
        subject, text_body, html_body = _mail_content(batch, selected)
        # Address is intentionally read at send time, never frozen in payloads.
        send_email(batch.recipient.email, subject, text_body, html_body,
                   message_id=f"<csupor-{batch.id}@notifications.csupor.invalid>")
        if _finish(batch.id, token, {"status": "sent", "sent_at": now, "claim_token": None,
                                     "next_attempt_at": None, "last_error": None}):
            for row, _, _ in selected:
                row.status = "sent"
            for row in skipped:
                row.status = "skipped"
        db.session.commit()
        return "sent"
    except Exception as error:
        db.session.rollback()
        # Never persist/log a raw SMTP exception: it may contain credentials,
        # server responses, addresses, or the message body.
        safe_codes = {"disabled", "configuration", "authentication", "connection", "delivery"}
        code = getattr(error, "code", None) if isinstance(error, MailDeliveryError) else None
        code = code if code in safe_codes else "internal"
        fresh = db.session.get(MailBatch, batch_id, populate_existing=True)
        attempts = fresh.attempts if fresh else 1
        delay = timedelta(minutes=min(360, 5 * 2 ** min(attempts - 1, 7)))
        _finish(batch_id, token, {"status": "failed", "claim_token": None,
                                 "next_attempt_at": now + delay, "last_error": code})
        db.session.commit()
        current_app.logger.warning("Leave notification batch delivery failed (%s).", code)
        return "failed"


def process_due_notifications(app=None, now=None):
    """Send due batches; safe for a daemon, CLI/cron, and multiple processes."""
    if app is not None:
        with app.app_context():
            return process_due_notifications(now=now)
    from .mail_settings import is_mail_enabled

    result = {"grouped": 0, "sent": 0, "skipped": 0, "failed": 0, "disabled": False}
    if not is_mail_enabled():
        result["disabled"] = True
        return result
    now = _aware_utc(now).replace(tzinfo=None)
    from .notification_events import reconcile_pending_leave_tasks

    # Future-dated leadership appointments become effective without a web
    # request. Discover newly eligible reviewers on the worker's regular poll.
    reconcile_pending_leave_tasks(now=_aware_utc(now))
    db.session.commit()
    result["grouped"] = _group_due_notifications(now)
    ids = [row[0] for row in db.session.query(MailBatch.id).filter(_claimable(now)).order_by(MailBatch.due_at, MailBatch.id).all()]
    db.session.commit()
    for batch_id in ids:
        token = _claim_batch(batch_id, now)
        if token:
            result[_deliver_claimed(batch_id, token, now)] += 1
    return result


def _worker_loop(app, wake):
    while True:
        try:
            process_due_notifications(app)
        except Exception:
            # No raw exception/traceback: delivery must not leak email content.
            app.logger.error("Leave notification worker could not process the queue.")
        wake.wait(timeout=30)
        wake.clear()


def wake_notifications(app=None):
    """Wake a local daemon after a committed request without doing SMTP in it."""
    app = app or current_app._get_current_object()
    if not app.config.get("EMAIL_WORKER_ENABLED", False):
        return
    try:
        state = app.extensions["leave_notification_worker"]
        with state["lock"]:
            if state.get("pid") != os.getpid() or not state.get("thread") or not state["thread"].is_alive():
                state["pid"] = os.getpid()
                state["wake"] = threading.Event()
                state["thread"] = threading.Thread(target=_worker_loop, args=(app, state["wake"]),
                                                   name="csupor-leave-mail", daemon=True)
                state["thread"].start()
            state["wake"].set()
    except Exception:
        # The business transaction has already committed. A worker-start failure
        # must not turn a successful leave action into a HTTP error.
        app.logger.error("Leave notification worker could not be started; queued mail is retained.")


def init_notifications(app):
    app.extensions["leave_notification_worker"] = {"lock": threading.Lock()}
    if hasattr(os, "register_at_fork"):
        def reset_worker_after_fork():
            # Gunicorn --preload forks while the parent may hold the old lock.
            # Replace it without acquiring it; only the calling thread survives.
            app.extensions["leave_notification_worker"] = {"lock": threading.Lock()}
            # Preloading also inherits connection pools created by startup or
            # the parent's mail worker. Replace the child pools without closing
            # the parent's sockets. A fresh context avoids touching any session
            # inherited from the parent's in-flight notification transaction.
            with app.app_context():
                for engine in db.engines.values():
                    engine.dispose(close=False)

        os.register_at_fork(after_in_child=reset_worker_after_fork)

    @app.cli.group("notifications")
    def notifications_cli():
        """Process durable leave email notifications."""

    @notifications_cli.command("send-due")
    def send_due():
        """Send due immediate notifications and Budapest 20:00 digests once."""
        click.echo(process_due_notifications())

    @notifications_cli.command("worker")
    def worker():
        """Run continuously; may safely run beside web workers or cron."""
        _worker_loop(app, threading.Event())

    @app.after_request
    def notify_after_request(response):
        wake_notifications(app)
        return response

    wake_notifications(app)
