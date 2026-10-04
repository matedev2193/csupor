"""Durable transactional outbox for leave notifications.

All stored timestamps are naive UTC; local scheduling is deliberately confined
to notification_delivery. Notification content excludes leave notes/categories.
"""

from datetime import datetime, timezone

from . import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MailBatch(db.Model):
    __tablename__ = "mail_batches"
    __table_args__ = (
        db.Index("ix_mail_batches_dispatch", "status", "due_at", "next_attempt_at"),
    )

    id = db.Column(db.String(32), primary_key=True)
    batch_key = db.Column(db.String(120), nullable=False, unique=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind = db.Column(db.String(12), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    due_at = db.Column(db.DateTime, nullable=False)
    sent_at = db.Column(db.DateTime, nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    next_attempt_at = db.Column(db.DateTime, nullable=True)
    claim_token = db.Column(db.String(32), nullable=True)
    status = db.Column(db.String(12), nullable=False, default="pending")
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(80), nullable=True)
    recipient = db.relationship("User", backref=db.backref("mail_batches", cascade="all, delete-orphan"))


class LeaveNotification(db.Model):
    __tablename__ = "leave_notifications"
    __table_args__ = (
        db.UniqueConstraint("event_key", "recipient_id", name="uq_leave_notification_event_recipient"),
        db.Index("ix_leave_notifications_dispatch", "status", "batch_id", "due_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_key = db.Column(db.String(32), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    leave_request_id = db.Column(db.Integer, db.ForeignKey("leave_requests.id", ondelete="CASCADE"), nullable=False)
    event_type = db.Column(db.String(40), nullable=False)
    payload = db.Column(db.JSON, nullable=False)
    is_task = db.Column(db.Boolean, nullable=False, default=False)
    is_owner = db.Column(db.Boolean, nullable=False, default=False)
    urgent = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    due_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(12), nullable=False, default="pending")
    batch_id = db.Column(db.String(32), db.ForeignKey("mail_batches.id", ondelete="SET NULL"), nullable=True, index=True)
    recipient = db.relationship("User", backref=db.backref("leave_notifications", cascade="all, delete-orphan"))
    leave_request = db.relationship("LeaveRequest", backref=db.backref("email_notifications", cascade="all, delete-orphan"))
    batch = db.relationship("MailBatch", backref="notifications")
