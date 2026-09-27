"""The application-wide leave approval rule, defaulting to dual approval."""

from flask import g
from flask_babel import lazy_gettext
from sqlalchemy.exc import IntegrityError

from . import db
from .models import LeaveApprovalPolicy, LeaveApprovalSettings


POLICY_LABELS = {
    LeaveApprovalPolicy.ceo_only: lazy_gettext("CEO only"),
    LeaveApprovalPolicy.leadership_only: lazy_gettext("Principal/deputy only"),
    LeaveApprovalPolicy.both: lazy_gettext("Both"),
    LeaveApprovalPolicy.either: lazy_gettext("Either"),
}
POLICY_DESCRIPTIONS = {
    LeaveApprovalPolicy.ceo_only: lazy_gettext("A CEO approval is sufficient. Principal/deputy approval is not required."),
    LeaveApprovalPolicy.leadership_only: lazy_gettext("Approval from the relevant principal or deputy is sufficient. CEO approval is not required."),
    LeaveApprovalPolicy.both: lazy_gettext("Approval from both the CEO and the relevant principal/deputy is required."),
    LeaveApprovalPolicy.either: lazy_gettext("One approval from either the CEO or the relevant principal/deputy is sufficient."),
}


def initialise_leave_approval_settings():
    if db.session.get(LeaveApprovalSettings, 1) is None:
        db.session.add(LeaveApprovalSettings(id=1, policy=LeaveApprovalPolicy.both))
        try:
            db.session.commit()
        except IntegrityError:
            # Another worker may have initialised the singleton during startup.
            db.session.rollback()
            if db.session.get(LeaveApprovalSettings, 1) is None:
                raise


def get_leave_approval_settings(*, for_update=False):
    statement = db.select(LeaveApprovalSettings).where(LeaveApprovalSettings.id == 1)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    settings = db.session.execute(statement).scalar_one()
    g.leave_approval_policy = settings.policy
    return settings


def current_leave_approval_policy():
    if "leave_approval_policy" not in g:
        get_leave_approval_settings()
    return g.leave_approval_policy


def approvals_satisfy_policy(leave_request, policy):
    ceo = leave_request.ceo_approved_by_id is not None
    leadership = leave_request.leadership_approved_by_id is not None
    if policy == LeaveApprovalPolicy.ceo_only:
        return ceo
    if policy == LeaveApprovalPolicy.leadership_only:
        return leadership
    if policy == LeaveApprovalPolicy.either:
        return ceo or leadership
    return ceo and leadership
