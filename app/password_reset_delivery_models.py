"""Short-lived, private recovery-email diagnostics without recovery secrets."""

from zoneinfo import ZoneInfo

from . import db


class PasswordResetDelivery(db.Model):
    __tablename__ = "password_reset_deliveries"

    id = db.Column(db.String(32), primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, nullable=False)
    recipient_email = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(32), nullable=False)
    safe_error_code = db.Column(db.String(32), nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)

    @property
    def local_created_at(self):
        return self.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Budapest"))
