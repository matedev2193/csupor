"""Unified qualification records and their durable, private supporting files."""

from datetime import datetime, timezone

from sqlalchemy.dialects.mysql import MEDIUMBLOB

from . import db


def utc_now():
    """Use naive UTC consistently with the application's database timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class QualificationRecord(db.Model):
    __tablename__ = "qualification_records"
    __table_args__ = (
        db.UniqueConstraint("legacy_source", "legacy_id", name="uq_qualification_record_legacy"),
        db.CheckConstraint("status IN ('uploaded', 'processed')", name="qualification_record_status"),
        db.Index("ix_qualification_records_status_user_id", "status", "user_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="uploaded", server_default="uploaded")
    kind = db.Column(db.String(40), nullable=True)
    qualification_name = db.Column(db.String(255), nullable=True)
    level_or_type = db.Column(db.String(120), nullable=True)
    institution_name = db.Column(db.String(255), nullable=True)
    degree_number = db.Column(db.String(120), nullable=True)
    year_obtained = db.Column(db.Integer, nullable=True)
    date_obtained = db.Column(db.Date, nullable=True)
    highest = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    completion_state = db.Column(db.String(20), nullable=True)
    study_start_date = db.Column(db.Date, nullable=True)
    study_end_date = db.Column(db.Date, nullable=True)
    study_categories = db.Column(db.JSON, nullable=True)
    award_categories = db.Column(db.JSON, nullable=True)
    training_topic = db.Column(db.String(40), nullable=True)
    organiser_type = db.Column(db.String(40), nullable=True)
    funding_type = db.Column(db.String(40), nullable=True)
    attendance_mode = db.Column(db.String(40), nullable=True)
    duration_hours = db.Column(db.Numeric(8, 2), nullable=True)
    credits = db.Column(db.Numeric(8, 2), nullable=True)
    digital_pedagogy = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now, onupdate=utc_now)
    processed_at = db.Column(db.DateTime, nullable=True)
    processed_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revision = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    legacy_source = db.Column(db.String(40), nullable=True)
    legacy_id = db.Column(db.Integer, nullable=True)

    user = db.relationship("User", back_populates="qualification_records", foreign_keys=[user_id])
    processed_by = db.relationship("User", foreign_keys=[processed_by_id])
    documents = db.relationship("QualificationDocument", back_populates="record", cascade="all, delete-orphan")

    __mapper_args__ = {"version_id_col": revision}


class QualificationDocument(db.Model):
    __tablename__ = "qualification_documents"
    __table_args__ = (
        db.CheckConstraint("size_bytes > 0 AND size_bytes <= 10485760", name="qualification_document_size"),
    )

    id = db.Column(db.Integer, primary_key=True)
    record_id = db.Column(db.Integer, db.ForeignKey("qualification_records.id", ondelete="CASCADE"), nullable=False, unique=True)
    filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(80), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    data = db.deferred(db.Column(db.LargeBinary().with_variant(MEDIUMBLOB(), "mysql", "mariadb"), nullable=False))
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    record = db.relationship("QualificationRecord", back_populates="documents")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id])
