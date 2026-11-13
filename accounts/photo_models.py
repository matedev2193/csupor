"""Private account photo storage; no application-specific models are imported."""
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from .models import db


class AccountPhoto(db.Model):
    __tablename__ = 'account_profile_photos'
    __table_args__ = (
        db.CheckConstraint('width BETWEEN 1 AND 384 AND height BETWEEN 1 AND 384', name='account_photo_dimensions'),
        db.CheckConstraint('size_bytes > 0 AND size_bytes <= 1048576', name='account_photo_size'),
    )
    account_subject = db.Column(db.String(36), db.ForeignKey('accounts.subject', ondelete='CASCADE'), primary_key=True)
    version = db.Column(db.String(32), nullable=False)
    mime_type = db.Column(db.String(30), nullable=False, default='image/jpeg')
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False)
    data = db.deferred(db.Column(db.LargeBinary().with_variant(MEDIUMBLOB(), 'mysql', 'mariadb'), nullable=False))


class AccountPhotoSource(db.Model):
    __tablename__ = 'account_profile_photo_sources'
    __table_args__ = (
        db.CheckConstraint('width BETWEEN 1 AND 2048 AND height BETWEEN 1 AND 2048', name='account_photo_source_dimensions'),
        db.CheckConstraint('size_bytes > 0 AND size_bytes <= 8388608', name='account_photo_source_size'),
        db.CheckConstraint('center_x BETWEEN 0 AND 1 AND center_y BETWEEN 0 AND 1 AND zoom BETWEEN 1 AND 8', name='account_photo_source_crop'),
    )
    account_subject = db.Column(db.String(36), db.ForeignKey('accounts.subject', ondelete='CASCADE'), primary_key=True)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    center_x = db.Column(db.Double, nullable=False, default=0.5)
    center_y = db.Column(db.Double, nullable=False, default=0.5)
    zoom = db.Column(db.Double, nullable=False, default=1.0)
    data = db.deferred(db.Column(db.LargeBinary().with_variant(MEDIUMBLOB(), 'mysql', 'mariadb'), nullable=False))
