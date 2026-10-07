"""Employee evidence uploads and HR/director qualification processing."""

import re
import warnings
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from hmac import compare_digest
from io import BytesIO
from pathlib import PurePath
from secrets import token_urlsafe

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required
from PIL import Image, UnidentifiedImageError
from sqlalchemy import or_, select, update
from sqlalchemy.orm import joinedload, selectinload, undefer
from werkzeug.datastructures import MultiDict
from werkzeug.utils import secure_filename

from . import db
from .models import User, UserProfile
from .page_access import can_access_page
from .people import local_today
from .qualification_models import QualificationDocument, QualificationRecord
from .qualification_taxonomy import (
    ATTENDANCE_MODES, AWARD_CATEGORIES, COMPLETION_STATES, FUNDING_TYPES,
    KINDS, ORGANISER_TYPES, STUDY_CATEGORIES, TRAINING_TOPICS, taxonomy_context,
)

qualifications = Blueprint("qualifications", __name__)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_SIDE = 10_000
MIME_TYPES = {
    ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}
IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
TEXT_LIMITS = {
    "qualification_name": 255, "level_or_type": 120, "institution_name": 255,
    "degree_number": 120, "notes": 10000,
}
ENUM_FIELDS = {
    "kind": KINDS, "completion_state": COMPLETION_STATES,
    "training_topic": TRAINING_TOPICS, "organiser_type": ORGANISER_TYPES,
    "funding_type": FUNDING_TYPES, "attendance_mode": ATTENDANCE_MODES,
}
SCALAR_FIELDS = tuple(TEXT_LIMITS) + tuple(ENUM_FIELDS) + (
    "year_obtained", "date_obtained", "study_start_date", "study_end_date",
    "duration_hours", "credits", "highest", "digital_pedagogy", "action", "revision", "user_id",
)


class QualificationValidationError(ValueError):
    """A safe, translated message for invalid document evidence."""


def _is_manager():
    return getattr(current_user.privilege, "value", current_user.privilege) in {"hr", "ceo"}


def manager_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not _is_manager() or not can_access_page("qualifications.manage"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _token():
    return session.setdefault("qualification_csrf_token", token_urlsafe(32))


def _check_csrf():
    saved = session.get("qualification_csrf_token")
    supplied = request.form.get("csrf_token", "")
    if not saved or len(request.form.getlist("csrf_token")) != 1 or not compare_digest(saved.encode(), supplied.encode()):
        abort(400)


def _context():
    return {**taxonomy_context(), "qualification_csrf_token": _token(),
            "latest_date": local_today().isoformat(), "current_year": local_today().year,
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024)}


def validate_document(document):
    """Read at most 10 MiB, and verify the actual file format before storing it."""
    if document is None or not document.filename:
        raise QualificationValidationError(_("Choose a qualification document to upload."))
    filename = secure_filename(document.filename)
    extension = PurePath(filename).suffix.lower()
    if extension not in MIME_TYPES:
        raise QualificationValidationError(_("Upload a PDF, JPEG, PNG or WebP document."))
    data = document.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise QualificationValidationError(_("The qualification document must be no larger than 10 MB."))
    invalid = _("The document is empty, damaged or does not match its file type.")
    if not data:
        raise QualificationValidationError(invalid)
    if extension == ".pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
            raise QualificationValidationError(invalid)
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    if image.format != IMAGE_FORMATS[extension]:
                        raise QualificationValidationError(invalid)
                    if image.width * image.height > MAX_IMAGE_PIXELS or max(image.size) > MAX_IMAGE_SIDE:
                        raise QualificationValidationError(_("Use an image up to 20 megapixels and 10,000 pixels per side."))
                    image.verify()
                with Image.open(BytesIO(data)) as image:
                    image.load()
        except QualificationValidationError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise QualificationValidationError(_("Use an image up to 20 megapixels and 10,000 pixels per side.")) from None
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
            raise QualificationValidationError(invalid) from None
    if len(filename) > 255:
        filename = filename[:255 - len(extension)] + extension
    return filename, MIME_TYPES[extension], data


def parse_metadata(form, *, require_complete=True, today=None):
    """Validate all editable fields without mutating a database record."""
    today = today or local_today()
    values, errors = {}, []
    if any(len(form.getlist(field)) > 1 for field in SCALAR_FIELDS):
        return {}, [_("Submit each qualification field only once.")]
    for field, limit in TEXT_LIMITS.items():
        value = form.get(field, "").strip()
        if len(value) > limit:
            errors.append(_("A qualification text field exceeds its maximum length."))
        values[field] = value or None
    for field, choices in ENUM_FIELDS.items():
        value = form.get(field, "").strip()
        if value and value not in choices:
            errors.append(_("Choose a valid qualification classification."))
        values[field] = value or None
    if require_complete and (not values["qualification_name"] or not values["kind"] or not values["completion_state"]):
        errors.append(_("Enter the qualification name, type and completion status before processing it."))

    for field in ("date_obtained", "study_start_date", "study_end_date"):
        raw = form.get(field, "").strip()
        values[field] = None
        if raw:
            try:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
                    raise ValueError
                parsed = date.fromisoformat(raw)
                if not date(1900, 1, 1) <= parsed <= today:
                    raise ValueError
                values[field] = parsed
            except ValueError:
                errors.append(_("Enter valid dates between 1 January 1900 and today."))
    raw_year = form.get("year_obtained", "").strip()
    values["year_obtained"] = None
    if raw_year:
        if not re.fullmatch(r"[0-9]{4}", raw_year) or not 1900 <= int(raw_year) <= today.year:
            errors.append(_("Enter a valid year of completion between 1900 and the current year."))
        else:
            values["year_obtained"] = int(raw_year)
    if values["date_obtained"]:
        if values["year_obtained"] and values["year_obtained"] != values["date_obtained"].year:
            errors.append(_("The completion year must match the completion date."))
        values["year_obtained"] = values["date_obtained"].year

    for field, choices in (("study_categories", STUDY_CATEGORIES), ("award_categories", AWARD_CATEGORIES)):
        selected = set(form.getlist(field))
        if not selected.issubset(choices):
            errors.append(_("Choose a valid KSH classification."))
        values[field] = [key for key in choices if key in selected]
    awards = set(values["award_categories"])
    if "ecdl" in awards:
        awards.add("it")
    if awards.intersection({"leadership_university", "leadership_college", "public_education_leadership"}):
        awards.add("leadership")
    values["award_categories"] = [key for key in AWARD_CATEGORIES if key in awards]

    for field in ("duration_hours", "credits"):
        raw = form.get(field, "").strip().replace(",", ".")
        values[field] = None
        if raw:
            try:
                number = Decimal(raw)
                if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", raw) or not number.is_finite() or not Decimal("0") < number <= Decimal("999999.99"):
                    raise InvalidOperation
                values[field] = number
            except InvalidOperation:
                errors.append(_("Enter positive hours and credits with no more than two decimal places."))
    for field in ("highest", "digital_pedagogy"):
        raw = form.get(field, "")
        if raw not in {"", "1", "on"}:
            errors.append(_("Choose a valid checkbox value."))
        values[field] = raw in {"1", "on"}

    state = values["completion_state"]
    if state == "completed":
        if require_complete and not values["year_obtained"]:
            errors.append(_("Enter the year or exact date when the qualification was obtained."))
    elif state in {"in_progress", "discontinued"}:
        if values["year_obtained"] or values["date_obtained"] or values["award_categories"] or values["highest"]:
            errors.append(_("Uncompleted studies cannot have a completion date, award classification or highest-qualification marker."))
    elif values["year_obtained"] or values["date_obtained"] or values["award_categories"] or values["highest"]:
        errors.append(_("Select Completed before recording an award or completion date."))
    start, end = values["study_start_date"], values["study_end_date"]
    if end and not start:
        errors.append(_("Enter the study start date when recording a study end date."))
    if start and end and end < start:
        errors.append(_("The study end date cannot precede the start date."))
    if start and values["date_obtained"] and values["date_obtained"] < start:
        errors.append(_("The completion date cannot precede the study start date."))
    if start and values["year_obtained"] and values["year_obtained"] < start.year:
        errors.append(_("The completion year cannot precede the study start year."))
    if state == "in_progress" and end:
        errors.append(_("Ongoing studies cannot have an actual study end date."))
    if values["study_categories"] and (not start or (state != "in_progress" and not end)):
        errors.append(_("Enter the study period for participation statistics; ongoing studies need a start date."))
    if start and state in {"completed", "discontinued"} and not end:
        errors.append(_("Enter the study end date for completed or discontinued studies."))
    return values, list(dict.fromkeys(errors))


def _record_form(record):
    if record is None:
        return MultiDict({"kind": "qualification", "completion_state": "completed"})
    form = MultiDict()
    for field in SCALAR_FIELDS:
        if not hasattr(record, field):
            continue
        value = getattr(record, field)
        if isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, bool):
            value = "1" if value else ""
        elif value is not None:
            value = str(value)
        form[field] = value or ""
    for field in ("study_categories", "award_categories"):
        form.setlist(field, getattr(record, field) or [])
    return form


def _employees():
    return User.query.outerjoin(UserProfile).options(joinedload(User.profile)).order_by(
        UserProfile.full_name, User.username, User.id,
    ).all()


def _positive_id(raw):
    if not raw or not re.fullmatch(r"[0-9]{1,10}", str(raw)) or int(raw) < 1:
        return None
    return int(raw)


def _page_number():
    raw = request.args.get("page", "1")
    page = _positive_id(raw)
    if page is None:
        abort(400)
    return page


@qualifications.route("/qualifications", methods=["GET", "POST"])
@login_required
def index():
    if not can_access_page("add_qualification"):
        abort(403)
    if request.method == "POST":
        _check_csrf()
        if len(request.files.getlist("document")) != 1:
            flash(_("Choose one qualification document to upload."), "error")
            return redirect(url_for("qualifications.index"))
        try:
            filename, mime_type, data = validate_document(request.files.get("document"))
        except QualificationValidationError as error:
            flash(str(error), "error")
            return redirect(url_for("qualifications.index"))
        record = QualificationRecord(user_id=current_user.id, status="uploaded")
        record.documents.append(QualificationDocument(
            filename=filename, mime_type=mime_type, size_bytes=len(data),
            data=data, uploaded_by_id=current_user.id,
        ))
        db.session.add(record)
        db.session.commit()
        flash(_("Document uploaded. HR or a director will record the qualification details."), "success")
        return redirect(url_for("qualifications.index"))
    pagination = QualificationRecord.query.filter_by(user_id=current_user.id).options(
        selectinload(QualificationRecord.documents),
    ).order_by(QualificationRecord.created_at.desc(), QualificationRecord.id.desc()).paginate(
        page=_page_number(), per_page=50, error_out=False,
    )
    return render_template("qualifications.html", records=pagination.items, pagination=pagination, **_context())


@qualifications.route("/qualifications/manage")
@manager_required
def manage():
    status = request.args.get("status", "uploaded")
    if status not in {"uploaded", "processed", "all"}:
        abort(400)
    raw_user = request.args.get("user_id", "")
    user_id = _positive_id(raw_user) if raw_user else None
    if raw_user and user_id is None:
        abort(400)
    query_text = request.args.get("q", "").strip()
    if len(query_text) > 120:
        abort(400)
    query = QualificationRecord.query.join(User, QualificationRecord.user_id == User.id).outerjoin(UserProfile).options(
        joinedload(QualificationRecord.user).joinedload(User.profile),
        selectinload(QualificationRecord.documents),
    )
    if status != "all":
        query = query.filter(QualificationRecord.status == status)
    if user_id:
        query = query.filter(QualificationRecord.user_id == user_id)
    if query_text:
        escaped = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(or_(UserProfile.full_name.ilike(f"%{escaped}%", escape="\\"), User.username.ilike(f"%{escaped}%", escape="\\")))
    pagination = query.order_by(QualificationRecord.created_at.desc(), QualificationRecord.id.desc()).paginate(
        page=_page_number(), per_page=50, error_out=False,
    )
    return render_template("manage_qualifications.html", records=pagination.items, pagination=pagination,
                           employees=_employees(), filters={"status": status, "user_id": user_id, "q": query_text}, **_context())


def _edit_response(record, *, errors=None, status=200):
    return render_template(
        "qualification_record_form.html", record=record, employee=record.user if record else None,
        employees=_employees() if record is None else [], form=request.form if request.method == "POST" else _record_form(record),
        errors=errors or [], **_context(),
    ), status


def _save_record(record=None):
    _check_csrf()
    action = request.form.get("action", "")
    if action not in {"save_draft", "process"}:
        return _edit_response(record, errors=[_("Choose whether to save a draft or mark the qualification as processed.")], status=400)
    values, errors = parse_metadata(request.form, require_complete=action == "process" or bool(record and record.status == "processed"))
    owner_id = record.user_id if record else _positive_id(request.form.get("user_id"))
    if record and "user_id" in request.form and _positive_id(request.form.get("user_id")) != owner_id:
        errors.append(_("The employee linked to an existing qualification cannot be changed."))
    owner = db.session.get(User, owner_id) if owner_id else None
    if owner is None:
        errors.append(_("Choose a valid employee."))
    revision = _positive_id(request.form.get("revision")) if record else None
    if record and revision is None:
        errors.append(_("Reload the qualification editor before saving."))
    if errors:
        return _edit_response(record, errors=errors, status=400)
    # Serialise highest-qualification changes for the same employee. The revision
    # predicate independently prevents a stale browser overwriting newer edits.
    db.session.execute(select(User.id).where(User.id == owner_id).with_for_update()).scalar_one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    target_status = "processed" if action == "process" or bool(record and record.status == "processed") else "uploaded"
    values.update(status=target_status, updated_at=now)
    if target_status == "processed":
        values.update(processed_at=now, processed_by_id=current_user.id)
    if record:
        result = db.session.execute(update(QualificationRecord).where(
            QualificationRecord.id == record.id, QualificationRecord.revision == revision,
        ).values(**values, revision=revision + 1).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            db.session.rollback()
            return _edit_response(record, errors=[_("This qualification changed in another window. Reload it before saving again.")], status=409)
        saved_id = record.id
    else:
        new_record = QualificationRecord(user_id=owner_id, **values)
        db.session.add(new_record)
        db.session.flush()
        saved_id = new_record.id
    if values["highest"] and target_status == "processed":
        db.session.execute(update(QualificationRecord).where(
            QualificationRecord.user_id == owner_id, QualificationRecord.id != saved_id,
            QualificationRecord.highest.is_(True),
        ).values(highest=False, revision=QualificationRecord.revision + 1, updated_at=now).execution_options(synchronize_session=False))
    db.session.commit()
    flash(_("Qualification details saved."), "success")
    return redirect(url_for("qualifications.manage", status=target_status, user_id=owner_id))


@qualifications.route("/qualifications/manage/new", methods=["GET", "POST"])
@manager_required
def new():
    return _save_record() if request.method == "POST" else _edit_response(None)


@qualifications.route("/qualifications/manage/<int:record_id>/edit", methods=["GET", "POST"])
@manager_required
def edit(record_id):
    record = db.session.get(QualificationRecord, record_id)
    if record is None:
        abort(404)
    return _save_record(record) if request.method == "POST" else _edit_response(record)


@qualifications.route("/qualifications/documents/<int:document_id>")
@login_required
def document(document_id):
    evidence = db.session.get(QualificationDocument, document_id)
    if evidence is None:
        abort(404)
    own_access = evidence.record.user_id == current_user.id and can_access_page("add_qualification")
    manager_access = _is_manager() and can_access_page("qualifications.manage")
    if not (own_access or manager_access):
        abort(403)
    # Authorisation precedes fetching the deferred binary column.
    evidence = db.session.get(QualificationDocument, document_id, options=[undefer(QualificationDocument.data)], populate_existing=True)
    response = send_file(BytesIO(evidence.data), mimetype=evidence.mime_type, as_attachment=True,
                         download_name=evidence.filename, conditional=False, etag=False, max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = "sandbox"
    return response
