"""Delete an account's owned records while preserving shared business records."""

from . import db
from .models import Contract, GyapForm, LeaveApprovalSettings, LeaveRequest, LeaveYear
from .mail_settings_models import MailServerSettings
from .worktime_models import WorkGroupMerge, WorkSchedule, WorkTimeEntry


class AccountDeletionConflict(Exception):
    """Existing inconsistent ownership prevents deletion without data loss."""


def delete_user_account(user):
    """Stage all changes in the caller's transaction; the caller commits once."""
    owned_contracts = db.select(Contract.id).where(Contract.user_id == user.id)
    if LeaveRequest.query.filter(
        LeaveRequest.contract_id.in_(owned_contracts), LeaveRequest.user_id != user.id,
    ).first() is not None:
        raise AccountDeletionConflict()
    if WorkTimeEntry.query.filter(
        WorkTimeEntry.contract_id.in_(owned_contracts), WorkTimeEntry.user_id != user.id,
    ).first() is not None:
        raise AccountDeletionConflict()

    # These are attribution references, not ownership: keep the other user's
    # request, its status, and shared annual forms/settings intact.
    for column in (
        LeaveRequest.ceo_approved_by_id,
        LeaveRequest.leadership_approved_by_id,
        LeaveRequest.decided_by_id,
    ):
        LeaveRequest.query.filter(column == user.id).update({column: None}, synchronize_session="fetch")
    for model, column in (
        (LeaveYear, LeaveYear.imported_by_id),
        (LeaveApprovalSettings, LeaveApprovalSettings.updated_by_id),
        (MailServerSettings, MailServerSettings.updated_by_id),
        (GyapForm, GyapForm.uploaded_by_id),
        (WorkGroupMerge, WorkGroupMerge.created_by_id),
        (WorkSchedule, WorkSchedule.generated_by_id),
        (WorkSchedule, WorkSchedule.confirmed_by_id),
    ):
        model.query.filter(column == user.id).update({column: None}, synchronize_session="fetch")

    # Explicit model cascades include profile/photo, dependents, qualifications,
    # professional exam, own leave requests, contracts, limits and leadership.
    db.session.delete(user)
