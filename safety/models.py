"""Independent training records; no imports or foreign keys into CSUPOR."""
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


db = SQLAlchemy()


class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    issuer = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False, default='')
    role = db.Column(db.String(16), nullable=False, default='employee')
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now)
    __table_args__ = (db.UniqueConstraint('issuer', 'subject'),)


class LoginSession(db.Model):
    id_hash = db.Column(db.String(64), primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    access_token = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    member = db.relationship(Member)


class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(16), nullable=False)  # fire / work
    revision = db.Column(db.Integer, nullable=False, default=1)
    title = db.Column(db.String(160), nullable=False)
    mode = db.Column(db.String(16), nullable=False, default='accept')
    status = db.Column(db.String(16), nullable=False, default='draft')
    passing_percent = db.Column(db.Integer, nullable=False, default=80)
    pdf_key = db.Column(db.String(80))
    pdf_name = db.Column(db.String(255))
    pdf_sha256 = db.Column(db.String(64))
    page_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=now)
    published_at = db.Column(db.DateTime)
    questions = db.relationship('Question', order_by='Question.position', cascade='all, delete-orphan')
    __table_args__ = (db.UniqueConstraint('year', 'kind', 'revision'),)


class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    position = db.Column(db.Integer, nullable=False)
    prompt = db.Column(db.Text, nullable=False)
    options = db.Column(db.JSON, nullable=False)  # ordered list of 2..6 strings
    correct_index = db.Column(db.Integer, nullable=False)


class Progress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    last_page = db.Column(db.Integer, nullable=False, default=0)
    furthest_page = db.Column(db.Integer, nullable=False, default=0)
    visited_pages = db.Column(db.JSON, nullable=False, default=list)
    active_seconds = db.Column(db.Integer, nullable=False, default=0)
    last_seen_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    completion_kind = db.Column(db.String(16))
    accepted_text = db.Column(db.Text)
    course = db.relationship(Course)
    member = db.relationship(Member)
    __table_args__ = (db.UniqueConstraint('course_id', 'member_id'),)


class ReadingSession(db.Model):
    id_hash = db.Column(db.String(64), primary_key=True)
    progress_id = db.Column(db.Integer, db.ForeignKey('progress.id'), nullable=False)
    last_ping_at = db.Column(db.DateTime, nullable=False, default=now)
    expires_at = db.Column(db.DateTime, nullable=False)
    sequence = db.Column(db.Integer, nullable=False, default=0)
    page = db.Column(db.Integer, nullable=False, default=1)
    progress = db.relationship(Progress)


class Attempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    progress_id = db.Column(db.Integer, db.ForeignKey('progress.id'), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now)
    score = db.Column(db.Integer, nullable=False)
    total = db.Column(db.Integer, nullable=False)
    passed = db.Column(db.Boolean, nullable=False)
    answers = db.Column(db.JSON, nullable=False)
    progress = db.relationship(Progress)
