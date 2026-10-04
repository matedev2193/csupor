"""Transactional leave-event outbox and current reviewer eligibility.

Routes take a snapshot before mutating a leave and record the result before the
same transaction commits. There is deliberately no mail/network I/O here. Any
future date, category or note editor must use the same snapshot/record pair;
only dates and the fact of a modification are placed in mail, never note text
or a medical leave category.
"""

from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import selectinload

from . import db
from .models import (
    Contract, Leadership, LeadershipPosition, LeaveApprovalPolicy,
    LeaveApprovalSettings, LeaveRequestStatus, User, UserPrivilege,
    LeaveRequest,
)
from .people import display_name
from .notification_models import LeaveNotification


BUDAPEST = ZoneInfo("Europe/Budapest")
EVENT_LABELS = {
    "submitted": "Új távolléti igény jóváhagyásra vár.",
    "approved": "A távolléti igényt elfogadták.",
    "partially_approved": "A távolléti igény egyik jóváhagyása megtörtént; további jóváhagyás szükséges.",
    "rejected": "A távolléti igényt elutasították.",
    "modified": "A távolléti igényt módosították.",
    "cancelled": "A távolléti igényt visszavonták.",
    "cancellation_requested": "A távolléti igény visszavonását kérték.",
    "cancellation_undone": "A visszavonási kérelmet visszavonták; a távollét továbbra is jóváhagyott.",
    "cancellation_rejected": "A visszavonási kérelmet elutasították; a távollét továbbra is jóváhagyott.",
    "approval_policy_changed": "A jóváhagyási beállítások változása miatt a távolléti igénnyel teendőd van.",
    "reviewer_changed": "A távolléti igénnyel kapcsolatban teendőd van.",
}


def _utc_now(now=None):
    now = now or datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _policy():
    # Read the live settings object; a policy-change route may have changed it
    # in this transaction while Flask's normal display cache is still old.
    settings = db.session.get(LeaveApprovalSettings, 1)
    return settings.policy if settings else LeaveApprovalPolicy.both


def _reviewer_parts(user, leave_request, *, policy, day):
    ceo = policy != LeaveApprovalPolicy.leadership_only and user.privilege == UserPrivilege.ceo
    leadership = False
    if policy != LeaveApprovalPolicy.ceo_only:
        for contract in user.contracts:
            for position in contract.leadership_positions:
                if position.legal_entity_id != leave_request.contract.legal_entity_id:
                    continue
                if position.start_date > day or (position.end_date and position.end_date < day):
                    continue
                if user.id == leave_request.user_id and position.position == LeadershipPosition.deputy_principal:
                    continue
                leadership = True
    return ceo, leadership


def user_has_leave_task(user, leave_request, *, now=None, policy=None):
    """Apply the configured approval roles and the request's current state."""
    if user is None or leave_request is None:
        return False
    if leave_request.status not in (LeaveRequestStatus.pending_approval, LeaveRequestStatus.pending_cancellation):
        return False
    ceo, leadership = _reviewer_parts(
        user, leave_request, policy=policy or _policy(), day=_utc_now(now).astimezone(BUDAPEST).date(),
    )
    if leave_request.status == LeaveRequestStatus.pending_cancellation:
        # Recorded original approvals do not decide the later cancellation.
        return ceo or leadership
    return ((ceo and leave_request.ceo_approved_by_id is None)
            or (leadership and leave_request.leadership_approved_by_id is None))


def actionable_recipient_ids(leave_request, *, now=None, policy=None):
    if leave_request.status not in (LeaveRequestStatus.pending_approval, LeaveRequestStatus.pending_cancellation):
        return set()
    policy = policy or _policy()
    now = _utc_now(now)
    day = now.astimezone(BUDAPEST).date()
    active_position = db.and_(
        Leadership.legal_entity_id == leave_request.contract.legal_entity_id,
        Leadership.start_date <= day,
        db.or_(Leadership.end_date.is_(None), Leadership.end_date >= day),
    )
    candidates = User.query.filter(db.or_(
        User.privilege == UserPrivilege.ceo,
        User.contracts.any(Contract.leadership_positions.any(active_position)),
    )).options(selectinload(User.contracts).selectinload(Contract.leadership_positions)).populate_existing().all()
    return {user.id for user in candidates if user_has_leave_task(user, leave_request, now=now, policy=policy)}


def notification_is_actionable(notification, user, *, now=None):
    """Recheck task access immediately before mail leaves the outbox.

    A combined owner/task row may retain its owner update when this returns
    false; the delivery worker separately verifies current request ownership.
    """
    if user is None or not notification.is_task or notification.recipient_id != user.id:
        return False
    leave_request = notification.leave_request
    if leave_request is None or notification.payload.get("status") != leave_request.status.value:
        return False
    return user_has_leave_task(user, leave_request, now=now)


def snapshot_leave_request(leave_request):
    return {
        "status": leave_request.status.value,
        "start_date": leave_request.start_date,
        "end_date": leave_request.end_date,
        "category": leave_request.category.value,
        "note": leave_request.note,
        "ceo_approved_by_id": leave_request.ceo_approved_by_id,
        "leadership_approved_by_id": leave_request.leadership_approved_by_id,
        "decided_by_id": leave_request.decided_by_id,
    }


def _event_type(leave_request, before, action):
    status = leave_request.status
    if before and before["status"] == status.value and any(
        before[key] != (getattr(leave_request, key).value if key == "category" else getattr(leave_request, key))
        for key in ("start_date", "end_date", "category", "note")
    ):
        return "modified"
    if status == LeaveRequestStatus.cancelled:
        return "cancelled"
    if status == LeaveRequestStatus.rejected:
        return "rejected"
    if status == LeaveRequestStatus.pending_cancellation:
        return "cancellation_requested"
    if status == LeaveRequestStatus.approved:
        if before and before["status"] == LeaveRequestStatus.pending_cancellation.value:
            return "cancellation_undone" if action == "undo_cancel" else "cancellation_rejected"
        if before and before["status"] == LeaveRequestStatus.approved.value:
            return "modified"
        return "approved"
    if before is None:
        return "submitted"
    if any(before[key] != getattr(leave_request, key) for key in ("ceo_approved_by_id", "leadership_approved_by_id")):
        return "partially_approved"
    return "modified"


def record_leave_change(leave_request, before, actor, *, action=None, previous_recipients=None, now=None):
    """Append one outbox row per recipient, sharing the caller's transaction.

    ``before=None`` is a new submission. ``previous_recipients`` accompanies
    approval-policy changes and prevents re-notifying unchanged reviewers.
    Rejected forms and repeated decisions are no-ops, including their mail.
    """
    now = _utc_now(now)
    after = snapshot_leave_request(leave_request)
    changed = before is None or before != after
    tasks = actionable_recipient_ids(leave_request, now=now)
    if not changed:
        if previous_recipients is None:
            return []
        tasks -= previous_recipients
        if not tasks:
            return []
        event_type = "reviewer_changed" if action == "reviewer_change" else "approval_policy_changed"
    else:
        event_type = _event_type(leave_request, before, action)
    # Submission confirmations aren't requested, but automatic approvals are.
    owner_id = leave_request.user_id if changed and event_type != "submitted" else None
    recipient_ids = tasks | ({owner_id} if owner_id is not None else set())
    if not recipient_ids:
        return []

    from .notification_delivery import notification_due_at, notification_is_urgent

    now = _utc_now(now)
    affected_start = leave_request.start_date
    if before and before["start_date"]:
        affected_start = min(affected_start, before["start_date"])
    event_key = uuid4().hex
    payload = {
        "request_id": leave_request.id,
        "applicant_name": display_name(leave_request.user),
        "actor_name": display_name(actor) if actor is not None else "CSUPOR",
        "start_date": leave_request.start_date.isoformat(),
        "end_date": leave_request.end_date.isoformat() if leave_request.end_date else None,
        "status": leave_request.status.value,
        "previous_status": before["status"] if before else None,
        "event_type": event_type,
        "label": EVENT_LABELS[event_type],
    }
    if before and (before["start_date"], before["end_date"]) != (leave_request.start_date, leave_request.end_date):
        payload["previous_start_date"] = before["start_date"].isoformat()
        payload["previous_end_date"] = before["end_date"].isoformat() if before["end_date"] else None
    rows = []
    for recipient_id in sorted(recipient_ids):
        item_payload = dict(payload, link_path="/leaves/manage" if recipient_id in tasks else "/leaves")
        row = LeaveNotification(
            event_key=event_key,
            recipient_id=recipient_id,
            leave_request_id=leave_request.id,
            event_type=event_type,
            payload=item_payload,
            is_task=recipient_id in tasks,
            is_owner=recipient_id == owner_id,
            created_at=now.replace(tzinfo=None),
            due_at=notification_due_at(affected_start, now),
            urgent=notification_is_urgent(affected_start, now),
            status="pending",
        )
        db.session.add(row)
        rows.append(row)
    return rows


def snapshot_pending_leave_tasks():
    """Serialise reviewer edits with leave actions and snapshot current tasks."""
    from .leave_approval import get_leave_approval_settings

    get_leave_approval_settings(for_update=True)
    requests = LeaveRequest.query.filter(LeaveRequest.status.in_((
        LeaveRequestStatus.pending_approval, LeaveRequestStatus.pending_cancellation,
    ))).with_for_update().populate_existing().all()
    return [(item, snapshot_leave_request(item), actionable_recipient_ids(item)) for item in requests]


def record_reviewer_changes(previous, actor):
    """Notify newly eligible reviewers after an actual privilege/leader edit."""
    for leave_request, before, recipient_ids in previous:
        record_leave_change(leave_request, before, actor, action="reviewer_change", previous_recipients=recipient_ids)


def reconcile_pending_leave_tasks(*, now=None):
    """Queue first notices for reviewers whose dated appointment has begun.

    No reminders are generated for an already notified pending task. Explicit
    privilege/policy/appointment changes use the route hooks above, which can
    report a new eligibility episode. This catches future start dates and
    pre-existing pending tasks without replaying completed leave history.
    The caller commits; the shared settings lock serialises multiple workers.
    """
    from .leave_approval import get_leave_approval_settings

    now = _utc_now(now)
    get_leave_approval_settings(for_update=True)
    requests = LeaveRequest.query.filter(LeaveRequest.status.in_((
        LeaveRequestStatus.pending_approval, LeaveRequestStatus.pending_cancellation,
    ))).with_for_update().populate_existing().all()
    if not requests:
        return 0
    historical = LeaveNotification.query.filter(
        LeaveNotification.leave_request_id.in_([item.id for item in requests]),
        LeaveNotification.is_task.is_(True),
    ).with_for_update().populate_existing().all()
    known = {}
    for item in historical:
        known.setdefault((item.leave_request_id, item.payload.get("status")), set()).add(item.recipient_id)
    count = 0
    for leave_request in requests:
        previously_notified = known.get((leave_request.id, leave_request.status.value), set())
        rows = record_leave_change(
            leave_request, snapshot_leave_request(leave_request), None,
            action="reviewer_change", previous_recipients=previously_notified, now=now,
        )
        event_key = sha256(f"task-reconcile:{leave_request.id}:{leave_request.status.value}".encode()).hexdigest()[:32]
        for row in rows:
            row.event_key = event_key
        count += len(rows)
    db.session.flush()
    return count
