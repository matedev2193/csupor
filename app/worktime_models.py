"""Dated group assignments and durable monthly working-time registers."""

from sqlalchemy import false

from . import db


class WorkGroup(db.Model):
    __tablename__ = "work_groups"
    __table_args__ = (
        db.UniqueConstraint("place_of_work_id", "name", name="uq_work_group_name"),
        db.CheckConstraint("end_date IS NULL OR end_date >= start_date", name="work_group_dates"),
    )
    id = db.Column(db.Integer, primary_key=True)
    place_of_work_id = db.Column(db.Integer, db.ForeignKey("places_of_work.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=True)
    place_of_work = db.relationship("PlaceOfWork")


class WorkAssignment(db.Model):
    __tablename__ = "work_assignments"
    __table_args__ = (
        db.CheckConstraint("shift_phase IN (0, 1)", name="work_assignment_phase"),
        db.CheckConstraint("end_date IS NULL OR end_date >= start_date", name="work_assignment_dates"),
    )
    id = db.Column(db.Integer, primary_key=True)
    contract_id = db.Column(db.Integer, db.ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = db.Column(db.Integer, db.ForeignKey("work_groups.id", ondelete="CASCADE"), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=True)
    shift_phase = db.Column(db.Integer, nullable=False, default=0)
    flexible_shift = db.Column(db.Boolean, nullable=False, default=False, server_default=false())
    contract = db.relationship("Contract", backref=db.backref("work_assignments", cascade="all, delete-orphan"))
    group = db.relationship("WorkGroup")


class WorkGroupMerge(db.Model):
    __tablename__ = "work_group_merges"
    __table_args__ = (
        db.UniqueConstraint("day", "source_group_id", name="uq_work_group_merge_day"),
        db.CheckConstraint("source_group_id != target_group_id", name="work_merge_distinct_groups"),
    )
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False, index=True)
    source_group_id = db.Column(db.Integer, db.ForeignKey("work_groups.id", ondelete="CASCADE"), nullable=False)
    target_group_id = db.Column(db.Integer, db.ForeignKey("work_groups.id", ondelete="CASCADE"), nullable=False)
    note = db.Column(db.String(255), nullable=False, default="")
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    source_group = db.relationship("WorkGroup", foreign_keys=[source_group_id])
    target_group = db.relationship("WorkGroup", foreign_keys=[target_group_id])


class WorkSchedule(db.Model):
    __tablename__ = "work_schedules"
    __table_args__ = (
        db.UniqueConstraint("place_of_work_id", "year", "month", name="uq_work_schedule_month"),
        db.CheckConstraint("month BETWEEN 1 AND 12 AND year BETWEEN 1970 AND 2100", name="work_schedule_period"),
        db.CheckConstraint("status IN ('draft', 'confirmed')", name="work_schedule_status"),
    )
    id = db.Column(db.Integer, primary_key=True)
    place_of_work_id = db.Column(db.Integer, db.ForeignKey("places_of_work.id", ondelete="CASCADE"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    revision = db.Column(db.String(32), nullable=False)
    source_hash = db.Column(db.String(64), nullable=False)
    issues = db.Column(db.JSON, nullable=False, default=list)
    status = db.Column(db.String(20), nullable=False, default="draft")
    generated_at = db.Column(db.DateTime, nullable=False)
    generated_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    confirmed_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    place_of_work = db.relationship("PlaceOfWork")
    entries = db.relationship("WorkTimeEntry", back_populates="schedule", cascade="all, delete-orphan")


class WorkTimeEntry(db.Model):
    __tablename__ = "work_time_entries"
    __table_args__ = (
        db.UniqueConstraint("schedule_id", "contract_id", "day", name="uq_work_time_entry_day"),
        db.CheckConstraint("work_minutes BETWEEN 0 AND 480", name="work_entry_minutes"),
        db.CheckConstraint("teaching_minutes >= 0 AND teaching_minutes <= work_minutes", name="work_entry_teaching"),
        db.CheckConstraint("break_minutes IN (0, 20)", name="work_entry_break"),
    )
    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("work_schedules.id", ondelete="CASCADE"), nullable=False, index=True)
    contract_id = db.Column(db.Integer, db.ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = db.Column(db.Integer, db.ForeignKey("work_groups.id", ondelete="SET NULL"), nullable=True)
    day = db.Column(db.Date, nullable=False, index=True)
    start_minute = db.Column(db.Integer, nullable=True)
    end_minute = db.Column(db.Integer, nullable=True)
    break_start = db.Column(db.Integer, nullable=True)
    break_minutes = db.Column(db.Integer, nullable=False, default=0)
    work_minutes = db.Column(db.Integer, nullable=False, default=0)
    teaching_minutes = db.Column(db.Integer, nullable=False, default=0)
    shift = db.Column(db.String(24), nullable=False)
    note = db.Column(db.Text, nullable=False, default="")
    note_parts = db.Column(db.JSON, nullable=False, default=list)
    is_manual = db.Column(db.Boolean, nullable=False, default=False)
    schedule = db.relationship("WorkSchedule", back_populates="entries")
    contract = db.relationship("Contract", backref=db.backref("work_time_entries", cascade="all, delete-orphan"))
    user = db.relationship("User", backref=db.backref("work_time_entries", cascade="all, delete-orphan"))
    group = db.relationship("WorkGroup")
