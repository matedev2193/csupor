"""Singleton SMTP configuration; credentials are always encrypted at rest."""

from uuid import uuid4

from . import db


class MailServerSettings(db.Model):
    __tablename__ = "mail_server_settings"
    __table_args__ = (
        db.CheckConstraint("id = 1", name="single_mail_server_settings"),
        db.CheckConstraint("port BETWEEN 1 AND 65535", name="mail_server_port"),
        db.CheckConstraint("security IN ('starttls', 'ssl', 'none')", name="mail_server_security"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    host = db.Column(db.String(255), nullable=False, default="")
    port = db.Column(db.Integer, nullable=False, default=587)
    security = db.Column(db.String(10), nullable=False, default="starttls")
    username = db.Column(db.String(255), nullable=False, default="")
    encrypted_password = db.Column(db.Text, nullable=False, default="")
    sender_email = db.Column(db.String(254), nullable=False, default="")
    sender_name = db.Column(db.String(120), nullable=False, default="CSUPOR")
    base_url = db.Column(db.String(500), nullable=False, default="")
    revision = db.Column(db.String(32), nullable=False, default=lambda: uuid4().hex)
    updated_at = db.Column(db.DateTime, nullable=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_by = db.relationship("User")

    __mapper_args__ = {
        "version_id_col": revision,
        "version_id_generator": lambda previous: uuid4().hex,
    }
