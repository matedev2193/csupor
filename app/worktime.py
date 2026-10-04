"""Authorised planning, human verification and monthly employee exports."""

from datetime import date, datetime, timedelta, timezone
from functools import wraps
from hmac import compare_digest
from secrets import token_hex, token_urlsafe

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import default_exceptions

from . import db
from .models import Contract, PlaceOfWork, User, UserPrivilege
from .people import local_today
from .worktime_models import WorkAssignment, WorkGroup, WorkGroupMerge, WorkSchedule, WorkTimeEntry
from .worktime_service import (
    WorktimeError, active_on, build_payload, current_absences, display_rows, entry_dict,
    export_register, intersects, month_bounds, settings_revision, user_display_name, weekly_totals,
)


worktime = Blueprint("worktime", __name__)


def can_manage():
    return current_user.privilege in {UserPrivilege.hr, UserPrivilege.ceo}


def manager_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not can_manage():
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def _csrf_token():
    if "worktime_csrf_token" not in session:
        session["worktime_csrf_token"] = token_urlsafe(32)
    return session["worktime_csrf_token"]


def _check_csrf():
    supplied, expected = request.form.get("csrf_token", ""), session.get("worktime_csrf_token", "")
    if not supplied or not expected or not compare_digest(supplied, expected):
        raise WorktimeError(_("Your session expired. Reload the page and try again."), 400)


def _period(values):
    today = local_today()
    if request.method == "POST" and ("year" not in values or "month" not in values):
        raise WorktimeError(_("Choose a valid year and month."))
    start, _end = month_bounds(values.get("year", today.year), values.get("month", today.month))
    return start.year, start.month


def _integer(value, label, *, optional=False):
    if optional and value in (None, ""):
        return None
    try:
        integer = int(value)
        if str(integer) != str(value) or integer < 1:
            raise ValueError
        return integer
    except (ValueError, TypeError, OverflowError):
        raise WorktimeError(_("Choose a valid %(field)s.", field=label)) from None


def _parse_date(value, *, optional=False):
    if not value and optional:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or not (1970 <= parsed.year <= 2100):
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise WorktimeError(_("Enter a valid date.")) from None


def _wants_json():
    return request.accept_mimetypes.best == "application/json"


def _response_error(error):
    db.session.rollback()
    if _wants_json():
        return jsonify(error=str(error)), error.status
    # Ordinary form submissions retain their contextual page and visible error;
    # JSON callers receive the original validation/conflict status above.
    flash(str(error), "error")
    if request.method == "POST":
        values = request.form
        target = "worktime.groups" if request.endpoint == "worktime.groups" else "worktime.index"
        return redirect(url_for(target, year=values.get("year"), month=values.get("month"), place_id=values.get("place_id"), user_id=values.get("user_id")))
    return default_exceptions[error.status](description=str(error)).get_response()


@worktime.errorhandler(WorktimeError)
def handle_error(error):
    return _response_error(error)


@worktime.after_request
def private_response(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _success(message, *, target="worktime.index", **values):
    if _wants_json():
        return jsonify(message=message, **values)
    flash(message, "success")
    values.pop("revision", None)
    return redirect(url_for(target, **values))


def _place(place_id, *, lock=False):
    identifier = _integer(place_id, _("workplace"))
    statement = select(PlaceOfWork).where(PlaceOfWork.id == identifier)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    place = db.session.execute(statement).scalar_one_or_none()
    if not place:
        abort(404)
    return place


def _schedule(place_id, year, month, *, lock=False):
    statement = select(WorkSchedule).where(WorkSchedule.place_of_work_id == place_id, WorkSchedule.year == year, WorkSchedule.month == month)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return db.session.execute(statement).scalar_one_or_none()


def _check_revision(schedule):
    submitted = request.form.get("revision")
    expected = schedule.revision if schedule else ""
    if submitted is None or not compare_digest(submitted, expected):
        raise WorktimeError(_("The register changed in another window. Reload the page and try again."), 409)


def _check_fresh(schedule, fingerprint):
    if schedule.source_hash != fingerprint:
        raise WorktimeError(_("The register is out of date. Regenerate it before making changes."), 409)


def _claim_revision(schedule):
    """Conditional update also protects SQLite where FOR UPDATE is ignored."""
    revision = token_hex(16)
    changed = db.session.execute(update(WorkSchedule).where(WorkSchedule.id == schedule.id, WorkSchedule.revision == schedule.revision).values(revision=revision).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise WorktimeError(_("The register changed in another window. Reload the page and try again."), 409)
    schedule.revision = revision
    return revision


def _issue_display(issues, *, own_contract_ids=None):
    result = []
    for issue in issues:
        if own_contract_ids is not None and issue.get("contract_id") not in own_contract_ids:
            continue
        row = dict(issue)
        if row.get("contract_id"):
            contract = db.session.get(Contract, row["contract_id"])
            row["user_name"] = user_display_name(contract.user) if contract else ""
        if row.get("group_id"):
            group = db.session.get(WorkGroup, row["group_id"])
            row["group_name"] = group.name if group else ""
        row["message"] = _(row.get("message", "Scheduling conflict"), **row.get("params", {}))
        row.setdefault("severity", "error")
        row.setdefault("date", row.get("day"))
        result.append(row)
    return result


def _hard_issues(issues):
    return any(issue.get("severity", "error") in {"error", "hard"} for issue in issues)


def _validation_days(payload, year, month):
    prefix = f"{year:04d}-{month:02d}-"
    return [day for day in payload["days"] if day.startswith(prefix)]


def _mutation_context():
    _check_csrf()
    year, month = _period(request.form)
    place = _place(request.form.get("place_id"), lock=True)
    schedule = _schedule(place.id, year, month, lock=True)
    _check_revision(schedule)
    return place, year, month, schedule


@worktime.get("/worktime")
@login_required
def index():
    year, month = _period(request.args)
    start, end = month_bounds(year, month)
    contracts = Contract.query.filter(Contract.start_date <= end, db.or_(Contract.end_date.is_(None), Contract.end_date >= start)).order_by(Contract.id).all()
    if can_manage():
        places = PlaceOfWork.query.order_by(PlaceOfWork.id).all()
    else:
        contracts = [row for row in contracts if row.user_id == current_user.id]
        places = sorted({row.place_of_work for row in contracts}, key=lambda row: row.id)
    selected_place_id = _integer(request.args.get("place_id"), _("workplace"), optional=True)
    if selected_place_id is not None and selected_place_id not in {row.id for row in places}:
        abort(403 if not can_manage() else 404)
    selected_place = next((row for row in places if row.id == selected_place_id), None) or (places[0] if places else None)
    selected_place_id = selected_place.id if selected_place else None
    site_contracts = [row for row in contracts if row.place_of_work_id == selected_place_id]
    users = sorted({row.user for row in site_contracts}, key=lambda user: (user_display_name(user).casefold(), user.id))
    selected_user_id = _integer(request.args.get("user_id"), _("employee"), optional=True)
    if not can_manage() and selected_user_id not in {None, current_user.id}:
        abort(403)
    selected_user = next((user for user in users if user.id == selected_user_id), None)
    if selected_user is None:
        selected_user = next((user for user in users if user.id == current_user.id), None) or (users[0] if users else (current_user if not can_manage() else None))
    selected_user_id = selected_user.id if selected_user else None
    schedule = _schedule(selected_place_id, year, month) if selected_place else None
    payload, fingerprint = build_payload(selected_place_id, year, month) if selected_place else (None, None)
    stale = schedule is not None and schedule.source_hash != fingerprint
    rows = display_rows(schedule, selected_user_id, payload, stale=stale) if selected_user else []
    groups = WorkGroup.query.filter_by(place_of_work_id=selected_place_id).order_by(WorkGroup.name).all() if selected_place else []
    merges = WorkGroupMerge.query.filter(WorkGroupMerge.day.between(start, end), WorkGroupMerge.source_group_id.in_([group.id for group in groups])).order_by(WorkGroupMerge.day).all() if groups else []
    context = dict(
        selected_year=year, selected_month=month, places=places, selected_place=selected_place,
        selected_place_id=selected_place_id or "", users=users, selected_user=selected_user,
        selected_user_id=selected_user_id or "", can_manage_worktime=can_manage(),
        csrf_token=_csrf_token(), schedule=schedule, is_stale=stale,
        issues=_issue_display(schedule.issues, own_contract_ids=None if can_manage() else {row.id for row in site_contracts}) if schedule else [], rows=rows, groups=groups, merges=merges,
        weekly_totals=weekly_totals(rows, year, month), monthly_total_minutes=sum(row["work_minutes"] for row in rows),
        monthly_teaching_minutes=sum(row["teaching_minutes"] for row in rows), user_display_name=user_display_name,
    )
    return render_template("worktime.html", **context)


@worktime.post("/worktime/generate")
@manager_required
def generate():
    from .worktime_engine import build_schedule, validate_schedule
    place, year, month, schedule = _mutation_context()
    if schedule and any(row.is_manual for row in schedule.entries) and request.form.get("discard_manual") != "1":
        raise WorktimeError(_("Confirm that manual changes may be replaced before regenerating."), 409)
    payload, fingerprint = build_payload(place.id, year, month)
    result = build_schedule(payload)
    first, last = month_bounds(year, month)
    entries = [row for row in result["entries"] if first <= date.fromisoformat(row["day"]) <= last]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if schedule is None:
        schedule = WorkSchedule(place_of_work_id=place.id, year=year, month=month, revision=token_hex(16), source_hash=fingerprint, issues=[], status="draft", generated_at=now)
        db.session.add(schedule)
    else:
        _claim_revision(schedule)
        schedule.entries.clear()
    schedule.source_hash = fingerprint
    visible_issues = [issue for issue in result["issues"] if not issue.get("day") or first.isoformat() <= issue["day"] <= last.isoformat()]
    validation = validate_schedule(payload, entries, validation_days=_validation_days(payload, year, month))
    schedule.issues = validation + [issue for issue in visible_issues if issue not in validation]
    schedule.status = "draft"
    schedule.generated_at = now
    schedule.generated_by_id = current_user.id
    schedule.confirmed_at = schedule.confirmed_by_id = None
    try:
        # Delete old rows before inserting replacements under the unique key.
        db.session.flush()
        for row in entries:
            fields = {name: row.get(name) for name in ("contract_id", "user_id", "group_id", "start_minute", "end_minute", "break_start", "break_minutes", "work_minutes", "teaching_minutes", "shift", "note")}
            fields["note_parts"] = row.get("note_parts") or []
            fields["note"] = "" if fields["note_parts"] else (fields["note"] or "")
            schedule.entries.append(WorkTimeEntry(day=date.fromisoformat(row["day"]), is_manual=False, **fields))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise WorktimeError(_("The register changed in another window. Reload the page and try again."), 409) from None
    return _success(_("The draft register has been generated. Review the warnings before verifying it."), place_id=place.id, year=year, month=month, revision=schedule.revision, user_id=request.form.get("user_id"))


@worktime.post("/worktime/confirm")
@manager_required
def confirm():
    from .worktime_engine import validate_schedule
    place, year, month, schedule = _mutation_context()
    if schedule is None:
        abort(404)
    if not schedule.entries:
        raise WorktimeError(_("Generate employee entries before verifying the register."), 409)
    payload, fingerprint = build_payload(place.id, year, month)
    _check_fresh(schedule, fingerprint)
    issues = validate_schedule(payload, [entry_dict(row) for row in schedule.entries], validation_days=_validation_days(payload, year, month))
    if _hard_issues(issues):
        raise WorktimeError(_("Resolve every scheduling conflict before verifying the register."), 409)
    if request.form.get("acknowledge") != "1":
        raise WorktimeError(_("Confirm that you have checked the entries against actual attendance."))
    _claim_revision(schedule)
    schedule.issues = issues
    schedule.status = "confirmed"
    schedule.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    schedule.confirmed_by_id = current_user.id
    db.session.commit()
    return _success(_("The register has been verified."), place_id=place.id, year=year, month=month, revision=schedule.revision, user_id=request.form.get("user_id"))


def _minutes(value, *, optional=False):
    if optional and not value:
        return None
    try:
        if len(value) != 5 or value[2] != ":":
            raise ValueError
        hour, minute = map(int, value.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
        return hour * 60 + minute
    except (ValueError, TypeError, AttributeError):
        raise WorktimeError(_("Enter a valid time in HH:MM format.")) from None


@worktime.post("/worktime/entries/<int:entry_id>")
@manager_required
def edit_entry(entry_id):
    from .worktime_engine import validate_schedule
    place, year, month, schedule = _mutation_context()
    entry = db.session.get(WorkTimeEntry, entry_id)
    if schedule is None or entry is None or entry.schedule_id != schedule.id:
        abort(404)
    payload, fingerprint = build_payload(place.id, year, month)
    _check_fresh(schedule, fingerprint)
    start = _minutes(request.form.get("start"), optional=True)
    end = _minutes(request.form.get("end"), optional=True)
    break_start = _minutes(request.form.get("break_start"), optional=True)
    note = request.form.get("note", "").strip()
    if len(note) > 2000:
        raise WorktimeError(_("Keep the note within 2,000 characters."))
    if (start is None) != (end is None):
        raise WorktimeError(_("Enter both the start and end of the working day."))
    if start is None:
        work_minutes = break_minutes = teaching_minutes = 0
        break_start = None
    else:
        duration = end - start
        # Exactly six hours has no break. A longer shift includes 20 unpaid
        # minutes, and cannot contain more than eight hours of actual work.
        break_minutes = 20 if duration > 360 else 0
        work_minutes = duration - break_minutes
        if not (0 < work_minutes <= 480) or (work_minutes <= 360 and break_minutes):
            raise WorktimeError(_("A working day may contain at most eight working hours and a 20-minute break."))
        if break_minutes and (break_start is None or not start <= break_start <= end - 20):
            raise WorktimeError(_("Place the 20-minute break inside the working day."))
        if not break_minutes:
            break_start = None
        if current_absences(payload, entry.user_id, entry.day):
            raise WorktimeError(_("An absent employee must have zero working time."))
        try:
            teaching_minutes = int(request.form.get("teaching_minutes", "0"))
            if not 0 <= teaching_minutes <= work_minutes:
                raise ValueError
        except (ValueError, TypeError):
            raise WorktimeError(_("Teaching minutes must be between zero and the working minutes.")) from None
    _claim_revision(schedule)
    entry.start_minute, entry.end_minute = start, end
    entry.break_start, entry.break_minutes = break_start, break_minutes
    entry.work_minutes, entry.teaching_minutes = work_minutes, teaching_minutes
    entry.note, entry.is_manual = note, True
    entry.shift = "absence" if current_absences(payload, entry.user_id, entry.day) else ("manual" if start is not None else "off")
    db.session.flush()
    schedule.issues = validate_schedule(payload, [entry_dict(row) for row in schedule.entries], validation_days=_validation_days(payload, year, month))
    schedule.status = "draft"
    schedule.confirmed_at = schedule.confirmed_by_id = None
    db.session.commit()
    return _success(_("The entry has been saved. Review the updated scheduling warnings."), place_id=place.id, year=year, month=month, revision=schedule.revision, user_id=entry.user_id)


@worktime.route("/worktime/groups", methods=["GET", "POST"])
@manager_required
def groups():
    values = request.form if request.method == "POST" else request.args
    year, month = _period(values)
    places = PlaceOfWork.query.order_by(PlaceOfWork.id).all()
    selected_id = _integer(values.get("place_id"), _("workplace"), optional=True)
    selected_place = _place(values.get("place_id"), lock=True) if request.method == "POST" else (_place(selected_id) if selected_id else (places[0] if places else None))
    if request.method == "POST":
        _check_csrf()
        if selected_place is None:
            raise WorktimeError(_("Create a workplace before adding groups."))
        submitted_revision = request.form.get("settings_revision", "")
        if not compare_digest(submitted_revision, settings_revision(selected_place.id)):
            raise WorktimeError(_("Group settings changed in another window. Reload the page and try again."), 409)
        action = request.form.get("action")
        if action == "group":
            _save_group(selected_place)
        elif action == "assignment":
            _save_assignment(selected_place)
        elif action == "delete_assignment":
            assignment = db.session.get(WorkAssignment, _integer(request.form.get("assignment_id"), _("assignment")))
            if assignment is None or assignment.group.place_of_work_id != selected_place.id:
                abort(404)
            db.session.delete(assignment)
        else:
            raise WorktimeError(_("Choose a valid action."))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise WorktimeError(_("This group name already exists at the workplace."), 409) from None
        return _success(_("Group settings saved. Regenerate affected registers."), target="worktime.groups", place_id=selected_place.id, year=year, month=month)
    site_id = selected_place.id if selected_place else None
    site_groups = WorkGroup.query.filter_by(place_of_work_id=site_id).order_by(WorkGroup.name).all() if site_id else []
    assignments = WorkAssignment.query.filter(WorkAssignment.group_id.in_([group.id for group in site_groups])).order_by(WorkAssignment.start_date, WorkAssignment.id).all() if site_groups else []
    contracts = Contract.query.filter_by(place_of_work_id=site_id).order_by(Contract.start_date.desc(), Contract.id).all() if site_id else []
    monday = date(year, month, 1)
    monday -= timedelta(days=monday.weekday())
    phase_weeks = {phase: monday + timedelta(days=7 * (((monday - date(1970, 1, 5)).days // 7 + phase) % 2)) for phase in (0, 1)}
    return render_template("worktime_groups.html", settings_revision=settings_revision(site_id) if site_id else "", shift_phase_weeks=phase_weeks, places=places, selected_place=selected_place, selected_place_id=site_id or "", groups=site_groups, assignments=assignments, contracts=contracts, csrf_token=_csrf_token(), selected_year=year, selected_month=month, can_manage_worktime=True, user_display_name=user_display_name)


def _date_range():
    start = _parse_date(request.form.get("start_date"))
    end = _parse_date(request.form.get("end_date"), optional=True)
    if end is not None and end < start:
        raise WorktimeError(_("The end date cannot be before the start date."))
    return start, end


def _save_group(place):
    group_id = _integer(request.form.get("group_id"), _("group"), optional=True)
    group = db.session.get(WorkGroup, group_id) if group_id else None
    if group_id and (group is None or group.place_of_work_id != place.id):
        abort(404)
    name = request.form.get("name", "").strip()
    if not name or len(name) > 120:
        raise WorktimeError(_("Enter a group name of at most 120 characters."))
    start, end = _date_range()
    if group:
        for assignment in WorkAssignment.query.filter_by(group_id=group.id).all():
            if assignment.start_date < start or (end is not None and (assignment.end_date is None or assignment.end_date > end)):
                raise WorktimeError(_("The group dates must contain all existing assignments."))
        for merge in WorkGroupMerge.query.filter(db.or_(WorkGroupMerge.source_group_id == group.id, WorkGroupMerge.target_group_id == group.id)).all():
            if merge.day < start or (end is not None and merge.day > end):
                raise WorktimeError(_("The group dates must contain all existing group merges."))
    else:
        group = WorkGroup(place_of_work_id=place.id)
        db.session.add(group)
    group.name, group.start_date, group.end_date = name, start, end


def _save_assignment(place):
    contract = db.session.get(Contract, _integer(request.form.get("contract_id"), _("contract")))
    group = db.session.get(WorkGroup, _integer(request.form.get("group_id"), _("group")))
    if contract is None or group is None or contract.place_of_work_id != place.id or group.place_of_work_id != place.id:
        raise WorktimeError(_("Choose a contract and group at the same workplace."))
    start, end = _date_range()
    for owner in (contract, group):
        if start < owner.start_date or (owner.end_date is not None and (end is None or end > owner.end_date)):
            raise WorktimeError(_("The assignment must stay inside the contract and group dates."))
    phase = request.form.get("shift_phase")
    if phase not in {"0", "1"}:
        raise WorktimeError(_("Choose the weekly shift pattern."))
    assignment_id = _integer(request.form.get("assignment_id"), _("assignment"), optional=True)
    assignment = db.session.get(WorkAssignment, assignment_id) if assignment_id else None
    if assignment_id and (assignment is None or assignment.group.place_of_work_id != place.id):
        abort(404)
    others = WorkAssignment.query.filter_by(contract_id=contract.id).all()
    if any(row.id != assignment_id and intersects(start, end, row.start_date, row.end_date) for row in others):
        raise WorktimeError(_("This contract already has a group assignment during these dates."))
    if assignment is None:
        assignment = WorkAssignment()
        db.session.add(assignment)
    assignment.contract_id, assignment.group_id = contract.id, group.id
    assignment.start_date, assignment.end_date, assignment.shift_phase = start, end, int(phase)


@worktime.post("/worktime/merges")
@manager_required
def merge():
    place, year, month, schedule = _mutation_context()
    if request.form.get("action", "create") not in {"create", "delete"}:
        raise WorktimeError(_("Choose a valid action."))
    if request.form.get("action", "create") == "delete":
        merge_row = db.session.get(WorkGroupMerge, _integer(request.form.get("merge_id"), _("group merge")))
        if merge_row is None or merge_row.source_group.place_of_work_id != place.id or (merge_row.day.year, merge_row.day.month) != (year, month):
            abort(404)
        db.session.delete(merge_row)
    else:
        day = _parse_date(request.form.get("day"))
        if (day.year, day.month) != (year, month):
            raise WorktimeError(_("Choose a merge date inside the selected month."))
        source = db.session.get(WorkGroup, _integer(request.form.get("source_group_id"), _("group")))
        target = db.session.get(WorkGroup, _integer(request.form.get("target_group_id"), _("group")))
        if source is None or target is None or source.id == target.id or source.place_of_work_id != place.id or target.place_of_work_id != place.id or not active_on(source, day) or not active_on(target, day):
            raise WorktimeError(_("Choose two different active groups at the same workplace."))
        existing = WorkGroupMerge.query.filter_by(day=day).all()
        if any(row.source_group_id == target.id or row.target_group_id == source.id for row in existing):
            raise WorktimeError(_("A group merge cannot form a chain or a cycle."))
        merge_row = next((row for row in existing if row.source_group_id == source.id), None)
        if merge_row is None:
            merge_row = WorkGroupMerge(day=day, source_group_id=source.id, created_by_id=current_user.id)
            db.session.add(merge_row)
        note = request.form.get("note", "").strip()
        if len(note) > 255:
            raise WorktimeError(_("Keep the merge note within 255 characters."))
        merge_row.target_group_id, merge_row.note = target.id, note
    if schedule:
        _claim_revision(schedule)
        schedule.status = "draft"
        schedule.confirmed_at = schedule.confirmed_by_id = None
    db.session.commit()
    return _success(_("Group merge saved. Regenerate the monthly register."), place_id=place.id, year=year, month=month, revision=schedule.revision if schedule else "", user_id=request.form.get("user_id"))


@worktime.get("/worktime/export/<int:user_id>")
@login_required
def export(user_id):
    from io import BytesIO
    from .worktime_export import export_worktime_csv, export_worktime_pdf
    if not can_manage() and current_user.id != user_id:
        abort(403)
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    year, month = _period(request.args)
    format_ = request.args.get("format", "pdf")
    if format_ not in {"pdf", "csv"}:
        raise WorktimeError(_("Choose PDF or CSV for the export."))
    register = export_register(user, year, month)
    data = export_worktime_csv(register) if format_ == "csv" else export_worktime_pdf(register)
    return send_file(BytesIO(data), mimetype="text/csv; charset=utf-8" if format_ == "csv" else "application/pdf", as_attachment=True, download_name=f"worktime-{user.id}-{year:04d}-{month:02d}.{format_}")
