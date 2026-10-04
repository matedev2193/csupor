"""Calendar, database and freshness boundary around the pure scheduling engine."""

import calendar
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta

from flask_babel import force_locale, gettext as _

from . import db
from .i18n import enum_label
from .models import Contract, ContractType, LeaveRequest, LeaveRequestCategory, LeaveRequestStatus, TeacherClassification, WorkingDayOverride
from .working_calendar import HungaryCalendar
from .worktime_models import WorkAssignment, WorkGroup, WorkGroupMerge, WorkSchedule, WorkTimeEntry


# Increment when allocation rules change, so older registers require regeneration.
SCHEDULING_RULE_VERSION = 3


class WorktimeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def month_bounds(year, month):
    try:
        year, month = int(year), int(month)
        if not (1970 <= year <= 2100 and 1 <= month <= 12):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise WorktimeError(_("Choose a valid year and month.")) from None
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def iter_days(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def intersects(start, end, other_start, other_end):
    return start <= (other_end or date.max) and other_start <= (end or date.max)


def active_on(record, day):
    return record.start_date <= day and (record.end_date is None or day <= record.end_date)


def user_display_name(user):
    name = (user.profile.full_name or "").strip() if user.profile else ""
    return name or user.username


def time_string(minutes):
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


def _iso(value):
    return value.isoformat() if value else None


def calendar_days(start, end):
    overrides = {row.day: row for row in WorkingDayOverride.query.filter(WorkingDayOverride.day.between(start, end)).all()}
    calendar_ = HungaryCalendar()
    return [
        {"date": day.isoformat(), "is_working_day": overrides[day].is_working_day if day in overrides else calendar_.is_working_day(day)}
        for day in iter_days(start, end)
    ]


def _contract_dict(contract, assignments=()):
    roles = {
        ContractType.teacher: "teacher", ContractType.teaching_assistant: "teaching_assistant",
        ContractType.nursery_assistant: "nursery_assistant", ContractType.secretary: "secretary",
        ContractType.employee_under_the_labour_code: "employee_under_the_labour_code",
    }
    return {
        "contract_id": contract.id, "user_id": contract.user_id, "site_id": contract.place_of_work_id,
        "name": user_display_name(contract.user), "role": roles[contract.contract_type],
        "trainee": contract.teacher_classification == TeacherClassification.trainee,
        "weekly_hours": contract.working_hours_per_week,
        "start_date": _iso(contract.start_date), "end_date": _iso(contract.end_date),
        "classification_start_date": _iso(contract.classification_start_date), "job_title": contract.job_title,
        "assignments": [{"id": row.id, "group_id": row.group_id, "start_date": _iso(row.start_date), "end_date": _iso(row.end_date), "shift_phase": None if row.flexible_shift else row.shift_phase} for row in assignments if row.contract_id == contract.id],
    }


def build_payload(place_id, year, month):
    """Use complete Monday–Sunday weeks and every overlapping worker contract.

    Hash only source records, never stored schedules: regenerating another month
    cannot invalidate an otherwise identical register.
    """
    start, end = month_bounds(year, month)
    first = start - timedelta(days=start.weekday())
    last = end + timedelta(days=6 - end.weekday())
    site_contracts = Contract.query.filter(
        Contract.place_of_work_id == place_id, Contract.start_date <= last,
        db.or_(Contract.end_date.is_(None), Contract.end_date >= first),
    ).order_by(Contract.id).all()
    user_ids = {row.user_id for row in site_contracts}
    contracts = Contract.query.filter(
        Contract.user_id.in_(user_ids), Contract.start_date <= last,
        db.or_(Contract.end_date.is_(None), Contract.end_date >= first),
    ).order_by(Contract.id).all() if user_ids else []
    groups = WorkGroup.query.filter(
        WorkGroup.place_of_work_id == place_id, WorkGroup.start_date <= last,
        db.or_(WorkGroup.end_date.is_(None), WorkGroup.end_date >= first),
    ).order_by(WorkGroup.id).all()
    group_ids = {group.id for group in groups}
    assignments = WorkAssignment.query.filter(
        WorkAssignment.contract_id.in_([row.id for row in site_contracts]), WorkAssignment.start_date <= last,
        db.or_(WorkAssignment.end_date.is_(None), WorkAssignment.end_date >= first),
    ).order_by(WorkAssignment.id).all() if site_contracts else []
    leaves = LeaveRequest.query.filter(
        LeaveRequest.user_id.in_(user_ids),
        LeaveRequest.status.in_([LeaveRequestStatus.approved, LeaveRequestStatus.pending_cancellation]),
        LeaveRequest.start_date <= last,
        db.or_(LeaveRequest.end_date >= first, db.and_(LeaveRequest.end_date.is_(None), LeaveRequest.start_date >= first)),
    ).order_by(LeaveRequest.id).all() if user_ids else []
    merges = WorkGroupMerge.query.filter(
        WorkGroupMerge.source_group_id.in_(group_ids), WorkGroupMerge.day.between(first, last),
    ).order_by(WorkGroupMerge.day, WorkGroupMerge.id).all() if group_ids else []
    day_statuses = calendar_days(first, last)
    payload = {
        "site_id": place_id, "year": start.year, "month": start.month,
        "days": [day["date"] for day in day_statuses if day["is_working_day"]],
        "workers": [_contract_dict(row, assignments) for row in site_contracts],
        "external_contracts": [_contract_dict(row) for row in contracts if row.place_of_work_id != place_id],
        "groups": [{"id": row.id, "site_id": row.place_of_work_id, "name": row.name, "start_date": _iso(row.start_date), "end_date": _iso(row.end_date)} for row in groups],
        "absences": [{"id": row.id, "user_id": row.user_id, "contract_id": row.contract_id, "start_date": _iso(row.start_date), "end_date": _iso(row.end_date or row.start_date), "category": row.category.value, "label": row.category.value, "status": row.status.value} for row in leaves],
        "merges": [{"id": row.id, "day": _iso(row.day), "source_group_id": row.source_group_id, "target_group_id": row.target_group_id, "note": row.note} for row in merges],
    }
    # Bump when configured scheduling semantics change. Include off-days too,
    # but not history: a new adjacent register must not stale this month.
    hash_inputs = {**payload, "rule_version": SCHEDULING_RULE_VERSION, "calendar": day_statuses}
    fingerprint = hashlib.sha256(json.dumps(hash_inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    history = WorkTimeEntry.query.join(WorkSchedule).join(Contract, WorkTimeEntry.contract_id == Contract.id).filter(
        WorkSchedule.place_of_work_id == place_id,
        WorkTimeEntry.day < start,
        db.or_(db.and_(Contract.contract_type == ContractType.teacher, WorkTimeEntry.start_minute == 420),
               db.and_(Contract.contract_type == ContractType.nursery_assistant, WorkTimeEntry.start_minute == 360)),
        WorkTimeEntry.work_minutes > 0,
    ).order_by(WorkTimeEntry.day, WorkTimeEntry.contract_id).all()
    payload["history"] = [{
        "day": _iso(row.day), "contract_id": row.contract_id, "user_id": row.user_id,
        "site_id": place_id, "start_minute": row.start_minute,
        "shift": "early_teacher" if row.start_minute == 420 else "early_nursery",
    } for row in history]

    return payload, fingerprint


def settings_revision(place_id, *, lock=False):
    def records(query):
        # Mutations first lock the workplace. Current locking reads also bypass
        # an older MySQL repeatable-read snapshot; populate_existing refreshes
        # any group loaded before that lock was acquired.
        if lock:
            query = query.populate_existing().with_for_update()
        return query.all()

    groups = records(WorkGroup.query.filter_by(place_of_work_id=place_id).order_by(WorkGroup.id))
    contracts = records(Contract.query.filter_by(place_of_work_id=place_id).order_by(Contract.id))
    assignments = records(WorkAssignment.query.filter(WorkAssignment.group_id.in_([row.id for row in groups])).order_by(WorkAssignment.id)) if groups else []
    values = {
        "groups": [{"id": row.id, "name": row.name, "start": _iso(row.start_date), "end": _iso(row.end_date)} for row in groups],
        "contracts": [_contract_dict(row) for row in contracts],
        "assignments": [{"id": row.id, "group": row.group_id, "contract": row.contract_id, "start": _iso(row.start_date), "end": _iso(row.end_date), "phase": row.shift_phase, "flexible": row.flexible_shift} for row in assignments],
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def schedule_is_stale(schedule):
    return schedule.source_hash != build_payload(schedule.place_of_work_id, schedule.year, schedule.month)[1]


def entry_dict(entry):
    return {name: getattr(entry, name) for name in (
        "contract_id", "user_id", "group_id", "day", "start_minute", "end_minute", "break_start",
        "break_minutes", "work_minutes", "teaching_minutes", "shift", "note", "note_parts", "is_manual",
    )} | {"day": entry.day.isoformat()}


def current_absences(payload, user_id, day):
    day = _iso(day) if isinstance(day, date) else day
    return [item for item in payload["absences"] if item["user_id"] == user_id and item["start_date"] <= day <= item["end_date"]]


def _translated_part(part):
    message = part["message"]
    return _(message, **part.get("params", {}))


def display_rows(schedule, user_id, payload=None, *, stale=False):
    """A changed approved absence must never still look like time worked."""
    if schedule is None:
        return []
    rows = []
    for entry in sorted(schedule.entries, key=lambda row: (row.day, row.user_id, row.contract_id)):
        if entry.user_id != user_id:
            continue
        absence = current_absences(payload, user_id, entry.day) if payload is not None else []
        overridden = stale and bool(absence)
        parts = getattr(entry, "note_parts", None) or []
        # Absence categories have their own translated field. The engine's
        # English fallback must not become a second, mixed-language label in
        # notes; merge and substitution annotations remain separate.
        absence_messages = {category.value for category in LeaveRequestCategory} | {"Absence"}
        parts = [part for part in parts if not all(item.strip() in absence_messages for item in part["message"].split(";"))]
        system_note = " ".join(_translated_part(part) for part in parts)
        manual_note = entry.note if entry.is_manual or entry.shift != "absence" else ""
        note = " ".join(part for part in (system_note, manual_note) if part)
        absence_label = "; ".join(dict.fromkeys(str(enum_label(item["category"])) for item in absence)) if absence else (_("Absence") if entry.shift == "absence" else "")
        rows.append({
            "id": entry.id, "date": entry.day, "user_id": entry.user_id,
            "user_name": user_display_name(entry.user), "contract_id": entry.contract_id,
            "job_title": entry.contract.job_title, "workplace": entry.contract.place_of_work.address,
            "group_name": entry.group.name if entry.group else "",
            "start": None if overridden else time_string(entry.start_minute),
            "end": None if overridden else time_string(entry.end_minute),
            "break_start": None if overridden else time_string(entry.break_start),
            "break_minutes": 0 if overridden else entry.break_minutes,
            "work_minutes": 0 if overridden else entry.work_minutes,
            "worked_minutes": 0 if overridden else entry.work_minutes,
            "teaching_minutes": 0 if overridden else entry.teaching_minutes,
            "shift": "absence" if overridden else entry.shift,
            "note": _("Approved absence; regenerate the register.") if overridden else note,
            "absence_label": absence_label,
            "is_manual": entry.is_manual, "manual_note": manual_note,
        })
    return rows


def weekly_totals(rows, year, month):
    first, last = month_bounds(year, month)
    totals = defaultdict(lambda: {"minutes": 0, "teaching_minutes": 0})
    for row in rows:
        monday = row["date"] - timedelta(days=row["date"].weekday())
        totals[monday]["minutes"] += row["work_minutes"]
        totals[monday]["teaching_minutes"] += row["teaching_minutes"]
    return [{"week_start": monday, "week_end": monday + timedelta(days=6), "partial": monday < first or monday + timedelta(days=6) > last, **total} for monday, total in sorted(totals.items())]


def export_register(user, year, month):
    # The printable employer register consistently uses Hungarian labels even
    # when an employee navigates the website in English.
    with force_locale("hu"):
        return _export_register(user, year, month)


def _export_register(user, year, month):
    start, end = month_bounds(year, month)
    contracts = Contract.query.filter(
        Contract.user_id == user.id, Contract.start_date <= end,
        db.or_(Contract.end_date.is_(None), Contract.end_date >= start),
    ).all()
    if not contracts:
        raise WorktimeError(_("This employee has no contract in the selected month."), 404)
    rows, schedules = [], []
    for place_id in sorted({contract.place_of_work_id for contract in contracts}):
        schedule = WorkSchedule.query.filter_by(place_of_work_id=place_id, year=start.year, month=start.month).first()
        if not schedule:
            raise WorktimeError(_("Generate the register for every workplace before exporting this employee's month."), 409)
        payload, fingerprint = build_payload(place_id, year, month)
        if schedule.source_hash != fingerprint:
            raise WorktimeError(_("The register is out of date. Regenerate it before exporting."), 409)
        rows.extend(display_rows(schedule, user.id, payload))
        schedules.append(schedule)
    return {
        "employee_name": user_display_name(user), "year": start.year, "month": start.month,
        "job_title": ", ".join(dict.fromkeys(contract.job_title for contract in contracts)),
        "is_teacher": any(contract.contract_type == ContractType.teacher for contract in contracts),
        "status": "confirmed" if all(schedule.status == "confirmed" for schedule in schedules) else "draft",
        "rows": sorted(rows, key=lambda row: (row["date"], row["contract_id"])),
    }
