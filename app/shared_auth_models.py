"""Local application memberships and server-side sessions for shared accounts."""

from . import db


class ExternalIdentity(db.Model):
    __tablename__ = "external_identities"
    __table_args__ = (db.UniqueConstraint("issuer", "subject", name="uq_external_identity"),)

    id = db.Column(db.Integer, primary_key=True)
    issuer = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, unique=True)
    disabled = db.Column(db.Boolean, nullable=False, default=False)
    user = db.relationship("User", foreign_keys=[user_id])


class ExternalLoginSession(db.Model):
    __tablename__ = "external_login_sessions"

    id_hash = db.Column(db.String(64), primary_key=True)
    identity_id = db.Column(db.Integer, db.ForeignKey("external_identities.id", ondelete="CASCADE"), nullable=False, index=True)
    # Encrypted at rest; the browser contains only an independent random handle.
    access_token = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    authenticated_at = db.Column(db.DateTime, nullable=False)
    deletion_target_id = db.Column(db.Integer, nullable=True)
    deletion_expires_at = db.Column(db.DateTime, nullable=True)
    identity = db.relationship("ExternalIdentity")
