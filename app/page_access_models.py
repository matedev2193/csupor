"""Explicit role-to-page access rules and a versioned settings singleton."""

from uuid import uuid4

from . import db


class PageAccessSettings(db.Model):
    __tablename__ = "page_access_settings"
    __table_args__ = (db.CheckConstraint("id = 1", name="single_page_access_settings"),)

    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    revision = db.Column(db.String(32), nullable=False, default=lambda: uuid4().hex)
    updated_at = db.Column(db.DateTime, nullable=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_by = db.relationship("User")

    __mapper_args__ = {
        "version_id_col": revision,
        "version_id_generator": lambda previous: uuid4().hex,
    }


class PageRolePermission(db.Model):
    __tablename__ = "page_role_permissions"
    __table_args__ = (
        db.CheckConstraint("`role` IN ('employee', 'hr', 'ceo', 'developer')", name="page_permission_role"),
    )

    page_key = db.Column(db.String(64), primary_key=True)
    role = db.Column(db.String(20), primary_key=True)
    allowed = db.Column(db.Boolean, nullable=False, default=False)
