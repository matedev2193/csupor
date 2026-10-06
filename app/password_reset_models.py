"""Short-lived account recovery secrets and shared request limits."""

from . import db


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    # Only a SHA-256 digest is stored. The random secret lives in the email.
    token_hash = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    credentials_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime, nullable=True)


class PasswordResetThrottle(db.Model):
    __tablename__ = "password_reset_throttles"

    # HMACs keep email addresses and requester addresses out of this table.
    key_hash = db.Column(db.String(64), primary_key=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    request_count = db.Column(db.Integer, nullable=False)
