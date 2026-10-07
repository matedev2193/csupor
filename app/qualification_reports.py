"""Read-only qualification reporting by completion date and nursery school year.

Historical records which only contain a calendar year cannot reliably be
assigned to a September–August school year. They stay visible, but are never
silently assigned an invented completion date or month.
"""

from collections import defaultdict
from datetime import date
import unicodedata

from flask import Blueprint, abort, render_template, request
from flask_babel import format_date, gettext as _, pgettext
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload, load_only

from .models import User, UserProfile
from .qualification_models import QualificationRecord
from .qualification_taxonomy import (
    KINDS, COMPLETION_STATES, STUDY_CATEGORIES, AWARD_CATEGORIES,
    TRAINING_TOPICS, ORGANISER_TYPES, FUNDING_TYPES, ATTENDANCE_MODES,
)
from .page_access import can_access_endpoint, can_access_page
from .people import local_today


qualification_reports = Blueprint("qualification_reports", __name__)


def _text(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def _normalise(value):
    return _text(value).casefold()


def school_year_start(completed):
    """Return the start year; a missing full date always stays unassigned."""
    if completed is None:
        return None
    return completed.year if completed.month >= 9 else completed.year - 1


def _year_label(year):
    return f"{year}/{year + 1}"


def _employee(user):
    return {
        "id": user.id,
        "name": _text(user.profile.full_name if user.profile else "") or user.username,
        "username": user.username,
    }


def _users():
    # Do not join contracts: several contracts must never multiply completions.
    # Only names and account identifiers are needed for this read-only directory.
    users = User.query.options(
        load_only(User.id, User.username),
        joinedload(User.profile).load_only(UserProfile.full_name),
    ).all()
    return sorted((_employee(user) for user in users), key=lambda row: (_normalise(row["name"]), row["id"]))


def _record(record, employee, kind=None):
    kind = kind or record.kind
    year = school_year_start(record.date_obtained)
    kind_label = str(KINDS.get(kind, _("Not yet classified")))
    type_name = _text(record.level_or_type) if kind == "qualification" else kind_label
    return {
        "id": record.id, "kind": kind, "kind_label": kind_label,
        "status": record.status,
        "status_label": _("Processed") if record.status == "processed" else pgettext("qualification_status", "Uploaded"),
        "completion_state": record.completion_state,
        "completion_label": str(COMPLETION_STATES.get(record.completion_state, _("Not specified"))),
        "user_id": employee["id"], "employee_name": employee["name"], "username": employee["username"],
        "qualification_name": _text(record.qualification_name),
        "type_name": type_name or _("Not specified"),
        "type_key": "qualification:" + _normalise(record.level_or_type) if kind == "qualification" else (kind or "unclassified"),
        "institution_name": _text(record.institution_name),
        "date_obtained": record.date_obtained, "year_obtained": record.year_obtained,
        "degree_number": record.degree_number, "highest": bool(record.highest),
        "school_year": _year_label(year) if year is not None else None,
        "study_start_date": record.study_start_date, "study_end_date": record.study_end_date,
        "study_categories": record.study_categories or [], "award_categories": record.award_categories or [],
        "training_topic": record.training_topic, "organiser_type": record.organiser_type,
        "funding_type": record.funding_type, "attendance_mode": record.attendance_mode,
        "duration_hours": record.duration_hours, "credits": record.credits,
        "digital_pedagogy": bool(record.digital_pedagogy),
    }


def _records(users, user_id=None):
    by_id = {user["id"]: user for user in users}
    query = QualificationRecord.query
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    records = [_record(record, by_id[record.user_id]) for record in query.all() if record.user_id in by_id]
    return sorted(records, key=lambda row: (
        -(row["date_obtained"].toordinal() if row["date_obtained"] else 0),
        _normalise(row["employee_name"]), _normalise(row["qualification_name"]), row["kind"] or "", row["id"],
    ))


def _counts(records):
    return {
        "employees": len({row["user_id"] for row in records}), "total": len(records),
        "qualifications": sum(row["kind"] == "qualification" for row in records),
        "exams": sum(row["kind"] == "exam" for row in records),
        "teacher_training": sum(row["kind"] == "teacher_training" for row in records),
        "other_courses": sum(row["kind"] == "other_course" for row in records),
    }


def _classification_counts(records, field, options, *, multiple=False):
    rows = []
    for value, label in options.items():
        matching = [row for row in records if value in row[field]] if multiple else [row for row in records if row[field] == value]
        rows.append({"value": value, "label": str(label), **_counts(matching)})
    unclassified = [row for row in records if not row[field]]
    if unclassified:
        rows.append({"value": "unknown", "label": _("Not specified"), **_counts(unclassified)})
    return rows


def _participates(row, year):
    """Use recorded study intervals only; only ongoing studies have an open end."""
    start = row["study_start_date"]
    end = row["study_end_date"]
    if start is None or (end is None and row["completion_state"] != "in_progress"):
        return False
    if end is not None and end < start:
        return False
    if year == "all":
        return True
    if year == "unknown":
        return False
    period_start, period_end = date(int(year), 9, 1), date(int(year) + 1, 8, 31)
    return start <= period_end and (end is None or end >= period_start)


def _ksh_report(processed, completed, year):
    participants = [row for row in processed if _participates(row, year)]
    studying = [row for row in participants if row["study_categories"]]
    training = [row for row in participants if row["kind"] == "teacher_training"]
    by_duration = []
    for hours in (30, 60, 90, 120):
        by_duration.append({"label": _("%(hours)s hours", hours=hours),
                            **_counts([row for row in training if row["duration_hours"] == hours])})
    by_duration.append({"label": _("Other duration"), **_counts([
        row for row in training if row["duration_hours"] is not None and row["duration_hours"] not in (30, 60, 90, 120)
    ])})
    by_duration.append({"label": _("Not specified"), **_counts([row for row in training if row["duration_hours"] is None])})
    return {
        "participation_totals": _counts(participants), "study_totals": _counts(studying),
        "teacher_training_totals": _counts(training),
        "other_course_totals": _counts([row for row in participants if row["kind"] == "other_course"]),
        "by_study": _classification_counts(studying, "study_categories", STUDY_CATEGORIES, multiple=True),
        "by_award": _classification_counts(completed, "award_categories", AWARD_CATEGORIES, multiple=True),
        "by_study_funding": _classification_counts(studying, "funding_type", FUNDING_TYPES),
        "by_training_topic": _classification_counts(training, "training_topic", TRAINING_TOPICS),
        "by_organiser": _classification_counts(training, "organiser_type", ORGANISER_TYPES),
        "by_training_funding": _classification_counts(training, "funding_type", FUNDING_TYPES),
        "by_attendance": _classification_counts(training, "attendance_mode", ATTENDANCE_MODES),
        "by_duration": by_duration,
        "by_digital_pedagogy": [
            {"label": _("Digital pedagogy"), **_counts([row for row in training if row["digital_pedagogy"]])},
            {"label": _("Other teacher training"), **_counts([row for row in training if not row["digital_pedagogy"]])},
        ],
        "unassigned_participation_count": sum(
            (row["study_categories"] or row["kind"] in {"teacher_training", "other_course"})
            and not _participates(row, "all") for row in processed
        ),
    }


def _display(values):
    """A deterministic human-readable spelling for a normalised group."""
    return min(values, key=lambda value: (_normalise(value), value))


def _type_options(records):
    types = defaultdict(list)
    for row in records:
        types[row["type_key"]].append(row["type_name"])
    options = [{"value": key, "label": _display(labels)} for key, labels in types.items()]
    return sorted(options, key=lambda row: (_normalise(row["label"]), row["value"]))


def _parse_filters(args, users, type_options, today):
    for key in ("year", "user_id", "kind", "type", "q"):
        if len(args.getlist(key)) > 1:
            abort(400)
    year = args.get("year", str(school_year_start(today)))
    if year not in {"all", "unknown"}:
        if not (len(year) == 4 and year.isascii() and year.isdigit() and 1899 <= int(year) <= 9998):
            abort(400)
    user_id = args.get("user_id", "")
    if user_id and not (
        user_id.isascii() and user_id.isdigit() and len(user_id) <= 10
        and int(user_id) in {user["id"] for user in users}
    ):
        abort(400)
    kind = args.get("kind", "all")
    if kind not in {"all", *KINDS}:
        abort(400)
    selected_type = args.get("type", "")
    if selected_type.startswith("qualification:"):
        selected_type = "qualification:" + _normalise(selected_type[len("qualification:"):])
    if selected_type and selected_type not in {row["value"] for row in type_options}:
        abort(400)
    query = args.get("q", "")
    if len(query) > 200 or "\x00" in query:
        abort(400)
    return {"year": year, "user_id": str(int(user_id)) if user_id else "", "kind": kind, "type": selected_type, "q": _text(query)}


def build_report(records, filters, today):
    """Build counts from unique records, counting people distinctly per group."""
    matching = []
    query = _normalise(filters["q"])
    for row in records:
        if filters["user_id"] and row["user_id"] != int(filters["user_id"]):
            continue
        if filters["kind"] != "all" and row["kind"] != filters["kind"]:
            continue
        if filters["type"] and row["type_key"] != filters["type"]:
            continue
        searchable = " ".join(str(row[key] or "") for key in (
            "qualification_name", "institution_name", "employee_name", "username", "type_name", "degree_number",
        ))
        if query and query not in _normalise(searchable):
            continue
        matching.append(row)

    year = filters["year"]
    processed = [row for row in matching if row["status"] == "processed"]
    completed = [row for row in processed if row["completion_state"] == "completed"]
    selected = [row for row in completed if (
        year == "all"
        or (year == "unknown" and row["date_obtained"] is None)
        or (year not in {"all", "unknown"} and school_year_start(row["date_obtained"]) == int(year))
    )]
    years, types, names, months = (defaultdict(list) for _ in range(4))
    for row in selected:
        years[school_year_start(row["date_obtained"])].append(row)
        types[row["type_key"]].append(row)
        names[(row["type_key"], _normalise(row["qualification_name"]))].append(row)
        if row["date_obtained"] is not None:
            months[(row["date_obtained"].year, row["date_obtained"].month)].append(row)

    by_year = [{"value": str(key) if key is not None else "unknown", "label": _year_label(key) if key is not None else _("Exact date not recorded"), **_counts(years[key])}
               for key in sorted(years, key=lambda value: (value is None, -(value or 0)))]
    by_type = [{"label": _display([row["type_name"] for row in rows]), "value": key, **_counts(rows)} for key, rows in types.items()]
    by_name = [{"label": f"{_display([row['qualification_name'] for row in rows])} ({_display([row['type_name'] for row in rows])})",
                **_counts(rows)} for rows in names.values()]
    by_type.sort(key=lambda row: (_normalise(row["label"]), row["value"]))
    by_name.sort(key=lambda row: _normalise(row["label"]))

    if year not in {"all", "unknown"}:
        # Keep September–August order and display quiet months as zero too.
        month_keys = [(int(year) + (month // 12), (month % 12) + 1) for month in range(8, 20)]
    else:
        month_keys = sorted(months)
    by_month = [{"label": format_date(date(y, m, 1), "LLLL yyyy"), "value": f"{y:04d}-{m:02d}", **_counts(months[(y, m)])} for y, m in month_keys]
    available_years = {school_year_start(row["date_obtained"]) for row in records if row["date_obtained"] is not None}
    for row in records:
        start, end = row["study_start_date"], row["study_end_date"]
        if start is not None:
            last = end or (today if row["completion_state"] == "in_progress" else start)
            available_years.update(range(school_year_start(start), school_year_start(last) + 1))
    available_years.add(school_year_start(today))
    if year not in {"all", "unknown"}:
        available_years.add(int(year))
    school_years = [{"value": "all", "label": _("All school years")}, {"value": "unknown", "label": _("Exact date not recorded")}]
    school_years.extend({"value": str(value), "label": _year_label(value)} for value in sorted(available_years, reverse=True))
    return {
        "filters": filters, "school_years": school_years, "totals": _counts(selected),
        "records": selected, "by_year": by_year, "by_type": by_type, "by_name": by_name, "by_month": by_month,
        "unknown_count": sum(row["date_obtained"] is None for row in completed),
        "ksh": _ksh_report(processed, selected, year),
    }


def _require_access():
    if not can_access_page("qualification_reports.index"):
        abort(403)


@qualification_reports.get("/qualification-reports")
@login_required
def index():
    _require_access()
    users = _users()
    records = _records(users)
    types = _type_options(records)
    today = local_today()
    filters = _parse_filters(request.args, users, types, today)
    return render_template("qualification_reports.html", user_options=users, type_options=types, kinds=KINDS, **build_report(records, filters, today))


@qualification_reports.get("/qualification-reports/employees")
@login_required
def employees():
    _require_access()
    if len(request.args.getlist("q")) > 1:
        abort(400)
    query = request.args.get("q", "")
    if len(query) > 200 or "\x00" in query:
        abort(400)
    query = _text(query)
    users = _users()
    records = defaultdict(list)
    for row in _records(users):
        records[row["user_id"]].append(row)
    result = []
    for user in users:
        if query and _normalise(query) not in _normalise(user["name"] + " " + user["username"]):
            continue
        counts = _counts(records[user["id"]])
        result.append({**user, "qualification_count": counts["qualifications"], "exam_count": counts["exams"],
                       "training_count": counts["teacher_training"] + counts["other_courses"],
                       "uploaded_count": sum(row["status"] == "uploaded" for row in records[user["id"]]),
                       "unknown_count": sum(row["status"] == "processed" and row["completion_state"] == "completed"
                                            and row["date_obtained"] is None for row in records[user["id"]])})
    return render_template("qualification_report_employees.html", users=result, q=query)


@qualification_reports.get("/qualification-reports/employees/<int:user_id>")
@login_required
def employee(user_id):
    _require_access()
    user = User.query.options(load_only(User.id, User.username), joinedload(User.profile).load_only(UserProfile.full_name)).get_or_404(user_id)
    person = _employee(user)
    records = _records([person], user_id=user_id)
    return render_template(
        "qualification_report_employee.html", employee=person, records=records, totals=_counts(records),
        can_manage=current_user.privilege.value in {"hr", "ceo"} and can_access_endpoint("qualifications.edit"),
        unknown_count=sum(row["status"] == "processed" and row["completion_state"] == "completed"
                          and row["date_obtained"] is None for row in records),
    )
