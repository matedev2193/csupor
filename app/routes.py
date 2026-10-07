from datetime import date, datetime, timedelta, timezone
from functools import wraps
from secrets import compare_digest, token_urlsafe
from urllib.parse import urlsplit

from flask import abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_babel import _
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import generate_password_hash

from . import SUPPORTED_LOCALES, db, get_locale

from .working_calendar import HungaryCalendar
from .i18n import enum_label
from .people import birthday_context, local_today
from .page_access import can_access_endpoint, can_access_page, invalidate_access_cache, landing_url
from .approval_display import approval_description
from .leave_basis import leave_basis, age_supplement_days, eligible_children
from .leave_approval import (
    POLICY_DESCRIPTIONS,
    POLICY_LABELS,
    approvals_satisfy_policy,
    current_leave_approval_policy,
    get_leave_approval_settings,
)
from .notification_events import (
    actionable_recipient_ids, record_leave_change, record_reviewer_changes,
    snapshot_leave_request, snapshot_pending_leave_tasks,
)

from .models import (
    Contract,
    ContractLeaveLimit,
    ContractType,
    Dependent,
    DependentType,
    Gender,
    Leadership,
    LeadershipPosition,
    LegalEntity,
    LeaveApprovalPolicy,
    LeaveRequest,
    LeaveYear,
    LeaveRequestCategory,
    LeaveRequestStatus,
    LeaveType,
    PlaceOfWork,
    MaritalStatus,
    TeacherClassification,
    User,
    UserPrivilege,
    UserProfile,
    WorkingDayOverride,
    parse_iso_date,
)


MANAGEABLE_PRIVILEGES = (UserPrivilege.employee, UserPrivilege.hr, UserPrivilege.ceo, UserPrivilege.developer)

PROFILE_COMPLETION_FIELDS = (
    "full_name",
    "name_at_birth",
    "date_of_birth",
    "place_of_birth",
    "gender",
    "mothers_maiden_name",
    "citizenships",
    "social_security_number",
    "tax_number",
    "education_number",
    "teacher_id_card_number",
    "permanent_residence",
    "phone_number",
    "bank_account_number",
    "marital_status",
)


TEACHERS_STATUS_ACT_CONTRACT_TYPES = {
    ContractType.teacher,
    ContractType.teaching_assistant,
    ContractType.nursery_assistant,
    ContractType.secretary,
}

CALENDAR_YEAR_BOUNDED_LEAVE_TYPES = [
    LeaveType.basic_leave,
    LeaveType.supplementary_leave_based_on_age,
    LeaveType.supplementary_leave_for_children,
    LeaveType.supplementary_leave_for_children_with_disability,
    LeaveType.supplementary_leave_for_young_employees,
    LeaveType.supplementary_leave_for_reduced_working_capacity,
    LeaveType.sick_leave,
    LeaveType.leave_carried_over_from_previous_year,
]

RANGE_BASED_LEAVE_TYPES = [
    LeaveType.maternity_leave,
    LeaveType.paternity_leave,
    LeaveType.parental_leave,
    LeaveType.childcare_fee,
    LeaveType.childcare_allowance,
    LeaveType.supplementary_leave_for_birth_of_grandchild,
    LeaveType.supplementary_leave_for_first_marriage,
    LeaveType.exemption_from_obligation_to_work,
]

LEAVE_TYPES_WITHOUT_LIMIT = ["unpaid leave", "sickness benefit", "childcare sickness benefit"]


def _profile_completion_percentage(profile: UserProfile | None) -> int:
    if profile is None:
        return 0

    completed = sum(1 for field in PROFILE_COMPLETION_FIELDS if _profile_value_present(getattr(profile, field)))
    return round((completed / len(PROFILE_COMPLETION_FIELDS)) * 100)


def _profile_value_present(value) -> bool:
    return bool(value.strip()) if isinstance(value, str) else value is not None


def _profile_completion_state(profile: UserProfile | None) -> str:
    if profile is None:
        return "empty"
    if all(_profile_value_present(getattr(profile, field)) for field in PROFILE_COMPLETION_FIELDS):
        return "complete"
    if any(_profile_value_present(getattr(profile, field)) for field in (*PROFILE_COMPLETION_FIELDS, "temporary_address", "disability")):
        return "partial"
    return "empty"


def _profile_status_label(profile: UserProfile | None) -> str:
    completion_percentage = _profile_completion_percentage(profile)
    if completion_percentage == 100:
        return _("Complete")
    return _("%(percent)s%% complete", percent=completion_percentage)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_digit_field(label: str, value: str | None, length: int) -> str | None:
    normalized = _normalize_optional_text(value)
    if not normalized:
        return None
    if not normalized.isdigit() or len(normalized) != length:
        return _("%(label)s must be exactly %(length)s digits.", label=label, length=length)
    return None


def _normalize_legal_entity_tax_number(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if not normalized:
        return None
    return normalized.replace("-", "")


def _format_legal_entity_tax_number(value: str | None) -> str:
    normalized = _normalize_legal_entity_tax_number(value)
    if not normalized or len(normalized) != 11 or not normalized.isdigit():
        return value or ""
    return f"{normalized[:8]}-{normalized[8]}-{normalized[9:]}"


def _save_profile_from_form(profile: UserProfile) -> list[str]:
    social_security_number = _normalize_optional_text(request.form.get("social_security_number"))
    tax_number = _normalize_optional_text(request.form.get("tax_number"))
    education_number = _normalize_optional_text(request.form.get("education_number"))

    teacher_id_card_number = _normalize_optional_text(request.form.get("teacher_id_card_number"))
    errors = [
        _validate_digit_field(_("Social security number"), social_security_number, 9),
        _validate_digit_field(_("Personal tax identification number"), tax_number, 10),
        _validate_digit_field(_("Education number"), education_number, 11),
    ]
    if teacher_id_card_number and len(teacher_id_card_number) > 64:
        errors.append(_("Teacher ID card number must be at most %(length)s characters.", length=64))
    errors = [err for err in errors if err]
    if errors:
        return errors

    profile.full_name = _normalize_optional_text(request.form.get("full_name"))
    profile.name_at_birth = _normalize_optional_text(request.form.get("name_at_birth"))
    profile.date_of_birth = parse_iso_date(request.form.get("date_of_birth"))
    profile.place_of_birth = _normalize_optional_text(request.form.get("place_of_birth"))
    gender_value = _normalize_optional_text(request.form.get("gender"))
    profile.gender = Gender(gender_value) if gender_value else None
    profile.mothers_maiden_name = _normalize_optional_text(request.form.get("mothers_maiden_name"))
    profile.citizenships = _normalize_optional_text(request.form.get("citizenships"))
    profile.social_security_number = social_security_number
    profile.tax_number = tax_number
    profile.education_number = education_number
    profile.teacher_id_card_number = teacher_id_card_number
    profile.permanent_residence = _normalize_optional_text(request.form.get("permanent_residence"))
    profile.temporary_address = _normalize_optional_text(request.form.get("temporary_address"))
    profile.phone_number = _normalize_optional_text(request.form.get("phone_number"))
    profile.bank_account_number = _normalize_optional_text(request.form.get("bank_account_number"))
    marital_status_value = _normalize_optional_text(request.form.get("marital_status"))
    profile.marital_status = MaritalStatus(marital_status_value) if marital_status_value else None
    profile.disability = _normalize_optional_text(request.form.get("disability"))
    return []


def _render_profile_editor(
    profile: UserProfile,
    target_user: User,
    *,
    manager_mode: bool = False,
    email_confirmation_required: bool = False,
    email_confirmation_error: str | None = None,
):
    if not manager_mode:
        session.setdefault("profile_email_csrf_token", token_urlsafe(32))
    return render_template(
        "profile.html",
        profile=profile,
        genders=Gender,
        marital_statuses=MaritalStatus,
        manager_mode=manager_mode,
        target_user=target_user,
        profile_email_csrf_token=session.get("profile_email_csrf_token", "") if not manager_mode else "",
        email_confirmation_required=email_confirmation_required,
        email_confirmation_error=email_confirmation_error,
    )




def _validate_om_id(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if not normalized or not normalized.isdigit() or len(normalized) != 6:
        return _("OM id must be exactly 6 digits.")
    return None


def _contract_place_label(place: PlaceOfWork) -> str:
    return f"{place.legal_entity.name} - {place.address}"


def _save_contract_from_form(contract: Contract) -> list[str]:
    # Validate before assigning: queries below must not autoflush invalid edits.
    errors = []
    values = {}
    contract_type_raw = _normalize_optional_text(request.form.get("contract_type"))
    if not contract_type_raw:
        errors.append(_("Contract type is required."))
    else:
        try:
            values["contract_type"] = ContractType(contract_type_raw)
        except ValueError:
            errors.append(_("Invalid contract type selected."))

    classification = _normalize_optional_text(request.form.get("teacher_classification"))
    try:
        values["teacher_classification"] = TeacherClassification(classification) if classification else None
    except ValueError:
        errors.append(_("Invalid teacher classification selected."))

    for field in ("start_date", "end_date", "certificate_of_good_conduct_date", "classification_start_date"):
        try:
            values[field] = parse_iso_date(request.form.get(field))
        except ValueError:
            errors.append(_("Enter valid dates in the contract form."))
            values[field] = None
    if values["start_date"] is None:
        errors.append(_("Start date is required."))
    if values["end_date"] and values["start_date"] and values["end_date"] < values["start_date"]:
        errors.append(_("End date cannot be earlier than the start date."))

    values["job_title"] = _normalize_optional_text(request.form.get("job_title"))
    if not values["job_title"]:
        errors.append(_("Job title is required."))
    elif len(values["job_title"]) > 120:
        errors.append(_("Job title must be at most 120 characters."))
    values["certificate_of_good_conduct_number"] = _normalize_optional_text(request.form.get("certificate_of_good_conduct_number"))
    if values["certificate_of_good_conduct_number"] and len(values["certificate_of_good_conduct_number"]) > 64:
        errors.append(_("Certificate of good conduct number must be at most 64 characters."))
    values["working_hours_per_week"] = request.form.get("working_hours_per_week", type=int)
    if values["working_hours_per_week"] is None:
        errors.append(_("Working hours per week is required."))
    elif values["working_hours_per_week"] < 1:
        errors.append(_("Working hours per week must be greater than 0."))

    values["legal_entity_id"] = request.form.get("legal_entity_id", type=int)
    values["place_of_work_id"] = request.form.get("place_of_work_id", type=int)
    if not values["legal_entity_id"]:
        errors.append(_("Employer is required."))
    elif db.session.get(LegalEntity, values["legal_entity_id"]) is None:
        errors.append(_("Selected employer does not exist."))
    if not values["place_of_work_id"]:
        errors.append(_("Place of work is required."))
    else:
        place = db.session.get(PlaceOfWork, values["place_of_work_id"])
        if place is None:
            errors.append(_("Selected place of work does not exist."))
        elif place.legal_entity_id != values["legal_entity_id"]:
            errors.append(_("Selected place of work does not belong to the selected employer."))
    if not errors:
        for field, value in values.items():
            setattr(contract, field, value)
    return errors


def _save_leadership_from_form(leadership: Leadership) -> list[str]:
    legal_entity_id = request.form.get("legal_entity_id", type=int)
    contract_id = request.form.get("contract_id", type=int)
    position_value = request.form.get("position")

    leadership.legal_entity_id = legal_entity_id
    leadership.contract_id = contract_id
    leadership.position = LeadershipPosition(position_value) if position_value in {item.value for item in LeadershipPosition} else None
    leadership.start_date = parse_iso_date(request.form.get("start_date"))
    leadership.end_date = parse_iso_date(request.form.get("end_date"))

    errors = []
    if not legal_entity_id:
        errors.append(_("Legal entity is required."))
    if not contract_id:
        errors.append(_("Contract is required."))
    if leadership.position is None:
        errors.append(_("Leadership position is required."))
    if leadership.start_date is None:
        errors.append(_("Start date is required."))
    if leadership.end_date and leadership.start_date and leadership.end_date < leadership.start_date:
        errors.append(_("End date cannot be earlier than the start date."))

    if contract_id:
        contract = db.session.get(Contract, contract_id)
        if contract is None:
            errors.append(_("Selected contract does not exist."))
        elif legal_entity_id and contract.legal_entity_id != legal_entity_id:
            errors.append(_("Selected contract does not belong to the selected legal entity."))

    return errors


def _contract_display_name(contract: Contract) -> str:
    display_name = contract.user.profile.full_name if contract.user.profile and contract.user.profile.full_name else contract.user.username
    return f"{display_name} · {enum_label(contract.contract_type)} · {contract.start_date} → {contract.end_date or _('Ongoing')}"


def _is_contract_active_in_year(contract: Contract, calendar_year: int) -> bool:
    first_day = date(calendar_year, 1, 1)
    last_day = date(calendar_year, 12, 31)
    return contract.start_date <= last_day and (contract.end_date is None or contract.end_date >= first_day)


def _leave_type_available_for_contract(
    leave_type: LeaveType,
    contract_type: ContractType,
    user: User | None = None,
) -> bool:
    if leave_type == LeaveType.maternity_leave:
        return bool(user and user.profile and user.profile.gender == Gender.female)
    if leave_type == LeaveType.paternity_leave:
        return bool(user and user.profile and user.profile.gender == Gender.male)
    if leave_type == LeaveType.supplementary_leave_based_on_age:
        return contract_type == ContractType.employee_under_the_labour_code
    if leave_type in {
        LeaveType.supplementary_leave_for_birth_of_grandchild,
        LeaveType.supplementary_leave_for_first_marriage,
    }:
        return contract_type in TEACHERS_STATUS_ACT_CONTRACT_TYPES
    return True


def _is_contract_active_on(contract: Contract, day: date) -> bool:
    return contract.start_date <= day and (contract.end_date is None or contract.end_date >= day)


def _active_contracts_for_user(user: User, day: date | None = None) -> list[Contract]:
    active_day = day or date.today()
    return sorted(
        [contract for contract in user.contracts if _is_contract_active_on(contract, active_day)],
        key=lambda contract: (contract.start_date, contract.id),
        reverse=True,
    )


def _current_active_contract(user: User) -> Contract | None:
    active_contracts = _active_contracts_for_user(user)
    return active_contracts[0] if active_contracts else None


def _has_child_dependents(user: User) -> bool:
    return any(dependent.dependent_type == DependentType.child for dependent in user.dependents)


def _has_positive_leave_limit(contract: Contract, leave_types: set[LeaveType]) -> bool:
    return any(limit.leave_type in leave_types and limit.limit_days > 0 for limit in contract.leave_limits)


PAID_LEAVE_LIMIT_TYPES = {
    LeaveType.basic_leave,
    LeaveType.supplementary_leave_based_on_age,
    LeaveType.supplementary_leave_for_children,
    LeaveType.supplementary_leave_for_children_with_disability,
    LeaveType.supplementary_leave_for_young_employees,
    LeaveType.supplementary_leave_for_reduced_working_capacity,
    LeaveType.leave_carried_over_from_previous_year,
    LeaveType.parental_leave,
    LeaveType.paternity_leave,
    LeaveType.supplementary_leave_for_birth_of_grandchild,
    LeaveType.supplementary_leave_for_first_marriage,
}


def _iter_dates(start_date: date, end_date: date):
    current_day = start_date
    while current_day <= end_date:
        yield current_day
        current_day += timedelta(days=1)


def _working_day_overrides(start_date: date, end_date: date) -> dict[date, WorkingDayOverride]:
    return {
        override.day: override
        for override in WorkingDayOverride.query.filter(
            WorkingDayOverride.day >= start_date,
            WorkingDayOverride.day <= end_date,
        ).all()
    }


def _is_working_day(day: date, overrides_by_day: dict[date, WorkingDayOverride] | None = None) -> bool:
    return bool(_working_day_status(day, overrides_by_day)["is_working_day"])


def _calendar_day_count(start_date: date, end_date: date) -> int:
    return (end_date - start_date).days + 1


def _working_day_count(start_date: date, end_date: date) -> int:
    overrides_by_day = _working_day_overrides(start_date, end_date)
    return sum(1 for day in _iter_dates(start_date, end_date) if _is_working_day(day, overrides_by_day))


def _leave_request_year_bounds(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


def _leave_limit_days_for_year(contract: Contract, leave_types: set[LeaveType], year: int) -> int:
    year_start, year_end = _leave_request_year_bounds(year)
    total = 0
    for limit in contract.leave_limits:
        if limit.leave_type not in leave_types or limit.limit_days <= 0:
            continue
        if limit.period_start is not None or limit.period_end is not None:
            period_start = limit.period_start or year_start
            period_end = limit.period_end or year_end
            if period_start > year_end or period_end < year_start:
                continue
        elif limit.calendar_year != year:
            continue
        total += limit.limit_days
    return total


def _leave_request_used_days_in_year(leave_request: LeaveRequest, year: int) -> int:
    year_start, year_end = _leave_request_year_bounds(year)
    request_start = max(leave_request.start_date, year_start)
    request_end = min(_leave_request_end_date(leave_request), year_end)
    if request_start > request_end:
        return 0
    if leave_request.category in {
        LeaveRequestCategory.paid_leave,
        LeaveRequestCategory.exemption_from_obligation_to_work,
        LeaveRequestCategory.unpaid_leave,
    }:
        return _working_day_count(request_start, request_end)
    if leave_request.category == LeaveRequestCategory.health_leave:
        sick_available = _leave_limit_days_for_year(leave_request.contract, {LeaveType.sick_leave}, year)
        earlier_sick_used = _health_leave_sick_days_used_before_request(leave_request, year)
        working_days = _working_day_count(request_start, request_end)
        sick_days = min(max(sick_available - earlier_sick_used, 0), working_days)
        return sick_days + max(_calendar_day_count(request_start, request_end) - sick_days, 0)
    if leave_request.category in {
        LeaveRequestCategory.childcare_sickness_benefit,
        LeaveRequestCategory.childbirth_leave,
    }:
        return _calendar_day_count(request_start, request_end)
    return 0


def _health_leave_sick_days_used_before_request(leave_request: LeaveRequest, year: int) -> int:
    year_start, year_end = _leave_request_year_bounds(year)
    query = LeaveRequest.query.filter(
        LeaveRequest.user_id == leave_request.user_id,
        LeaveRequest.contract_id == leave_request.contract_id,
        LeaveRequest.category == LeaveRequestCategory.health_leave,
        LeaveRequest.status.in_(BLOCKING_LEAVE_REQUEST_STATUSES),
        LeaveRequest.start_date <= year_end,
        db.or_(LeaveRequest.end_date.is_(None), LeaveRequest.end_date >= year_start),
    )
    if leave_request.id is not None:
        query = query.filter(LeaveRequest.id != leave_request.id)
    earlier_requests = query.order_by(LeaveRequest.start_date.asc(), LeaveRequest.id.asc()).all()
    used = 0
    for earlier_request in earlier_requests:
        if (earlier_request.start_date, earlier_request.id or 0) >= (leave_request.start_date, leave_request.id or 0):
            continue
        request_start = max(earlier_request.start_date, year_start)
        request_end = min(_leave_request_end_date(earlier_request), year_end)
        if request_start <= request_end:
            used += _working_day_count(request_start, request_end)
    return min(used, _leave_limit_days_for_year(leave_request.contract, {LeaveType.sick_leave}, year))


def _paid_leave_available_days(contract: Contract, year: int) -> int:
    return _leave_limit_days_for_year(contract, PAID_LEAVE_LIMIT_TYPES, year)


def _paid_leave_used_days(contract: Contract, year: int) -> int:
    year_start, year_end = _leave_request_year_bounds(year)
    requests = LeaveRequest.query.filter(
        LeaveRequest.contract_id == contract.id,
        LeaveRequest.category == LeaveRequestCategory.paid_leave,
        LeaveRequest.status.in_(BLOCKING_LEAVE_REQUEST_STATUSES),
        LeaveRequest.start_date <= year_end,
        db.or_(LeaveRequest.end_date.is_(None), LeaveRequest.end_date >= year_start),
    ).all()
    return sum(_leave_request_used_days_in_year(leave_request, year) for leave_request in requests)


def _paid_leave_remaining_days(contract: Contract, year: int) -> int:
    return sum(_paid_leave_remaining_by_limit(contract, year).values())


def _is_leave_year_open(year: int) -> bool:
    leave_year = db.session.get(LeaveYear, year)
    return bool(leave_year and leave_year.is_open)


def _paid_leave_limits_by_expiry(contract: Contract, year: int | None = None) -> list[ContractLeaveLimit]:
    limits = []
    for limit in contract.leave_limits:
        if limit.leave_type not in PAID_LEAVE_LIMIT_TYPES or limit.limit_days <= 0:
            continue
        period_start = limit.period_start or date(limit.calendar_year, 1, 1)
        period_end = limit.period_end or date(limit.calendar_year, 12, 31)
        if year is not None:
            year_start, year_end = _leave_request_year_bounds(year)
            if period_start > year_end or period_end < year_start:
                continue
        limits.append(limit)
    return sorted(
        limits,
        key=lambda limit: (
            limit.period_end or date(limit.calendar_year, 12, 31),
            limit.period_start or date(limit.calendar_year, 1, 1),
            limit.id or 0,
        ),
    )


def _consume_paid_leave_days(
    capacity_by_limit: dict[int, int],
    limits: list[ContractLeaveLimit],
    start_date: date,
    end_date: date,
) -> bool:
    overrides_by_day = _working_day_overrides(start_date, end_date)
    has_capacity = True
    for day in _iter_dates(start_date, end_date):
        if not _is_working_day(day, overrides_by_day):
            continue
        matching_limit = next(
            (
                limit
                for limit in limits
                if capacity_by_limit.get(limit.id, 0) > 0
                and (limit.period_start or date(limit.calendar_year, 1, 1)) <= day
                and (limit.period_end or date(limit.calendar_year, 12, 31)) >= day
            ),
            None,
        )
        if matching_limit is None:
            # Keep accounting for later covered days even if historical requests
            # no longer fit a subsequently edited limit.
            has_capacity = False
            continue
        capacity_by_limit[matching_limit.id] -= 1
    return has_capacity


def _paid_leave_remaining_capacity(
    contract: Contract,
    excluding_request_id: int | None = None,
) -> dict[int, int]:
    # A custom validity period may span several years. Build one shared capacity
    # map and charge all existing requests before selecting a calendar year.
    limits = _paid_leave_limits_by_expiry(contract)
    capacity_by_limit = {limit.id: limit.limit_days for limit in limits if limit.id is not None}
    if not limits:
        return capacity_by_limit
    period_start = min(limit.period_start or date(limit.calendar_year, 1, 1) for limit in limits)
    period_end = max(limit.period_end or date(limit.calendar_year, 12, 31) for limit in limits)
    query = LeaveRequest.query.filter(
        LeaveRequest.contract_id == contract.id,
        LeaveRequest.category == LeaveRequestCategory.paid_leave,
        LeaveRequest.status.in_(BLOCKING_LEAVE_REQUEST_STATUSES),
        LeaveRequest.start_date <= period_end,
        db.or_(LeaveRequest.end_date.is_(None), LeaveRequest.end_date >= period_start),
    )
    if excluding_request_id is not None:
        query = query.filter(LeaveRequest.id != excluding_request_id)
    requests = query.order_by(LeaveRequest.start_date.asc(), LeaveRequest.id.asc()).all()
    for leave_request in requests:
        request_start = max(leave_request.start_date, period_start)
        request_end = min(_leave_request_end_date(leave_request), period_end)
        _consume_paid_leave_days(capacity_by_limit, limits, request_start, request_end)
    return {limit_id: max(remaining, 0) for limit_id, remaining in capacity_by_limit.items()}


def _paid_leave_remaining_by_limit(
    contract: Contract,
    year: int,
    excluding_request_id: int | None = None,
) -> dict[int, int]:
    capacity_by_limit = _paid_leave_remaining_capacity(contract, excluding_request_id)
    return {
        limit.id: capacity_by_limit.get(limit.id, 0)
        for limit in _paid_leave_limits_by_expiry(contract, year)
        if limit.id is not None
    }


def _paid_leave_request_capacity_error(contract: Contract, start_date: date, end_date: date) -> str | None:
    capacity_by_limit = _paid_leave_remaining_capacity(contract)
    for year in range(start_date.year, end_date.year + 1):
        year_start, year_end = _leave_request_year_bounds(year)
        request_start = max(start_date, year_start)
        request_end = min(end_date, year_end)
        limits = _paid_leave_limits_by_expiry(contract, year)
        requested_days = _working_day_count(request_start, request_end)
        remaining_days = sum(capacity_by_limit.get(limit.id, 0) for limit in limits)
        if requested_days > remaining_days:
            return _(
                "Paid leave requested for %(year)s needs %(requested)s days, but only %(remaining)s days remain for that year.",
                year=year, requested=requested_days, remaining=remaining_days,
            )
        if not _consume_paid_leave_days(capacity_by_limit, limits, request_start, request_end):
            return _(
                "The paid leave balances valid on the requested dates in %(year)s do not cover every working day. Please contact HR to check the validity periods.",
                year=year,
            )
    return None


def _age_on_year_end(birth_date: date | None, year: int) -> int | None:
    if birth_date is None:
        return None
    return year - birth_date.year


def _age_supplement_days(age: int | None) -> int:
    return age_supplement_days(age)


def _round_half_up(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return (2 * numerator + denominator) // (2 * denominator)


def _contract_calendar_days_in_year(contract: Contract, year: int) -> int:
    year_start, year_end = _leave_request_year_bounds(year)
    start = max(contract.start_date, year_start)
    end = min(contract.end_date or year_end, year_end)
    if start > end:
        return 0
    return _calendar_day_count(start, end)


def _prorate_annual_days(full_year_days: int, contract: Contract, year: int) -> int:
    return _round_half_up(
        full_year_days * _contract_calendar_days_in_year(contract, year),
        _calendar_day_count(date(year, 1, 1), date(year, 12, 31)),
    )


def _distribute_prorated_paid_leave_days(
    full_year_limits: dict[LeaveType, int],
    contract: Contract,
    year: int,
) -> dict[LeaveType, int]:
    full_year_total = sum(full_year_limits.values())
    prorated_total = _prorate_annual_days(full_year_total, contract, year)
    if full_year_total <= 0 or prorated_total <= 0:
        return {leave_type: 0 for leave_type in full_year_limits}

    distributed = {}
    remainders = []
    assigned = 0
    for index, (leave_type, full_year_days) in enumerate(full_year_limits.items()):
        numerator = prorated_total * full_year_days
        days = numerator // full_year_total
        distributed[leave_type] = days
        assigned += days
        remainders.append((numerator % full_year_total, full_year_days, -index, leave_type))

    for _, _, _, leave_type in sorted(remainders, reverse=True)[: prorated_total - assigned]:
        distributed[leave_type] += 1
    return distributed


def _calculated_calendar_leave_limits(contract: Contract, year: int) -> dict[LeaveType, int]:
    user = contract.user
    profile = user.profile
    age = _age_on_year_end(profile.date_of_birth if profile else None, year)
    status_law = contract.contract_type != ContractType.employee_under_the_labour_code
    children = eligible_children(user, year)
    children_count = len(children)
    paid_full_year_limits = {
        LeaveType.basic_leave: 35 if status_law else 20,
        LeaveType.supplementary_leave_based_on_age: 0 if status_law else _age_supplement_days(age),
        LeaveType.supplementary_leave_for_children: (
            7 if children_count >= 3 else 4 if children_count == 2 else 2 if children_count == 1 else 0
        ),
        LeaveType.supplementary_leave_for_children_with_disability: (
            sum(1 for child in children if child.disability) * 2
        ),
        LeaveType.supplementary_leave_for_young_employees: 5 if not status_law and age is not None and 0 <= age <= 18 else 0,
        LeaveType.supplementary_leave_for_reduced_working_capacity: 5 if profile and profile.disability else 0,
    }
    limits = _distribute_prorated_paid_leave_days(paid_full_year_limits, contract, year)
    limits[LeaveType.sick_leave] = _prorate_annual_days(15, contract, year)
    limits[LeaveType.leave_carried_over_from_previous_year] = _paid_leave_remaining_days(contract, year - 1)
    return limits


def _store_previous_leave_limit_values(record: ContractLeaveLimit) -> None:
    if record.imported:
        return
    record.previous_limit_days = record.limit_days
    record.previous_period_start = record.period_start
    record.previous_period_end = record.period_end
    record.previous_imported = record.imported


def _clear_previous_leave_limit_values(record: ContractLeaveLimit) -> None:
    record.previous_limit_days = None
    record.previous_period_start = None
    record.previous_period_end = None
    record.previous_imported = None


def _upsert_leave_limit(contract: Contract, year: int, leave_type: LeaveType, days: int, start: date, end: date) -> None:
    record = ContractLeaveLimit.query.filter_by(contract_id=contract.id, calendar_year=year, leave_type=leave_type).first()
    if record is None:
        record = ContractLeaveLimit(contract_id=contract.id, calendar_year=year, leave_type=leave_type)
    else:
        _store_previous_leave_limit_values(record)
    record.limit_days = max(days, 0)
    record.period_start = start
    record.period_end = end
    record.imported = True
    db.session.add(record)


def _set_leave_year_open(year: int, is_open: bool, imported_by: User | None = None) -> None:
    leave_year = db.session.get(LeaveYear, year) or LeaveYear(year=year)
    leave_year.is_open = is_open
    if is_open:
        leave_year.imported_by_id = imported_by.id if imported_by else None
        leave_year.imported_at = datetime.utcnow()
    db.session.add(leave_year)


def _import_default_leave_limits_for_contract(contract: Contract, year: int) -> None:
    if not _is_contract_active_in_year(contract, year):
        return
    for leave_type, days in _calculated_calendar_leave_limits(contract, year).items():
        if _leave_type_available_for_contract(leave_type, contract.contract_type, contract.user):
            end = (
                date(year, 1, 31)
                if leave_type == LeaveType.leave_carried_over_from_previous_year
                else date(year, 12, 31)
            )
            _upsert_leave_limit(contract, year, leave_type, days, date(year, 1, 1), end)


def _import_default_leave_limits_for_year(year: int) -> None:
    for contract in Contract.query.all():
        _import_default_leave_limits_for_contract(contract, year)


def _copy_range_limits_for_year(year: int) -> None:
    for contract in Contract.query.all():
        if not _is_contract_active_in_year(contract, year):
            continue
        for previous in ContractLeaveLimit.query.filter(
            ContractLeaveLimit.contract_id == contract.id,
            ContractLeaveLimit.leave_type.in_(RANGE_BASED_LEAVE_TYPES),
            ContractLeaveLimit.period_end >= date(year, 1, 1),
            ContractLeaveLimit.period_start <= date(year, 12, 31),
        ).all():
            exists = ContractLeaveLimit.query.filter_by(
                contract_id=contract.id,
                calendar_year=year,
                leave_type=previous.leave_type,
                period_start=previous.period_start,
                period_end=previous.period_end,
            ).first()
            if exists is None:
                db.session.add(
                    ContractLeaveLimit(
                        contract_id=contract.id,
                        calendar_year=year,
                        leave_type=previous.leave_type,
                        limit_days=previous.limit_days,
                        period_start=previous.period_start,
                        period_end=previous.period_end,
                        imported=True,
                    )
                )


def _has_imported_leave_limits_for_contract(contract: Contract, year: int) -> bool:
    return db.session.query(ContractLeaveLimit.id).filter_by(
        contract_id=contract.id,
        calendar_year=year,
        imported=True,
    ).first() is not None


def _has_imported_leave_limits_for_year(year: int) -> bool:
    return (
        db.session.query(ContractLeaveLimit.id)
        .filter_by(calendar_year=year, imported=True)
        .first()
        is not None
    )


def _revert_imported_leave_limit(record: ContractLeaveLimit) -> None:
    if record.previous_limit_days is None:
        db.session.delete(record)
        return
    record.limit_days = record.previous_limit_days
    record.period_start = record.previous_period_start
    record.period_end = record.previous_period_end
    record.imported = bool(record.previous_imported)
    _clear_previous_leave_limit_values(record)
    db.session.add(record)


def _remove_imported_leave_limits_for_contract(contract: Contract, year: int) -> None:
    records = ContractLeaveLimit.query.filter_by(
        contract_id=contract.id,
        calendar_year=year,
        imported=True,
    ).all()
    for record in records:
        _revert_imported_leave_limit(record)


def _remove_imported_leave_limits_for_year(year: int) -> None:
    for record in ContractLeaveLimit.query.filter_by(calendar_year=year, imported=True).all():
        _revert_imported_leave_limit(record)


def _leave_usage_summary(
    contract: Contract,
    year: int,
    categories: list[LeaveRequestCategory] | None = None,
) -> list[dict[str, object]]:
    year_start, year_end = _leave_request_year_bounds(year)
    used_by_category = {category: 0 for category in LeaveRequestCategory}
    requests = LeaveRequest.query.filter(
        LeaveRequest.contract_id == contract.id,
        LeaveRequest.status.in_(BLOCKING_LEAVE_REQUEST_STATUSES),
        LeaveRequest.start_date <= year_end,
        db.or_(LeaveRequest.end_date.is_(None), LeaveRequest.end_date >= year_start),
    ).all()
    for leave_request in requests:
        used_by_category[leave_request.category] += _leave_request_used_days_in_year(leave_request, year)
    summary_categories = categories if categories is not None else list(LeaveRequestCategory)
    return [
        {
            "category": category,
            "label": enum_label(category),
            "used_days": used_by_category[category],
            "available_days": _paid_leave_available_days(contract, year)
            if category == LeaveRequestCategory.paid_leave
            else None,
            "remaining_days": _paid_leave_remaining_days(contract, year)
            if category == LeaveRequestCategory.paid_leave
            else None,
        }
        for category in summary_categories
    ]


def _available_leave_request_categories(user: User, contract: Contract, year: int | None = None) -> list[dict[str, object]]:
    categories = [
        {
            "category": LeaveRequestCategory.paid_leave,
            "label": _("Paid leave"),
            "description": _("Basic leave, supplementary leaves, parental leave, paternity leave, and leave carry-over."),
            "end_required": True,
        },
        {
            "category": LeaveRequestCategory.health_leave,
            "label": _("Health leave"),
            "description": _("Sick leave and sickness benefit."),
            "end_required": False,
        },
    ]
    if _has_child_dependents(user):
        categories.append(
            {
                "category": LeaveRequestCategory.childcare_sickness_benefit,
                "label": _("Childcare sickness benefit"),
                "description": _("Available when a child dependent is recorded on your profile."),
                "end_required": False,
            }
        )
    childbirth_leave_types = {
        LeaveType.maternity_leave,
        LeaveType.childcare_fee,
        LeaveType.childcare_allowance,
    }
    if _has_positive_leave_limit(contract, childbirth_leave_types):
        categories.append(
            {
                "category": LeaveRequestCategory.childbirth_leave,
                "label": _("Childbirth leave"),
                "description": _("Maternity leave, childcare fee, and childcare allowance."),
                "end_required": False,
            }
        )
    categories.append(
        {
            "category": LeaveRequestCategory.exemption_from_obligation_to_work,
            "label": _("Exemption from obligation to work"),
            "description": _("Request time away under an exemption from work obligation."),
            "end_required": False,
        }
    )
    if year is not None and _paid_leave_remaining_days(contract, year) == 0:
        categories.append(
            {
                "category": LeaveRequestCategory.unpaid_leave,
                "label": _("Unpaid leave"),
                "description": _("Available once paid leave days are exhausted for the selected year."),
                "end_required": False,
            }
        )
    return categories


def _hungary_calendar() -> HungaryCalendar:
    return HungaryCalendar()


def _default_is_working_day(day: date) -> bool:
    return _hungary_calendar().is_working_day(day)


def _working_day_status(day: date, overrides_by_day: dict[date, WorkingDayOverride] | None = None) -> dict[str, object]:
    override = (overrides_by_day or {}).get(day)
    default_is_working = _default_is_working_day(day)
    is_working_day = override.is_working_day if override else default_is_working
    return {
        "day": day,
        "is_working_day": is_working_day,
        "default_is_working_day": default_is_working,
        "is_override": override is not None,
        "note": override.note if override else None,
    }


def _month_calendar_days(year: int, month: int) -> list[date | None]:
    first_day = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    day_count = (next_month - first_day).days
    days: list[date | None] = [None] * first_day.weekday()
    days.extend(date(year, month, day) for day in range(1, day_count + 1))
    while len(days) % 7:
        days.append(None)
    return days


BLOCKING_LEAVE_REQUEST_STATUSES = (
    LeaveRequestStatus.pending_approval,
    LeaveRequestStatus.approved,
    LeaveRequestStatus.pending_cancellation,
)


def _leave_request_overlaps_day(leave_request: LeaveRequest, day: date) -> bool:
    end_date = leave_request.end_date or leave_request.start_date
    return leave_request.start_date <= day <= end_date


def _leave_request_end_date(leave_request: LeaveRequest) -> date:
    return leave_request.end_date or leave_request.start_date


def _user_display_name(user: User) -> str:
    return user.profile.full_name if user.profile and user.profile.full_name else user.username


def _leadership_active_on(leadership: Leadership, day: date) -> bool:
    return leadership.start_date <= day and (leadership.end_date is None or leadership.end_date >= day)


def _active_leadership_for_user(user: User, day: date | None = None) -> list[Leadership]:
    active_day = day or local_today()
    return [
        leadership
        for contract in user.contracts
        for leadership in contract.leadership_positions
        if _leadership_active_on(leadership, active_day)
    ]


def _active_leadership_for_user_entity(user: User, legal_entity_id: int, day: date | None = None) -> list[Leadership]:
    active_day = day or local_today()
    return [
        leadership
        for leadership in _active_leadership_for_user(user, active_day)
        if leadership.legal_entity_id == legal_entity_id
    ]


def _is_active_principal_for_entity(user: User, legal_entity_id: int, day: date | None = None) -> bool:
    return any(
        leadership.position == LeadershipPosition.principal
        for leadership in _active_leadership_for_user_entity(user, legal_entity_id, day)
    )


def _can_manage_leaves(user: User) -> bool:
    return can_access_page("manage_leaves", user)


def _accessible_leave_legal_entity_ids(user: User) -> set[int] | None:
    if user.privilege == UserPrivilege.ceo:
        return None
    return {leadership.legal_entity_id for leadership in _active_leadership_for_user(user)}


def _leave_request_query_for_manager(user: User):
    query = LeaveRequest.query.join(Contract)
    legal_entity_ids = _accessible_leave_legal_entity_ids(user)
    if legal_entity_ids is not None:
        if not legal_entity_ids:
            return query.filter(db.false())
        query = query.filter(Contract.legal_entity_id.in_(legal_entity_ids))
    return query


def _can_approve_ceo_part(user: User, leave_request: LeaveRequest, *, automatic: bool = False) -> bool:
    return (
        (automatic or _can_manage_leaves(user))
        and current_leave_approval_policy() != LeaveApprovalPolicy.leadership_only
        and user.privilege == UserPrivilege.ceo
    )


def _can_approve_leadership_part(user: User, leave_request: LeaveRequest) -> bool:
    if not _can_manage_leaves(user) or current_leave_approval_policy() == LeaveApprovalPolicy.ceo_only:
        return False
    leadership_records = _active_leadership_for_user_entity(
        user,
        leave_request.contract.legal_entity_id,
    )
    for leadership in leadership_records:
        if user.id == leave_request.user_id and leadership.position == LeadershipPosition.deputy_principal:
            continue
        return True
    return False


def _can_review_leave_cancellation(user: User, leave_request: LeaveRequest) -> bool:
    return _can_approve_ceo_part(user, leave_request) or _can_approve_leadership_part(user, leave_request)


def _wake_leave_notifications() -> None:
    from .notification_delivery import wake_notifications

    wake_notifications(current_app._get_current_object())


def _apply_automatic_leave_approvals(leave_request: LeaveRequest) -> None:
    applicant = leave_request.user
    if _can_approve_ceo_part(applicant, leave_request, automatic=True):
        leave_request.ceo_approved_by_id = applicant.id
    if (
        current_leave_approval_policy() != LeaveApprovalPolicy.ceo_only
        and _is_active_principal_for_entity(applicant, leave_request.contract.legal_entity_id)
    ):
        leave_request.leadership_approved_by_id = applicant.id
    if _leave_request_is_fully_approved(leave_request):
        leave_request.status = LeaveRequestStatus.approved
        leave_request.decided_by_id = applicant.id


def _leave_request_is_fully_approved(leave_request: LeaveRequest) -> bool:
    return approvals_satisfy_policy(leave_request, current_leave_approval_policy())


def _approve_leave_request(leave_request: LeaveRequest, approver: User) -> list[str]:
    approved_parts = []
    if leave_request.status != LeaveRequestStatus.pending_approval:
        return approved_parts
    if leave_request.ceo_approved_by_id is None and _can_approve_ceo_part(approver, leave_request):
        leave_request.ceo_approved_by_id = approver.id
        approved_parts.append("CEO")
    if leave_request.leadership_approved_by_id is None and _can_approve_leadership_part(approver, leave_request):
        leave_request.leadership_approved_by_id = approver.id
        approved_parts.append("principal/deputy principal")
    if _leave_request_is_fully_approved(leave_request):
        leave_request.status = LeaveRequestStatus.approved
        leave_request.decided_by_id = approver.id
    return approved_parts


def _manager_review_leave_requests(user: User, limit: int | None = None) -> list[LeaveRequest]:
    if not _can_manage_leaves(user):
        return []
    query = (
        _leave_request_query_for_manager(user)
        .filter(
            LeaveRequest.status.in_(
                (
                    LeaveRequestStatus.pending_approval,
                    LeaveRequestStatus.pending_cancellation,
                )
            )
        )
        .order_by(LeaveRequest.start_date.asc(), LeaveRequest.id.asc())
    )
    actionable_requests = [
        leave_request
        for leave_request in query.all()
        if (leave_request.status == LeaveRequestStatus.pending_cancellation
            and _can_review_leave_cancellation(user, leave_request))
        or (
            leave_request.status == LeaveRequestStatus.pending_approval
            and (
                (
                    leave_request.ceo_approved_by_id is None
                    and _can_approve_ceo_part(user, leave_request)
                )
                or (
                    leave_request.leadership_approved_by_id is None
                    and _can_approve_leadership_part(user, leave_request)
                )
            )
        )
    ]
    if limit:
        return actionable_requests[:limit]
    return actionable_requests


def _max_workplace_leave_count_during_request(leave_request: LeaveRequest) -> int:
    request_end_date = _leave_request_end_date(leave_request)
    overlapping_requests = (
        LeaveRequest.query.join(Contract)
        .filter(
            Contract.place_of_work_id == leave_request.contract.place_of_work_id,
            LeaveRequest.status.in_(BLOCKING_LEAVE_REQUEST_STATUSES),
            LeaveRequest.start_date <= request_end_date,
            db.or_(
                db.and_(LeaveRequest.end_date.is_(None), LeaveRequest.start_date >= leave_request.start_date),
                LeaveRequest.end_date >= leave_request.start_date,
            ),
        )
        .all()
    )
    max_count = 0
    current_day = leave_request.start_date
    while current_day <= request_end_date:
        employees_on_leave = {
            overlapping_request.user_id
            for overlapping_request in overlapping_requests
            if _leave_request_overlaps_day(overlapping_request, current_day)
        }
        max_count = max(max_count, len(employees_on_leave))
        current_day = date.fromordinal(current_day.toordinal() + 1)
    return max_count


def _overlapping_leave_request(
    user_id: int,
    contract_id: int,
    start_date: date,
    end_date: date | None,
) -> LeaveRequest | None:
    request_end_date = end_date or start_date
    return (
        LeaveRequest.query.filter(
            LeaveRequest.user_id == user_id,
            LeaveRequest.contract_id == contract_id,
            LeaveRequest.status.in_(BLOCKING_LEAVE_REQUEST_STATUSES),
            LeaveRequest.start_date <= request_end_date,
            db.or_(
                db.and_(LeaveRequest.end_date.is_(None), LeaveRequest.start_date >= start_date),
                LeaveRequest.end_date >= start_date,
            ),
        )
        .order_by(LeaveRequest.start_date.asc(), LeaveRequest.id.asc())
        .first()
    )


def _can_manage_privileges(user: User) -> bool:
    return can_access_page("manage_privileges", user)


def _can_assign_privileges(user: User) -> bool:
    return can_access_page("manage_privileges", user)


def privilege_manager_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped_view(*args, **kwargs):
        if not can_access_endpoint(request.endpoint, current_user, **(request.view_args or {})):
            abort(403)
        return view_func(*args, **kwargs)

    return wrapped_view




def privilege_assignment_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped_view(*args, **kwargs):
        if not can_access_endpoint(request.endpoint, current_user, **(request.view_args or {})):
            abort(403)
        return view_func(*args, **kwargs)

    return wrapped_view


def init_routes(app):
    app.jinja_env.globals["enum_label"] = enum_label

    @app.context_processor
    def inject_global_template_context():
        return {
            "can_manage_leaves_global": _can_manage_leaves(current_user),
            "current_locale": get_locale(),
            "supported_locales": SUPPORTED_LOCALES,
            "leave_approval_policy": current_leave_approval_policy() if current_user.is_authenticated else None,
            "leave_approval_policy_labels": POLICY_LABELS,
            "leave_approval_policy_descriptions": POLICY_DESCRIPTIONS,
        }


    @app.route("/language", methods=["POST"])
    def set_language():
        locale = request.form.get("locale", "").strip()
        if locale in SUPPORTED_LOCALES:
            session["locale"] = locale
            flash(_("Language updated."), "success")
        else:
            flash(_("Selected language is not supported."), "error")

        next_url = request.form.get("next") or request.referrer or url_for("index")
        parsed_next_url = urlsplit(next_url)
        if parsed_next_url.netloc and parsed_next_url.netloc != request.host:
            next_url = url_for("index")
        return redirect(next_url)

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(landing_url())
        return redirect(url_for("login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            if not email or not username or not password:
                flash(_("Email, username and password are required."), "error")
                return render_template("register.html")

            if User.query.filter((User.email == email) | (User.username == username)).first():
                flash(_("Email or username already exists."), "error")
                return render_template("register.html")

            user = User(email=email, username=username, privilege=UserPrivilege.employee)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()

            profile = UserProfile(user_id=user.id)
            db.session.add(profile)
            db.session.commit()

            login_user(user)
            flash(_('Registration successful. Your privilege is set to employee until HR or the Director updates it.'), "success")
            return redirect(url_for("edit_profile") if can_access_page("edit_profile") else landing_url())

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            login_identifier = request.form.get("login", "").strip()
            password = request.form.get("password", "")

            user = User.query.filter(
                (User.email == login_identifier.lower()) | (User.username == login_identifier)
            ).first()

            if not user or not user.check_password(password):
                flash(_("Invalid credentials."), "error")
                return render_template("login.html")

            login_user(user)
            flash(_("Logged in successfully."), "success")
            return redirect(landing_url())

        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash(_("Logged out."), "success")
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        today = date.today()
        current_contract = _current_active_contract(current_user)
        dashboard_leave_usage_summary = []
        if current_contract:
            dashboard_leave_categories = [
                item["category"]
                for item in _available_leave_request_categories(current_user, current_contract, today.year)
            ]
            dashboard_leave_usage_summary = _leave_usage_summary(
                current_contract,
                today.year,
                dashboard_leave_categories,
            )

        return render_template(
            "dashboard.html",
            can_manage_privileges=_can_manage_privileges(current_user),
            can_assign_privileges=_can_assign_privileges(current_user),
            can_manage_user_profiles=can_access_page("manage_user_profiles"),
            can_manage_leadership=can_access_page("manage_leadership"),
            can_manage_leave_limits=can_access_page("manage_leave_limits"),
            can_manage_leaves=_can_manage_leaves(current_user),
            pending_leave_requests=_manager_review_leave_requests(current_user, limit=6)
            if _can_manage_leaves(current_user)
            else [],
            profile_status_label=_profile_status_label(current_user.profile),
            profile_completion_percentage=_profile_completion_percentage(current_user.profile),
            dashboard_leave_contract=current_contract,
            dashboard_leave_usage_summary=dashboard_leave_usage_summary,
            dashboard_leave_usage_year=today.year,
            **birthday_context(current_user),
        )

    @app.route("/leaves", methods=["GET", "POST"])
    @login_required
    def leaves():
        today = date.today()
        active_contracts = _active_contracts_for_user(current_user, today)
        selected_contract_id = request.values.get("contract_id", type=int)
        current_contract = _current_active_contract(current_user)
        selected_contract = None
        if selected_contract_id:
            selected_contract = next(
                (contract for contract in active_contracts if contract.id == selected_contract_id),
                None,
            )
            if selected_contract is None:
                flash(_("Please select one of your active contracts."), "error")
        if selected_contract is None:
            selected_contract = current_contract

        selected_year = request.values.get("year", type=int) or today.year
        selected_month = request.values.get("month", type=int) or today.month
        if selected_year < 1970 or selected_year > 2100:
            selected_year = today.year
        if selected_month < 1 or selected_month > 12:
            selected_month = today.month

        if request.method == "POST":
            action = request.form.get("action", "submit")
            # Serialise submissions and decisions with changes to the global rule.
            get_leave_approval_settings(for_update=True)
            if selected_contract is None:
                flash(_("You need an active contract before applying for leave."), "error")
                return redirect(url_for("leaves"))

            if action in {"cancel", "undo_cancel"}:
                leave_request_id = request.form.get("leave_request_id", type=int)
                leave_request = LeaveRequest.query.filter(
                    LeaveRequest.id == leave_request_id,
                    LeaveRequest.user_id == current_user.id,
                    LeaveRequest.contract_id == selected_contract.id,
                ).with_for_update().populate_existing().first()
                if leave_request is None:
                    flash(_("Leave request not found."), "error")
                elif action == "cancel":
                    before = snapshot_leave_request(leave_request)
                    if leave_request.status == LeaveRequestStatus.pending_approval:
                        leave_request.status = LeaveRequestStatus.cancelled
                        leave_request.decided_by_id = current_user.id
                        record_leave_change(leave_request, before, current_user, action=action)
                        db.session.commit()
                        _wake_leave_notifications()
                        flash(_("Pending leave request cancelled."), "success")
                    elif leave_request.status == LeaveRequestStatus.approved:
                        leave_request.status = LeaveRequestStatus.pending_cancellation
                        record_leave_change(leave_request, before, current_user, action=action)
                        db.session.commit()
                        _wake_leave_notifications()
                        flash(_("Leave cancellation requested."), "success")
                    else:
                        flash(_("Only approved or pending approval leaves can be cancelled here."), "error")
                elif leave_request.status == LeaveRequestStatus.pending_cancellation:
                    before = snapshot_leave_request(leave_request)
                    leave_request.status = LeaveRequestStatus.approved
                    record_leave_change(leave_request, before, current_user, action=action)
                    db.session.commit()
                    _wake_leave_notifications()
                    flash(_("Leave cancellation request undone."), "success")
                else:
                    flash(_("Only pending cancellation leaves can be undone."), "error")
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )
            if action != "submit":
                flash(_("Invalid leave action."), "error")
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )

            category_value = request.form.get("category")
            start_date = parse_iso_date(request.form.get("start_date"))
            end_date = parse_iso_date(request.form.get("end_date"))
            if start_date is None:
                flash(_("Start date is required."), "error")
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )
            available_categories = _available_leave_request_categories(current_user, selected_contract, start_date.year)
            available_by_value = {item["category"].value: item for item in available_categories}
            category_definition = available_by_value.get(category_value)
            if category_definition is None:
                flash(_("Please choose an available leave type."), "error")
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )
            if category_definition["end_required"] and end_date is None:
                flash(_("Paid leave requests require both start and end date."), "error")
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )
            if end_date is not None and end_date < start_date:
                flash(_("End date cannot be earlier than the start date."), "error")
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )
            if not _is_contract_active_on(selected_contract, start_date):
                flash(_("The request start date must fall within the selected active contract."), "error")
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )
            closed_years = [
                year
                for year in range(start_date.year, (end_date or start_date).year + 1)
                if not _is_leave_year_open(year)
            ]
            if closed_years:
                flash(
                    _("The following calendar years are not open for leave requests: %(years)s. Please contact HR.", years=", ".join(str(year) for year in closed_years)),
                    "error",
                )
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )
            if end_date is not None and not _is_contract_active_on(selected_contract, end_date):
                flash(_("The request end date must fall within the selected active contract."), "error")
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )

            conflicting_request = _overlapping_leave_request(
                current_user.id,
                selected_contract.id,
                start_date,
                end_date,
            )
            if conflicting_request is not None:
                conflicting_end_date = conflicting_request.end_date or conflicting_request.start_date
                flash(
                    _("This request overlaps with an existing %(status)s leave request (%(start)s → %(end)s).", status=enum_label(conflicting_request.status), start=conflicting_request.start_date, end=conflicting_end_date),
                    "error",
                )
                return redirect(
                    url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                )

            if category_definition["category"] == LeaveRequestCategory.paid_leave:
                request_end_date = end_date or start_date
                capacity_error = _paid_leave_request_capacity_error(selected_contract, start_date, request_end_date)
                if capacity_error:
                    flash(capacity_error, "error")
                    return redirect(
                        url_for("leaves", contract_id=selected_contract.id, year=selected_year, month=selected_month)
                    )

            leave_request = LeaveRequest(
                user_id=current_user.id,
                contract_id=selected_contract.id,
                category=LeaveRequestCategory(category_value),
                start_date=start_date,
                end_date=end_date,
                status=LeaveRequestStatus.pending_approval,
                note=_normalize_optional_text(request.form.get("note")),
            )
            db.session.add(leave_request)
            db.session.flush()
            _apply_automatic_leave_approvals(leave_request)
            record_leave_change(leave_request, None, current_user, action="submit")
            db.session.commit()
            _wake_leave_notifications()
            if leave_request.status == LeaveRequestStatus.approved:
                flash(_("Leave request submitted and automatically approved."), "success")
            else:
                flash(_("Leave request submitted and marked as pending approval."), "success")
            return redirect(
                url_for("leaves", contract_id=selected_contract.id, year=start_date.year, month=start_date.month)
            )

        available_categories = (
            _available_leave_request_categories(current_user, selected_contract, selected_year) if selected_contract else []
        )
        month_start = date(selected_year, selected_month, 1)
        if selected_month == 12:
            next_month = date(selected_year + 1, 1, 1)
            previous_month = date(selected_year, 11, 1)
        elif selected_month == 1:
            next_month = date(selected_year, 2, 1)
            previous_month = date(selected_year - 1, 12, 1)
        else:
            next_month = date(selected_year, selected_month + 1, 1)
            previous_month = date(selected_year, selected_month - 1, 1)
        month_requests = []
        leave_usage_summary = (
            _leave_usage_summary(
                selected_contract,
                selected_year,
                [item["category"] for item in available_categories],
            )
            if selected_contract
            else []
        )
        if selected_contract:
            query_end = next_month
            month_requests = (
                LeaveRequest.query.filter(
                    LeaveRequest.user_id == current_user.id,
                    LeaveRequest.contract_id == selected_contract.id,
                    LeaveRequest.start_date < query_end,
                    db.or_(
                        db.and_(LeaveRequest.end_date.is_(None), LeaveRequest.start_date >= month_start),
                        LeaveRequest.end_date >= month_start,
                    ),
                )
                .order_by(LeaveRequest.start_date.asc(), LeaveRequest.id.asc())
                .all()
            )
        return render_template(
            "leaves.html",
            named_approval_description=approval_description(selected_contract, current_user, current_leave_approval_policy()),
            active_contracts=active_contracts,
            selected_contract=selected_contract,
            available_categories=available_categories,
            calendar_days=_month_calendar_days(selected_year, selected_month),
            month_requests=month_requests,
            selected_year=selected_year,
            selected_month=selected_month,
            month_label=month_start.strftime("%B %Y"),
            previous_month=previous_month,
            next_month=next_month,
            leave_request_overlaps_day=_leave_request_overlaps_day,
            leave_usage_summary=leave_usage_summary,
        )

    @app.route("/users/privileges", methods=["GET", "POST"])
    @privilege_assignment_required
    def manage_privileges():
        if request.method == "POST":
            user_id = request.form.get("user_id", type=int)
            privilege_raw = request.form.get("privilege", UserPrivilege.employee.value)
            user = db.session.get(User, user_id)

            if user is None:
                flash(_("User not found."), "error")
                return redirect(url_for("manage_privileges"))

            try:
                privilege = UserPrivilege(privilege_raw)
            except ValueError:
                flash(_("Invalid privilege selected."), "error")
                return redirect(url_for("manage_privileges"))

            if privilege not in MANAGEABLE_PRIVILEGES:
                flash(_("That privilege cannot be assigned here."), "error")
                return redirect(url_for("manage_privileges"))

            # Keep the developer's recovery path to the access matrix intact,
            # including a crafted role-change submission for their own account.
            if user.id == current_user.id and current_user.privilege == UserPrivilege.developer and privilege != UserPrivilege.developer:
                flash(_("You cannot remove your own developer privilege."), "error")
                return redirect(url_for("manage_privileges"))

            previous_tasks = snapshot_pending_leave_tasks()
            user.privilege = privilege
            invalidate_access_cache()
            record_reviewer_changes(previous_tasks, current_user)
            db.session.commit()
            _wake_leave_notifications()
            flash(_("Updated %(username)s to %(privilege)s privilege.", username=user.username, privilege=enum_label(privilege)), "success")
            return redirect(url_for("manage_privileges") if can_access_page("manage_privileges") else landing_url())

        users = User.query.order_by(User.id.asc()).all()
        return render_template(
            "manage_privileges.html",
            users=users,
            privileges=MANAGEABLE_PRIVILEGES,
        )

    @app.route("/password", methods=["GET", "POST"])
    @login_required
    def change_password():
        session.setdefault("change_password_csrf_token", token_urlsafe(32))
        if request.method == "POST":
            if not compare_digest(session["change_password_csrf_token"].encode(), request.form.get("csrf_token", "").encode()):
                abort(400)
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            new_password_confirm = request.form.get("new_password_confirm", "")
            old_hash, old_email = current_user.password_hash, current_user.email

            if not current_user.check_password(current_password):
                flash(_("Current password is incorrect."), "error")
                return render_template("change_password.html")

            if not new_password:
                flash(_("New password is required."), "error")
                return render_template("change_password.html")

            if new_password != new_password_confirm:
                flash(_("New password and confirmation do not match."), "error")
                return render_template("change_password.html")

            changed = db.session.execute(db.update(User).where(
                User.id == current_user.id,
                User.password_hash == old_hash,
                User.email == old_email,
            ).values(password_hash=generate_password_hash(new_password)).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                db.session.rollback()
                logout_user()
                flash(_("Your account details changed. Log in again before retrying."), "error")
                return redirect(url_for("login"))
            db.session.commit()
            # Keep this re-authenticated session; other sessions carry the old
            # password-bound identity and must sign in again.
            login_user(current_user._get_current_object())
            flash(_("Password updated successfully."), "success")
            return redirect(landing_url())

        return render_template("change_password.html")

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def edit_profile():
        profile = current_user.profile or UserProfile(user_id=current_user.id)
        if request.method == "POST":
            email_changed = False
            observed_email = current_user.email
            observed_password_hash = current_user.password_hash
            if "email" in request.form:
                from .mail_settings import _has_control, _valid_email

                csrf_token = session.get("profile_email_csrf_token", "")
                if not csrf_token or not compare_digest(csrf_token.encode(), request.form.get("csrf_token", "").encode()):
                    abort(400)

                raw_email = request.form.get("email", "")
                email = raw_email.strip().lower()
                if _has_control(raw_email) or len(email) > 120 or not _valid_email(email):
                    flash(_("Enter a valid email address of at most 120 characters."), "error")
                    return _render_profile_editor(profile, current_user)

                email_changed = email != observed_email.strip().lower()
                if email_changed:
                    password = request.form.get("current_password", "")
                    if not password or not current_user.check_password(password):
                        return _render_profile_editor(
                            profile,
                            current_user,
                            email_confirmation_required=True,
                            email_confirmation_error=_("Current password is incorrect.") if password else None,
                        )

                    existing = db.session.execute(
                        db.select(User.id).where(db.func.lower(User.email) == email, User.id != current_user.id).limit(1)
                    ).scalar_one_or_none()
                    if existing is not None:
                        flash(_("This email address is already used by another account."), "error")
                        return _render_profile_editor(profile, current_user)

            errors = _save_profile_from_form(profile)
            if errors:
                for err in errors:
                    flash(err, "error")
                return _render_profile_editor(profile, current_user)

            try:
                if email_changed:
                    # Keep the personal details and verified email in one
                    # transaction. Do not flush the profile before checking
                    # whether another request changed the account meanwhile.
                    with db.session.no_autoflush:
                        changed = db.session.execute(
                            db.update(User).where(
                                User.id == current_user.id,
                                User.email == observed_email,
                                User.password_hash == observed_password_hash,
                            ).values(email=email).execution_options(synchronize_session=False)
                        )
                    if changed.rowcount != 1:
                        db.session.rollback()
                        flash(_("Your account changed while saving. Refresh the page and try again."), "error")
                        return _render_profile_editor(profile, current_user)
                db.session.add(profile)
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash(_("This email address is already used by another account."), "error")
                return _render_profile_editor(profile, current_user)
            flash(_("Profile updated."), "success")
            return redirect(landing_url())

        return _render_profile_editor(profile, current_user)

    @app.post("/profile/email")
    @login_required
    def change_account_email():
        from .mail_settings import _has_control, _valid_email

        csrf_token = session.get("profile_email_csrf_token", "")
        if not csrf_token or not compare_digest(csrf_token.encode(), request.form.get("csrf_token", "").encode()):
            abort(400)

        raw_email = request.form.get("email", "")
        email = raw_email.strip().lower()
        if _has_control(raw_email) or len(email) > 120 or not _valid_email(email):
            flash(_("Enter a valid email address of at most 120 characters."), "error")
            return redirect(url_for("edit_profile"))
        observed_email = current_user.email
        observed_password_hash = current_user.password_hash
        if not current_user.check_password(request.form.get("current_password", "")):
            flash(_("Current password is incorrect."), "error")
            return redirect(url_for("edit_profile"))

        # The account comes only from the authenticated session. Form-supplied
        # user ids and personal-profile fields cannot edit another account.
        existing = db.session.execute(
            db.select(User.id).where(db.func.lower(User.email) == email, User.id != current_user.id).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            flash(_("This email address is already used by another account."), "error")
            return redirect(url_for("edit_profile"))

        try:
            # A password reset or a different email change may finish between
            # reauthentication and this write. Only update the verified state.
            changed = db.session.execute(
                db.update(User).where(
                    User.id == current_user.id,
                    User.email == observed_email,
                    User.password_hash == observed_password_hash,
                ).values(email=email).execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                db.session.rollback()
                flash(_("Your account changed while saving. Refresh the page and try again."), "error")
                return redirect(url_for("edit_profile"))
            db.session.commit()
        except IntegrityError:
            # Another request may claim the address after the duplicate check.
            db.session.rollback()
            flash(_("This email address is already used by another account."), "error")
            return redirect(url_for("edit_profile"))
        flash(_("Account email address updated. Future emails will be sent to this address."), "success")
        return redirect(url_for("edit_profile"))

    @app.route("/users/profiles")
    @privilege_manager_required
    def manage_user_profiles():
        today = local_today()
        selected_status = request.args.get("status", "active")
        if selected_status not in {"active", "inactive", "all"}:
            selected_status = "active"
        selected_profile_state = request.args.get("profile_state", "all")
        if selected_profile_state not in {"empty", "partial", "complete", "all"}:
            selected_profile_state = "all"
        search = request.args.get("q", "").strip()
        users = User.query.options(joinedload(User.profile), joinedload(User.contracts)).all()
        active_user_ids = {
            user.id for user in users
            if any(_is_contract_active_on(contract, today) for contract in user.contracts)
        }
        profile_states = {user.id: _profile_completion_state(user.profile) for user in users}
        ceo_count = sum(user.privilege == UserPrivilege.ceo for user in users)
        if search:
            search_key = search.casefold()
            users = [user for user in users if any(search_key in value.casefold() for value in (
                user.username, user.email, user.profile.full_name or "" if user.profile else "",
            ))]
        if selected_profile_state != "all":
            users = [user for user in users if profile_states[user.id] == selected_profile_state]
        counts = {
            "all": len(users),
            "active": sum(user.id in active_user_ids for user in users),
            "inactive": sum(user.id not in active_user_ids for user in users),
        }
        if selected_status != "all":
            users = [user for user in users if (user.id in active_user_ids) == (selected_status == "active")]
        users.sort(key=lambda user: (_user_display_name(user).casefold(), user.id))
        session.setdefault("profile_delete_csrf_token", token_urlsafe(32))
        return render_template(
            "manage_user_profiles.html", users=users, active_user_ids=active_user_ids,
            profile_states=profile_states, selected_status=selected_status,
            selected_profile_state=selected_profile_state, search=search, counts=counts,
            ceo_count=ceo_count, csrf_token=session["profile_delete_csrf_token"],
        )

    @app.route("/users/<int:user_id>/delete", methods=["POST"])
    @privilege_manager_required
    def delete_user_profile(user_id: int):
        csrf_token = session.get("profile_delete_csrf_token")
        if not csrf_token or not compare_digest(csrf_token.encode(), request.form.get("csrf_token", "").encode()):
            abort(400)
        if not current_user.check_password(request.form.get("manager_password", "")):
            flash(_("Your password is incorrect. The user was not deleted."), "error")
            return redirect(url_for("manage_user_profiles"))
        if user_id == current_user.id:
            flash(_("You cannot delete your own account."), "error")
            return redirect(url_for("manage_user_profiles"))

        from .account_deletion import AccountDeletionConflict, delete_user_account

        try:
            # Lock the CEO set in a consistent order so concurrent deletions
            # cannot both remove the last remaining account with this role.
            ceos = User.query.filter_by(privilege=UserPrivilege.ceo).order_by(User.id).with_for_update().all()
            target_user = User.query.filter_by(id=user_id).with_for_update().populate_existing().first()
            if target_user is None:
                flash(_("User not found."), "error")
            elif target_user.privilege == UserPrivilege.ceo and len(ceos) <= 1:
                flash(_("The last CEO account cannot be deleted."), "error")
            else:
                username = target_user.username
                delete_user_account(target_user)
                db.session.commit()
                flash(_("Deleted the account and personal records for %(username)s.", username=username), "success")
                return redirect(url_for("manage_user_profiles"))
            db.session.rollback()
        except (SQLAlchemyError, AccountDeletionConflict):
            db.session.rollback()
            flash(_("The user could not be deleted. No changes were saved. Please try again."), "error")
        return redirect(url_for("manage_user_profiles"))

    @app.route("/users/<int:user_id>/profile", methods=["GET", "POST"])
    @privilege_manager_required
    def edit_user_profile(user_id: int):
        target_user = db.session.get(User, user_id)
        if target_user is None:
            flash(_("User not found."), "error")
            return redirect(url_for("manage_user_profiles"))

        profile = target_user.profile or UserProfile(user_id=target_user.id)
        if request.method == "POST":
            errors = _save_profile_from_form(profile)
            if errors:
                for err in errors:
                    flash(err, "error")
                return _render_profile_editor(profile, target_user, manager_mode=True)

            db.session.add(profile)
            db.session.commit()
            flash(_("Updated profile for %(username)s.", username=target_user.username), "success")
            return redirect(url_for("manage_user_profiles"))

        return _render_profile_editor(profile, target_user, manager_mode=True)

    @app.route("/dependents")
    @login_required
    def manage_dependents():
        dependents = Dependent.query.filter_by(user_id=current_user.id).order_by(Dependent.name, Dependent.id).all()
        return render_template("dependents.html", dependents=dependents)

    def dependent_editor(dependent=None):
        session.setdefault("dependent_csrf_token", token_urlsafe(32))
        today = local_today()
        if request.method == "POST":
            if not compare_digest(session["dependent_csrf_token"].encode(), request.form.get("csrf_token", "").encode()):
                abort(400)
            values = {field: _normalize_optional_text(request.form.get(field))
                      for field in ("name", "social_security_number", "disability")}
            errors = []
            if not values["name"]:
                errors.append(_("Dependent name is required."))
            elif len(values["name"]) > 120:
                errors.append(_("Dependent name must be at most 120 characters."))
            try:
                values["dependent_type"] = DependentType(request.form.get("dependent_type", ""))
            except ValueError:
                errors.append(_("Dependent type is required."))
            ssn = values["social_security_number"]
            if not ssn or len(ssn) != 9 or not ssn.isascii() or not ssn.isdigit():
                errors.append(_("Enter the dependent's social security number using exactly nine digits."))
            for field in ("date_of_birth", "dependency_start"):
                try:
                    values[field] = parse_iso_date(request.form.get(field))
                except ValueError:
                    values[field] = None
            if not values["date_of_birth"] or not values["dependency_start"]:
                errors.append(_("Enter a valid date of birth and dependency start date."))
            else:
                if values["date_of_birth"] > today:
                    errors.append(_("The date of birth cannot be in the future."))
                if values["dependency_start"] < values["date_of_birth"]:
                    errors.append(_("Dependency cannot start before the dependent's birth."))
            if values["disability"] and len(values["disability"]) > 255:
                errors.append(_("The disability or illness note must be at most 255 characters."))
            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                editing = dependent is not None
                dependent = dependent or Dependent(user_id=current_user.id)
                for field, value in values.items():
                    setattr(dependent, field, value)
                db.session.add(dependent)
                db.session.commit()
                flash(_("Dependent updated.") if editing else _("Dependent added."), "success")
                return redirect(url_for("manage_dependents"))
        return render_template("dependent_form.html", dependent=dependent, dependent_types=DependentType,
                               csrf_token=session["dependent_csrf_token"], latest_birth_date=today.isoformat())

    @app.route("/dependents/add", methods=["GET", "POST"])
    @login_required
    def add_dependent():
        return dependent_editor()

    @app.route("/dependents/<int:dependent_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_dependent(dependent_id):
        dependent = Dependent.query.filter_by(id=dependent_id, user_id=current_user.id).first_or_404()
        return dependent_editor(dependent)

    def legacy_qualification_redirect():
        # Metadata is now maintained by HR/Directors in the unified workflow.
        # Old forms must never write to the retained migration source tables.
        if request.method == "POST":
            abort(403)
        return redirect(url_for("qualifications.index"))

    @app.route("/qualifications/add", methods=["GET", "POST"])
    @login_required
    def add_qualification():
        return legacy_qualification_redirect()

    @app.route("/qualifications/<int:qualification_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_qualification(qualification_id):
        return legacy_qualification_redirect()

    @app.route("/legal-entities")
    @privilege_manager_required
    def manage_legal_entities():
        editing_id = request.args.get("edit", type=int)
        if editing_id:
            return redirect(url_for("edit_legal_entity", entity_id=editing_id))
        return render_template(
            "manage_legal_entities.html",
            entities=LegalEntity.query.order_by(LegalEntity.name.asc()).all(),
            format_legal_entity_tax_number=_format_legal_entity_tax_number,
        )

    def legal_entity_form(entity=None):
        editing = entity is not None
        if request.method == "POST":
            name = _normalize_optional_text(request.form.get("name"))
            address = _normalize_optional_text(request.form.get("address"))
            om_id = _normalize_optional_text(request.form.get("om_id"))
            tax_number = _normalize_legal_entity_tax_number(request.form.get("tax_number"))
            errors = []
            if not name or not address or not tax_number:
                errors.append(_("Name, address, and tax number are required."))
            errors.extend([
                _validate_om_id(om_id),
                _validate_digit_field(_("Tax number"), tax_number, 11),
            ])
            errors = [error for error in errors if error]
            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                entity = entity or LegalEntity()
                entity.name, entity.address = name, address
                entity.om_id, entity.tax_number = om_id, tax_number
                db.session.add(entity)
                db.session.commit()
                flash(_("Legal entity updated.") if editing else _("Legal entity saved."), "success")
                return redirect(url_for("manage_legal_entities"))
        return render_template(
            "legal_entity_form.html", entity=entity,
            format_legal_entity_tax_number=_format_legal_entity_tax_number,
        )

    @app.route("/legal-entities/new", methods=["GET", "POST"])
    @privilege_manager_required
    def create_legal_entity():
        return legal_entity_form()

    @app.route("/legal-entities/<int:entity_id>/edit", methods=["GET", "POST"])
    @privilege_manager_required
    def edit_legal_entity(entity_id):
        return legal_entity_form(db.get_or_404(LegalEntity, entity_id))

    @app.route("/places-of-work")
    @privilege_manager_required
    def manage_places_of_work():
        editing_id = request.args.get("edit", type=int)
        if editing_id:
            return redirect(url_for("edit_place_of_work", place_id=editing_id))
        places = PlaceOfWork.query.join(LegalEntity).order_by(LegalEntity.name.asc(), PlaceOfWork.address.asc()).all()
        return render_template("manage_places_of_work.html", places=places)

    def place_of_work_form(place=None):
        editing = place is not None
        if request.method == "POST":
            legal_entity_id = request.form.get("legal_entity_id", type=int)
            address = _normalize_optional_text(request.form.get("address"))
            if not legal_entity_id or not address:
                flash(_("Employer and address are required."), "error")
            elif db.session.get(LegalEntity, legal_entity_id) is None:
                flash(_("Selected employer does not exist."), "error")
            else:
                place = place or PlaceOfWork()
                place.legal_entity_id, place.address = legal_entity_id, address
                db.session.add(place)
                db.session.commit()
                flash(_("Place of work updated.") if editing else _("Place of work saved."), "success")
                return redirect(url_for("manage_places_of_work"))
        entities = LegalEntity.query.order_by(LegalEntity.name.asc()).all()
        return render_template("place_of_work_form.html", place=place, entities=entities)

    @app.route("/places-of-work/new", methods=["GET", "POST"])
    @privilege_manager_required
    def create_place_of_work():
        return place_of_work_form()

    @app.route("/places-of-work/<int:place_id>/edit", methods=["GET", "POST"])
    @privilege_manager_required
    def edit_place_of_work(place_id):
        return place_of_work_form(db.get_or_404(PlaceOfWork, place_id))

    @app.route("/contracts")
    @privilege_manager_required
    def manage_contracts():
        today = date.today()
        selected_status = request.args.get("status", "active")
        if selected_status not in {"active", "upcoming", "ended", "all"}:
            selected_status = "active"
        search = request.args.get("q", "").strip()
        selected_entity_id = request.args.get("legal_entity_id", type=int)
        query = Contract.query.join(User).outerjoin(UserProfile).join(LegalEntity)
        active = db.and_(Contract.start_date <= today, db.or_(Contract.end_date.is_(None), Contract.end_date >= today))
        upcoming = Contract.start_date > today
        ended = Contract.end_date < today
        if search:
            query = query.filter(db.or_(
                User.username.icontains(search, autoescape=True),
                UserProfile.full_name.icontains(search, autoescape=True),
                Contract.job_title.icontains(search, autoescape=True),
                LegalEntity.name.icontains(search, autoescape=True),
                Contract.id == int(search) if search.isdecimal() and len(search) < 10 else db.false(),
            ))
        if selected_entity_id:
            query = query.filter(Contract.legal_entity_id == selected_entity_id)
        counts = {key: query.filter(condition).count() for key, condition in
                  [("active", active), ("upcoming", upcoming), ("ended", ended)]}
        counts["all"] = sum(counts.values())
        if selected_status != "all":
            query = query.filter({"active": active, "upcoming": upcoming, "ended": ended}[selected_status])
        contracts = query.options(
            joinedload(Contract.user).joinedload(User.profile),
            joinedload(Contract.employer),
            joinedload(Contract.place_of_work),
        ).order_by(
            db.case((active, 0), (upcoming, 1), else_=2),
            Contract.start_date.desc(), Contract.id.desc(),
        ).all()
        return render_template(
            "manage_contracts.html", contracts=contracts, counts=counts, today=today,
            selected_status=selected_status, search=search, selected_entity_id=selected_entity_id,
            legal_entities=LegalEntity.query.order_by(LegalEntity.name.asc()).all(),
            user_display_name=_user_display_name,
        )

    @app.route("/contracts/new")
    @privilege_manager_required
    def select_contract_employee():
        user_id = request.args.get("user_id", type=int)
        if user_id:
            if db.session.get(User, user_id) is None:
                abort(404)
            return redirect(url_for("create_contract", user_id=user_id))
        return render_template("contract_employee.html", users=User.query.order_by(User.username.asc()).all(), user_display_name=_user_display_name)

    @app.route("/users/<int:user_id>/contracts/new", methods=["GET", "POST"])
    @privilege_manager_required
    def create_contract(user_id: int):
        target_user = db.session.get(User, user_id)
        if target_user is None:
            flash(_("User not found."), "error")
            return redirect(url_for("manage_contracts"))

        contract = Contract(user_id=target_user.id)
        if request.method == "POST":
            errors = _save_contract_from_form(contract)
            if errors:
                for err in errors:
                    flash(err, "error")
            else:
                db.session.add(contract)
                db.session.commit()
                flash(_("Contract created for %(username)s.", username=target_user.username), "success")
                return redirect(url_for("manage_contracts"))

        entities = LegalEntity.query.order_by(LegalEntity.name.asc()).all()
        places = PlaceOfWork.query.join(LegalEntity).order_by(LegalEntity.name.asc(), PlaceOfWork.address.asc()).all()
        return render_template(
            "contract_form.html",
            target_user=target_user,
            contract=contract,
            contract_types=ContractType,
            teacher_classifications=TeacherClassification,
            legal_entities=entities,
            places_of_work=places,
            place_label_fn=_contract_place_label,
            mode="create",
        )

    @app.route("/contracts/<int:contract_id>/edit", methods=["GET", "POST"])
    @privilege_manager_required
    def edit_contract(contract_id: int):
        contract = db.session.get(Contract, contract_id)
        if contract is None:
            flash(_("Contract not found."), "error")
            return redirect(url_for("manage_contracts"))

        if request.method == "POST":
            errors = _save_contract_from_form(contract)
            if errors:
                for err in errors:
                    flash(err, "error")
            else:
                db.session.add(contract)
                db.session.commit()
                flash(_("Contract updated for %(username)s.", username=contract.user.username), "success")
                return redirect(url_for("manage_contracts"))

        entities = LegalEntity.query.order_by(LegalEntity.name.asc()).all()
        places = PlaceOfWork.query.join(LegalEntity).order_by(LegalEntity.name.asc(), PlaceOfWork.address.asc()).all()
        return render_template(
            "contract_form.html",
            target_user=contract.user,
            contract=contract,
            contract_types=ContractType,
            teacher_classifications=TeacherClassification,
            legal_entities=entities,
            places_of_work=places,
            place_label_fn=_contract_place_label,
            mode="edit",
        )


    @app.route("/leaves/approval-settings", methods=["GET", "POST"])
    @login_required
    def leave_approval_settings():
        if not can_access_page("leave_approval_settings"):
            abort(403)

        if request.method == "POST":
            csrf_token = session.get("leave_approval_csrf_token")
            if not csrf_token or not compare_digest(csrf_token.encode(), request.form.get("csrf_token", "").encode()):
                abort(400)
            try:
                policy = LeaveApprovalPolicy(request.form.get("policy"))
            except ValueError:
                abort(400)
            settings = get_leave_approval_settings(for_update=True)
            if request.form.get("previous_policy") != settings.policy.value:
                db.session.rollback()
                flash(_('The approval rule was changed by another Director. Review the current setting and try again.'), "error")
                return redirect(url_for("leave_approval_settings"))
            if policy == settings.policy:
                db.session.rollback()
                flash(_("The approval rule is unchanged."), "success")
                return redirect(url_for("leave_approval_settings"))

            previous_policy = settings.policy
            settings.policy = policy
            settings.updated_by_id = current_user.id
            settings.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            approved_count = 0
            pending_requests = (
                LeaveRequest.query.filter(LeaveRequest.status.in_((
                    LeaveRequestStatus.pending_approval, LeaveRequestStatus.pending_cancellation,
                )))
                .with_for_update().populate_existing().all()
            )
            for leave_request in pending_requests:
                before = snapshot_leave_request(leave_request)
                previous_recipients = actionable_recipient_ids(leave_request, policy=previous_policy)
                if leave_request.status == LeaveRequestStatus.pending_approval and approvals_satisfy_policy(leave_request, policy):
                    leave_request.status = LeaveRequestStatus.approved
                    leave_request.decided_by_id = current_user.id
                    approved_count += 1
                record_leave_change(
                    leave_request, before, current_user, action="policy_change",
                    previous_recipients=previous_recipients,
                )
            db.session.commit()
            _wake_leave_notifications()
            flash(_("Approval rule saved. Pending requests approved using recorded approvals: %(count)s.", count=approved_count), "success")
            return redirect(url_for("leave_approval_settings"))

        settings = get_leave_approval_settings()
        session.setdefault("leave_approval_csrf_token", token_urlsafe(32))
        return render_template(
            "leave_approval_settings.html",
            settings=settings,
            policies=LeaveApprovalPolicy,
            user_display_name=_user_display_name,
            csrf_token=session["leave_approval_csrf_token"],
        )

    @app.route("/leaves/manage", methods=["GET", "POST"])
    @login_required
    def manage_leaves():
        if not _can_manage_leaves(current_user):
            abort(403)

        selected_legal_entity_id = request.values.get("legal_entity_id", type=int)
        selected_user_id = request.values.get("user_id", type=int)
        selected_contract_id = request.values.get("contract_id", type=int)
        selected_status = _normalize_optional_text(request.values.get("status"))

        if request.method == "POST":
            get_leave_approval_settings(for_update=True)
            leave_request_id = request.form.get("leave_request_id", type=int)
            action = request.form.get("action")
            return_to = request.form.get("return_to")
            leave_request = (
                _leave_request_query_for_manager(current_user)
                .filter(LeaveRequest.id == leave_request_id)
                .with_for_update().populate_existing().first()
            )
            if leave_request is None:
                flash(_("Leave request not found or not available to you."), "error")
                return redirect(landing_url() if return_to == "dashboard" else url_for("manage_leaves"))

            before = snapshot_leave_request(leave_request)
            if action == "approve":
                approved_parts = _approve_leave_request(leave_request, current_user)
                if not approved_parts:
                    flash(_("You cannot add another approval to this leave request."), "error")
                elif leave_request.status == LeaveRequestStatus.approved:
                    flash(_("Leave request fully approved."), "success")
                else:
                    flash(_("Recorded your %(reviewers)s approval; another approval is still required.", reviewers=_(" and ").join(_('Director') if part == "CEO" else _('Nursery head / deputy nursery head') for part in approved_parts)), "success")
            elif action == "reject":
                if leave_request.status == LeaveRequestStatus.pending_approval:
                    if not (_can_approve_ceo_part(current_user, leave_request) or _can_approve_leadership_part(current_user, leave_request)):
                        abort(403)
                    leave_request.status = LeaveRequestStatus.rejected
                    leave_request.decided_by_id = current_user.id
                    flash(_("Leave request rejected."), "success")
                elif leave_request.status == LeaveRequestStatus.pending_cancellation:
                    if not _can_review_leave_cancellation(current_user, leave_request):
                        abort(403)
                    leave_request.status = LeaveRequestStatus.approved
                    flash(_("Leave cancellation rejected; request remains approved."), "success")
                else:
                    flash(_("Only pending approval or pending cancellation leave requests can be rejected."), "error")
            elif action == "cancel":
                if leave_request.status in {LeaveRequestStatus.approved, LeaveRequestStatus.pending_cancellation}:
                    if not _can_review_leave_cancellation(current_user, leave_request):
                        abort(403)
                    leave_request.status = LeaveRequestStatus.cancelled
                    leave_request.decided_by_id = current_user.id
                    flash(_("Leave request cancelled."), "success")
                else:
                    flash(_("Only approved or pending cancellation leave requests can be cancelled."), "error")
            else:
                flash(_("Invalid leave action."), "error")
                return redirect(landing_url() if return_to == "dashboard" else url_for("manage_leaves"))

            record_leave_change(leave_request, before, current_user, action=action)
            db.session.commit()
            _wake_leave_notifications()
            if return_to == "dashboard":
                return redirect(landing_url())
            return redirect(
                url_for(
                    "manage_leaves",
                    legal_entity_id=selected_legal_entity_id,
                    user_id=selected_user_id,
                    contract_id=selected_contract_id,
                    status=selected_status,
                )
            )

        base_query = _leave_request_query_for_manager(current_user)
        accessible_legal_entity_ids = _accessible_leave_legal_entity_ids(current_user)
        legal_entities_query = LegalEntity.query
        if accessible_legal_entity_ids is not None:
            legal_entities_query = legal_entities_query.filter(LegalEntity.id.in_(accessible_legal_entity_ids))
        legal_entities = legal_entities_query.order_by(LegalEntity.name.asc()).all()

        users = (
            User.query.join(Contract)
            .join(LeaveRequest, LeaveRequest.contract_id == Contract.id)
            .filter(LeaveRequest.id.in_(base_query.with_entities(LeaveRequest.id)))
            .order_by(User.username.asc())
            .distinct()
            .all()
        )
        contracts = (
            Contract.query.join(LeaveRequest)
            .filter(LeaveRequest.id.in_(base_query.with_entities(LeaveRequest.id)))
            .order_by(Contract.start_date.desc(), Contract.id.desc())
            .all()
        )

        filtered_query = base_query
        if selected_legal_entity_id:
            filtered_query = filtered_query.filter(Contract.legal_entity_id == selected_legal_entity_id)
        if selected_user_id:
            filtered_query = filtered_query.filter(LeaveRequest.user_id == selected_user_id)
        if selected_contract_id:
            filtered_query = filtered_query.filter(LeaveRequest.contract_id == selected_contract_id)
        if selected_status:
            try:
                filtered_query = filtered_query.filter(LeaveRequest.status == LeaveRequestStatus(selected_status))
            except ValueError:
                flash(_("Invalid status filter ignored."), "error")

        leave_requests = (
            filtered_query
            .order_by(LeaveRequest.start_date.desc(), LeaveRequest.id.desc())
            .all()
        )
        workplace_leave_counts = {
            leave_request.id: _max_workplace_leave_count_during_request(leave_request)
            for leave_request in leave_requests
        }

        return render_template(
            "manage_leaves.html",
            legal_entities=legal_entities,
            users=users,
            contracts=contracts,
            leave_requests=leave_requests,
            statuses=LeaveRequestStatus,
            selected_legal_entity_id=selected_legal_entity_id,
            selected_user_id=selected_user_id,
            selected_contract_id=selected_contract_id,
            selected_status=selected_status,
            can_approve_ceo_part=_can_approve_ceo_part,
            can_approve_leadership_part=_can_approve_leadership_part,
            can_review_leave_cancellation=_can_review_leave_cancellation,
            leave_request_end_date=_leave_request_end_date,
            workplace_leave_counts=workplace_leave_counts,
            user_display_name=_user_display_name,
        )


    @app.route("/working-days", methods=["GET", "POST"])
    @privilege_manager_required
    def manage_working_days():
        today = date.today()
        selected_year = request.values.get("year", type=int) or today.year
        selected_month = request.values.get("month", type=int) or today.month
        if selected_year < 1970 or selected_year > 2100:
            selected_year = today.year
        if selected_month < 1 or selected_month > 12:
            selected_month = today.month

        if request.method == "POST":
            selected_day = parse_iso_date(request.form.get("day"))
            action = request.form.get("action")
            note = _normalize_optional_text(request.form.get("note"))
            if selected_day is None:
                flash(_("Please select a valid day."), "error")
            elif action == "reset":
                WorkingDayOverride.query.filter_by(day=selected_day).delete()
                db.session.commit()
                flash(_("The day now follows the Hungarian calendar default again."), "success")
            elif action in {"working", "holiday"}:
                override = WorkingDayOverride.query.filter_by(day=selected_day).first()
                if override is None:
                    override = WorkingDayOverride(day=selected_day)
                    db.session.add(override)
                override.is_working_day = action == "working"
                override.note = note
                db.session.commit()
                flash(_("Working day calendar updated."), "success")
            else:
                flash(_("Invalid working day action."), "error")
            return redirect(url_for("manage_working_days", year=selected_year, month=selected_month))

        month_start = date(selected_year, selected_month, 1)
        if selected_month == 12:
            next_month = date(selected_year + 1, 1, 1)
            previous_month = date(selected_year, 11, 1)
        elif selected_month == 1:
            next_month = date(selected_year, 2, 1)
            previous_month = date(selected_year - 1, 12, 1)
        else:
            next_month = date(selected_year, selected_month + 1, 1)
            previous_month = date(selected_year, selected_month - 1, 1)

        calendar_days = _month_calendar_days(selected_year, selected_month)
        real_days = [day for day in calendar_days if day]
        overrides = WorkingDayOverride.query.filter(
            WorkingDayOverride.day >= month_start,
            WorkingDayOverride.day < next_month,
        ).all()
        overrides_by_day = {override.day: override for override in overrides}
        day_statuses = {day: _working_day_status(day, overrides_by_day) for day in real_days}

        return render_template(
            "manage_working_days.html",
            calendar_days=calendar_days,
            day_statuses=day_statuses,
            month_label=month_start.strftime("%B %Y"),
            selected_year=selected_year,
            selected_month=selected_month,
            previous_month=previous_month,
            next_month=next_month,
        )

    @app.route("/leave-limits", methods=["GET", "POST"])
    @privilege_manager_required
    def manage_leave_limits():
        request_data = request.form if request.method == "POST" else request.args
        selected_user_id = request_data.get("user_id", type=int)
        selected_contract_id = request_data.get("contract_id", type=int)
        selected_year = request_data.get("calendar_year", type=int) or date.today().year

        users = User.query.order_by(User.username.asc()).all()
        all_contracts = (
            Contract.query.join(User)
            .order_by(User.username.asc(), Contract.start_date.desc(), Contract.id.desc())
            .all()
        )

        selected_user = db.session.get(User, selected_user_id) if selected_user_id else None
        selected_contract = None
        if selected_contract_id:
            selected_contract = next((contract for contract in all_contracts if contract.id == selected_contract_id), None)
            if selected_contract is None:
                flash(_("Selected contract does not exist."), "error")
            elif selected_user and selected_contract.user_id != selected_user.id:
                flash(_("Selected contract does not belong to the selected employee."), "error")
                selected_contract = None
            elif selected_user is None:
                selected_user = selected_contract.user
                selected_user_id = selected_user.id

        if request.method == "POST":
            action = request.form.get("action", "save")
            if selected_year < 1970 or selected_year > 2100:
                flash(_("Please provide a valid calendar year."), "error")
                return redirect(url_for("manage_leave_limits", calendar_year=selected_year))
            if action == "open_year":
                _set_leave_year_open(selected_year, True, current_user)
                db.session.commit()
                flash(_("%(year)s is now open for employee leave requests.", year=selected_year), "success")
                return redirect(url_for("manage_leave_limits", calendar_year=selected_year))
            if action == "lock_year":
                _set_leave_year_open(selected_year, False)
                db.session.commit()
                flash(_("%(year)s is now locked for employee leave requests.", year=selected_year), "success")
                return redirect(url_for("manage_leave_limits", calendar_year=selected_year))
            if action == "load_year_defaults":
                _import_default_leave_limits_for_year(selected_year)
                _copy_range_limits_for_year(selected_year)
                db.session.commit()
                flash(_("Default leave limits were loaded for active contracts in %(year)s.", year=selected_year), "success")
                return redirect(url_for("manage_leave_limits", calendar_year=selected_year))
            if action == "undo_year_import":
                _remove_imported_leave_limits_for_year(selected_year)
                db.session.commit()
                flash(_("Imported leave limits for %(year)s were removed.", year=selected_year), "success")
                return redirect(url_for("manage_leave_limits", calendar_year=selected_year))
            if selected_user is None:
                flash(_("Please select an employee first."), "error")
                return redirect(url_for("manage_leave_limits"))
            if selected_contract is None:
                flash(_("Please select a valid contract."), "error")
                return redirect(url_for("manage_leave_limits", user_id=selected_user_id, calendar_year=selected_year))
            if not _is_contract_active_in_year(selected_contract, selected_year):
                flash(_("The selected contract is not active in the selected calendar year."), "error")
                return redirect(
                    url_for(
                        "manage_leave_limits",
                        user_id=selected_user_id,
                        contract_id=selected_contract_id,
                        calendar_year=selected_year,
                    )
                )

            if action == "load_contract_defaults":
                _import_default_leave_limits_for_contract(selected_contract, selected_year)
                db.session.commit()
                flash(_("Default leave limits were loaded for the selected contract."), "success")
                return redirect(
                    url_for(
                        "manage_leave_limits",
                        user_id=selected_user_id,
                        contract_id=selected_contract_id,
                        calendar_year=selected_year,
                    )
                )
            if action == "undo_contract_import":
                _remove_imported_leave_limits_for_contract(selected_contract, selected_year)
                db.session.commit()
                flash(_("Imported leave limits were removed for the selected contract."), "success")
                return redirect(
                    url_for(
                        "manage_leave_limits",
                        user_id=selected_user_id,
                        contract_id=selected_contract_id,
                        calendar_year=selected_year,
                    )
                )

            first_day = date(selected_year, 1, 1)
            last_day = date(selected_year, 12, 31)

            for leave_type in CALENDAR_YEAR_BOUNDED_LEAVE_TYPES:
                if not _leave_type_available_for_contract(
                    leave_type,
                    selected_contract.contract_type,
                    selected_contract.user,
                ):
                    continue
                limit_days = request.form.get(f"limit_days_{leave_type.name}", type=int)
                if limit_days is None:
                    continue
                if limit_days < 0:
                    flash(_("%(category)s cannot be negative.", category=enum_label(leave_type)), "error")
                    return redirect(
                        url_for(
                            "manage_leave_limits",
                            user_id=selected_user_id,
                            contract_id=selected_contract_id,
                            calendar_year=selected_year,
                        )
                    )
                record = ContractLeaveLimit.query.filter_by(
                    contract_id=selected_contract.id,
                    calendar_year=selected_year,
                    leave_type=leave_type,
                ).first()
                if record is None:
                    record = ContractLeaveLimit(
                        contract_id=selected_contract.id,
                        calendar_year=selected_year,
                        leave_type=leave_type,
                    )
                record.limit_days = limit_days
                record.period_start = first_day
                record.period_end = last_day
                record.imported = False
                _clear_previous_leave_limit_values(record)
                db.session.add(record)

            available_range_leave_types = {
                leave_type
                for leave_type in RANGE_BASED_LEAVE_TYPES
                if _leave_type_available_for_contract(
                    leave_type,
                    selected_contract.contract_type,
                    selected_contract.user,
                )
            }
            range_leave_type_names = request.form.getlist("range_leave_type")
            range_limit_day_values = request.form.getlist("range_limit_days")
            range_start_values = request.form.getlist("range_period_start")
            range_end_values = request.form.getlist("range_period_end")
            new_range_records = []

            for index, leave_type_name in enumerate(range_leave_type_names):
                try:
                    leave_type = LeaveType[leave_type_name]
                except KeyError:
                    flash(_("Invalid custom leave category submitted."), "error")
                    return redirect(
                        url_for(
                            "manage_leave_limits",
                            user_id=selected_user_id,
                            contract_id=selected_contract_id,
                            calendar_year=selected_year,
                        )
                    )
                if leave_type not in available_range_leave_types:
                    continue

                limit_days_value = (
                    range_limit_day_values[index].strip()
                    if index < len(range_limit_day_values)
                    else ""
                )
                start_raw_value = range_start_values[index] if index < len(range_start_values) else ""
                end_raw_value = range_end_values[index] if index < len(range_end_values) else ""
                start_value = parse_iso_date(start_raw_value)
                end_value = parse_iso_date(end_raw_value)
                if not limit_days_value and start_value is None and end_value is None:
                    continue
                if not limit_days_value or start_value is None or end_value is None:
                    flash(_("%(category)s must include limit, start date, and end date.", category=enum_label(leave_type)), "error")
                    return redirect(
                        url_for(
                            "manage_leave_limits",
                            user_id=selected_user_id,
                            contract_id=selected_contract_id,
                            calendar_year=selected_year,
                        )
                    )
                try:
                    limit_days = int(limit_days_value)
                except ValueError:
                    flash(_("%(category)s limit must be a whole number.", category=enum_label(leave_type)), "error")
                    return redirect(
                        url_for(
                            "manage_leave_limits",
                            user_id=selected_user_id,
                            contract_id=selected_contract_id,
                            calendar_year=selected_year,
                        )
                    )
                if limit_days < 0:
                    flash(_("%(category)s cannot be negative.", category=enum_label(leave_type)), "error")
                    return redirect(
                        url_for(
                            "manage_leave_limits",
                            user_id=selected_user_id,
                            contract_id=selected_contract_id,
                            calendar_year=selected_year,
                        )
                    )
                if end_value < start_value:
                    flash(_("%(category)s end date cannot be earlier than start date.", category=enum_label(leave_type)), "error")
                    return redirect(
                        url_for(
                            "manage_leave_limits",
                            user_id=selected_user_id,
                            contract_id=selected_contract_id,
                            calendar_year=selected_year,
                        )
                    )
                new_range_records.append(
                    ContractLeaveLimit(
                        contract_id=selected_contract.id,
                        calendar_year=selected_year,
                        leave_type=leave_type,
                        limit_days=limit_days,
                        period_start=start_value,
                        period_end=end_value,
                        imported=False,
                    )
                )

            if available_range_leave_types:
                ContractLeaveLimit.query.filter(
                    ContractLeaveLimit.contract_id == selected_contract.id,
                    ContractLeaveLimit.calendar_year == selected_year,
                    ContractLeaveLimit.leave_type.in_(list(available_range_leave_types)),
                ).delete(synchronize_session=False)
                for record in new_range_records:
                    db.session.add(record)

            db.session.commit()
            flash(_("Leave limits saved for the selected contract and year."), "success")
            return redirect(
                url_for(
                    "manage_leave_limits",
                    user_id=selected_user_id,
                    contract_id=selected_contract_id,
                    calendar_year=selected_year,
                )
            )

        existing_limits = {}
        range_existing_limits = {leave_type: [] for leave_type in RANGE_BASED_LEAVE_TYPES}
        if selected_contract is not None:
            existing_records = (
                ContractLeaveLimit.query.filter_by(
                    contract_id=selected_contract.id,
                    calendar_year=selected_year,
                )
                .order_by(
                    ContractLeaveLimit.leave_type.asc(),
                    ContractLeaveLimit.period_start.asc(),
                    ContractLeaveLimit.id.asc(),
                )
                .all()
            )
            for record in existing_records:
                if record.leave_type in RANGE_BASED_LEAVE_TYPES:
                    range_existing_limits.setdefault(record.leave_type, []).append(record)
                elif record.leave_type not in existing_limits:
                    existing_limits[record.leave_type] = record
        leave_year = db.session.get(LeaveYear, selected_year)
        opened_leave_years = LeaveYear.query.order_by(LeaveYear.year.desc()).all()

        return render_template(
            "manage_leave_limits.html",
            leave_bases={kind: leave_basis(selected_contract, selected_year, kind) for kind in LeaveType} if selected_contract else {},
            users=users,
            all_contracts=all_contracts,
            selected_user=selected_user,
            selected_contract=selected_contract,
            selected_year=selected_year,
            existing_limits=existing_limits,
            range_existing_limits=range_existing_limits,
            calendar_year_bounded_leave_types=CALENDAR_YEAR_BOUNDED_LEAVE_TYPES,
            range_based_leave_types=RANGE_BASED_LEAVE_TYPES,
            leave_types_without_limit=LEAVE_TYPES_WITHOUT_LIMIT,
            is_contract_active_in_year=_is_contract_active_in_year,
            leave_type_available_for_contract=_leave_type_available_for_contract,
            contract_display_name=_contract_display_name,
            leave_year=leave_year,
            opened_leave_years=opened_leave_years,
            is_leave_year_open=_is_leave_year_open(selected_year),
            has_imported_year_limits=_has_imported_leave_limits_for_year(selected_year),
            has_imported_contract_limits=(
                _has_imported_leave_limits_for_contract(selected_contract, selected_year)
                if selected_contract is not None
                else False
            ),
        )

    @app.route("/leadership", methods=["GET", "POST"])
    @privilege_manager_required
    def manage_leadership():
        editing_leadership_id = request.args.get("edit", type=int)
        if request.method == "POST":
            leadership_id = request.form.get("leadership_id", type=int)
            leadership = db.session.get(Leadership, leadership_id) if leadership_id else Leadership()
            if leadership is None:
                flash(_("Leadership record not found."), "error")
                return redirect(url_for("manage_leadership"))

            previous_tasks = snapshot_pending_leave_tasks()
            errors = _save_leadership_from_form(leadership)
            if errors:
                db.session.rollback()
                for err in errors:
                    flash(err, "error")
                return redirect(url_for("manage_leadership", edit=leadership_id) if leadership_id else url_for("manage_leadership"))

            db.session.add(leadership)
            db.session.flush()
            invalidate_access_cache()
            record_reviewer_changes(previous_tasks, current_user)
            db.session.commit()
            _wake_leave_notifications()
            flash(_("Leadership record updated.") if leadership_id else _("Leadership record saved."), "success")
            return redirect(url_for("manage_leadership"))

        legal_entities = LegalEntity.query.order_by(LegalEntity.name.asc()).all()
        contracts = (
            Contract.query.join(User)
            .join(LegalEntity)
            .order_by(LegalEntity.name.asc(), User.username.asc(), Contract.start_date.desc())
            .all()
        )
        leadership_positions = (
            Leadership.query.join(LegalEntity)
            .join(Contract)
            .order_by(LegalEntity.name.asc(), Leadership.start_date.desc(), Leadership.id.desc())
            .all()
        )
        editing_leadership = None
        if editing_leadership_id:
            editing_leadership = db.session.get(Leadership, editing_leadership_id)
            if editing_leadership is None:
                flash(_("Leadership record not found."), "error")
                return redirect(url_for("manage_leadership"))

        return render_template(
            "manage_leadership.html",
            legal_entities=legal_entities,
            contracts=contracts,
            leadership_positions=leadership_positions,
            editing_leadership=editing_leadership,
        )

    @app.route("/professional-exam", methods=["GET", "POST"])
    @login_required
    def professional_exam():
        return legacy_qualification_redirect()
