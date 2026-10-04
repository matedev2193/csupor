"""Real leave transitions record mail transactionally for eligible recipients."""

import json
import unittest
from datetime import date, datetime, timedelta, timezone

import test_leave_approval as fixtures
from app import db
from app.models import LeaveApprovalPolicy, LeaveRequest, LeaveRequestStatus
from app.notification_events import (
    actionable_recipient_ids, notification_is_actionable,
    reconcile_pending_leave_tasks, record_leave_change, snapshot_leave_request,
)
from app.notification_models import LeaveNotification
from app.routes import _manager_review_leave_requests


class LeaveNotificationTests(unittest.TestCase):
    setUp = fixtures.LeaveApprovalTests.setUp
    tearDown = fixtures.LeaveApprovalTests.tearDown
    login = fixtures.LeaveApprovalTests.login
    set_policy = fixtures.LeaveApprovalTests.set_policy
    leave = fixtures.LeaveApprovalTests.leave
    act = fixtures.LeaveApprovalTests.act
    save_policy = fixtures.LeaveApprovalTests.save_policy

    def rows(self, leave_request):
        return LeaveNotification.query.filter_by(leave_request_id=leave_request.id).order_by(LeaveNotification.id).all()

    def submit(self, key="employee", **extra):
        self.login(key)
        data = {"contract_id": self.contracts[key].id, "category": "health leave", "start_date": date.today().isoformat()}
        data.update(extra)
        return self.client.post("/leaves", data=data)

    def owner_action(self, leave_request, action):
        key = next(key for key, user in self.users.items() if user.id == leave_request.user_id)
        self.login(key)
        response = self.client.post("/leaves", data={
            "contract_id": leave_request.contract_id, "leave_request_id": leave_request.id, "action": action,
        })
        db.session.refresh(leave_request)
        return response

    def test_submission_recipients_follow_every_policy_and_deduplicate_dual_role(self):
        for index, policy in enumerate(LeaveApprovalPolicy):
            with self.subTest(policy=policy):
                self.set_policy(policy)
                self.assertEqual(self.submit(start_date=(date.today() + timedelta(days=index)).isoformat()).status_code, 302)
                leave_request = LeaveRequest.query.order_by(LeaveRequest.id.desc()).first()
                expected = {"ceo", "dual"} if policy == LeaveApprovalPolicy.ceo_only else (
                    {"principal", "deputy", "dual"} if policy == LeaveApprovalPolicy.leadership_only
                    else {"ceo", "principal", "deputy", "dual"}
                )
                rows = self.rows(leave_request)
                self.assertEqual({row.recipient.username for row in rows}, expected)
                self.assertEqual(len(rows), len(expected))
                self.assertTrue(all(row.is_task and not row.is_owner for row in rows))
                self.assertEqual(len({row.event_key for row in rows}), 1)

    def test_deputy_self_approval_expired_and_other_entity_are_excluded(self):
        leave_request = self.leave("deputy")
        recipients = actionable_recipient_ids(leave_request)
        self.assertEqual(recipients, {self.users[key].id for key in ("ceo", "principal", "dual")})
        self.set_policy(LeaveApprovalPolicy.leadership_only)
        self.assertEqual(actionable_recipient_ids(leave_request), {self.users[key].id for key in ("principal", "dual")})

    def test_automatic_approval_notifies_owner_without_creating_task(self):
        self.assertEqual(self.submit("dual").status_code, 302)
        leave_request = LeaveRequest.query.order_by(LeaveRequest.id.desc()).first()
        rows = self.rows(leave_request)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].recipient_id, self.users["dual"].id)
        self.assertEqual(rows[0].event_type, "approved")
        self.assertTrue(rows[0].is_owner)
        self.assertFalse(rows[0].is_task)

    def test_partial_approval_notifies_owner_and_only_remaining_approval_roles(self):
        leave_request = self.leave()
        self.act(leave_request, "ceo")
        rows = self.rows(leave_request)
        self.assertEqual({row.recipient.username for row in rows if row.is_task}, {"principal", "deputy", "dual"})
        self.assertEqual([row.recipient.username for row in rows if row.is_owner], ["employee"])
        self.assertTrue(all(row.event_type == "partially_approved" for row in rows))
        count = len(rows)
        self.act(leave_request, "ceo")
        self.assertEqual(len(self.rows(leave_request)), count)
        self.act(leave_request, "principal")
        approved = self.rows(leave_request)[-1]
        self.assertEqual(approved.event_type, "approved")
        self.assertTrue(approved.is_owner)
        self.assertFalse(approved.is_task)
        self.assertTrue(all(not notification_is_actionable(row, row.recipient) for row in rows))

    def test_rejection_and_direct_manager_cancellation_notify_owner(self):
        for action, status in (("reject", LeaveRequestStatus.pending_approval), ("cancel", LeaveRequestStatus.approved)):
            with self.subTest(action=action):
                leave_request = self.leave(status=status)
                self.act(leave_request, "ceo", action)
                rows = self.rows(leave_request)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].event_type, "rejected" if action == "reject" else "cancelled")
                self.assertTrue(rows[0].is_owner)
                self.assertEqual(rows[0].payload["previous_status"], status.value)

    def test_pending_owner_cancellation_queues_one_owner_change(self):
        leave_request = self.leave()
        self.owner_action(leave_request, "cancel")
        self.assertEqual([row.event_type for row in self.rows(leave_request)], ["cancelled"])
        self.owner_action(leave_request, "cancel")
        self.assertEqual(len(self.rows(leave_request)), 1)

    def test_cancellation_requested_undo_rejected_and_accepted_have_correct_events(self):
        for outcome, expected in (("undo_cancel", "cancellation_undone"), ("reject", "cancellation_rejected"), ("cancel", "cancelled")):
            with self.subTest(outcome=outcome):
                leave_request = self.leave(status=LeaveRequestStatus.approved,
                                           ceo_approved_by_id=self.users["ceo"].id,
                                           leadership_approved_by_id=self.users["principal"].id)
                self.owner_action(leave_request, "cancel")
                rows = self.rows(leave_request)
                self.assertEqual({row.recipient.username for row in rows if row.is_task}, {"ceo", "principal", "deputy", "dual"})
                self.assertEqual([row.event_type for row in rows if row.is_owner], ["cancellation_requested"])
                if outcome == "undo_cancel":
                    self.owner_action(leave_request, outcome)
                else:
                    self.act(leave_request, "ceo", outcome)
                self.assertEqual(self.rows(leave_request)[-1].event_type, expected)
                self.assertTrue(all(not notification_is_actionable(row, row.recipient) for row in rows))

    def test_cancellation_queue_and_permission_follow_policy_with_deputy_self_exclusion(self):
        for policy, excluded, allowed in ((LeaveApprovalPolicy.ceo_only, "principal", "ceo"),
                                          (LeaveApprovalPolicy.leadership_only, "ceo", "principal")):
            self.set_policy(policy)
            leave_request = self.leave(status=LeaveRequestStatus.pending_cancellation)
            self.assertNotIn(leave_request, _manager_review_leave_requests(self.users[excluded]))
            self.assertIn(leave_request, _manager_review_leave_requests(self.users[allowed]))
            for action in ("reject", "cancel"):
                self.assertEqual(self.act(leave_request, excluded, action).status_code, 403)
                self.assertEqual(self.rows(leave_request), [])
            self.assertEqual(self.act(leave_request, allowed, "cancel").status_code, 302)
        deputy_leave = self.leave("deputy", status=LeaveRequestStatus.pending_cancellation)
        self.assertNotIn(deputy_leave, _manager_review_leave_requests(self.users["deputy"]))
        self.assertEqual(self.act(deputy_leave, "deputy", "cancel").status_code, 403)

    def test_policy_change_notifies_newly_required_roles_and_retroactively_approved_owner(self):
        self.set_policy(LeaveApprovalPolicy.ceo_only)
        pending = self.leave()
        pending_cancel = self.leave(status=LeaveRequestStatus.pending_cancellation)
        self.save_policy(LeaveApprovalPolicy.both)
        for leave_request in (pending, pending_cancel):
            self.assertEqual({row.recipient.username for row in self.rows(leave_request)}, {"principal", "deputy"})
            self.assertTrue(all(row.event_type == "approval_policy_changed" for row in self.rows(leave_request)))
        partial = self.leave(ceo_approved_by_id=self.users["ceo"].id)
        self.save_policy(LeaveApprovalPolicy.ceo_only)
        rows = self.rows(partial)
        self.assertEqual([(row.recipient.username, row.event_type) for row in rows], [("employee", "approved")])
        count = LeaveNotification.query.count()
        self.save_policy(LeaveApprovalPolicy.ceo_only)
        self.assertEqual(LeaveNotification.query.count(), count)

    def test_invalid_submission_and_unauthorised_or_repeated_decisions_do_not_queue(self):
        self.submit(start_date="")
        self.assertEqual(LeaveNotification.query.count(), 0)
        leave_request = self.leave()
        self.assertEqual(self.act(leave_request, "outsider").status_code, 302)
        self.assertEqual(self.rows(leave_request), [])
        self.act(leave_request, "ceo", "invalid")
        self.assertEqual(self.rows(leave_request), [])

    def test_record_and_leave_change_are_rolled_back_together(self):
        leave_request = self.leave()
        before = snapshot_leave_request(leave_request)
        leave_request.status = LeaveRequestStatus.rejected
        record_leave_change(leave_request, before, self.users["ceo"])
        db.session.flush()
        self.assertEqual(LeaveNotification.query.count(), 1)
        db.session.rollback()
        self.assertEqual(leave_request.status, LeaveRequestStatus.pending_approval)
        self.assertEqual(LeaveNotification.query.count(), 0)

    def test_generic_date_or_note_changes_are_reported_without_sensitive_content(self):
        now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        leave_request = self.leave(status=LeaveRequestStatus.approved, start_date=date(2026, 10, 5))
        before = snapshot_leave_request(leave_request)
        leave_request.start_date = date(2026, 10, 20)
        leave_request.note = "Sensitive diagnosis must never appear in notification JSON"
        rows = record_leave_change(leave_request, before, self.users["ceo"], now=now)
        db.session.commit()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event_type, "modified")
        self.assertEqual(rows[0].payload["previous_start_date"], "2026-10-05")
        self.assertEqual(rows[0].due_at, now.replace(tzinfo=None))
        self.assertTrue(rows[0].urgent)
        encoded = json.dumps(rows[0].payload)
        self.assertNotIn("diagnosis", encoded)
        self.assertNotIn("health leave", encoded)
        unchanged = snapshot_leave_request(leave_request)
        self.assertEqual(record_leave_change(leave_request, unchanged, self.users["ceo"]), [])
        leave_request.note = "Changed sensitive note"
        self.assertEqual(record_leave_change(leave_request, unchanged, self.users["ceo"])[0].event_type, "modified")

    def test_send_time_permission_recheck_removes_changed_role_and_completed_tasks(self):
        leave_request = self.leave()
        rows = record_leave_change(leave_request, None, self.users["employee"])
        db.session.commit()
        ceo_row = next(row for row in rows if row.recipient_id == self.users["ceo"].id)
        self.assertTrue(notification_is_actionable(ceo_row, ceo_row.recipient))
        self.set_policy(LeaveApprovalPolicy.leadership_only)
        self.assertFalse(notification_is_actionable(ceo_row, ceo_row.recipient))
        principal_row = next(row for row in rows if row.recipient_id == self.users["principal"].id)
        self.assertTrue(notification_is_actionable(principal_row, principal_row.recipient))
        leadership = self.contracts["principal"].leadership_positions[0]
        leadership.end_date = date.today() - timedelta(days=1)
        db.session.commit()
        self.assertFalse(notification_is_actionable(principal_row, principal_row.recipient))

    def test_pending_cancellation_date_edit_is_a_modification_for_owner_and_reviewers(self):
        leave_request = self.leave(status=LeaveRequestStatus.pending_cancellation,
                                   start_date=date.today() + timedelta(days=30))
        before = snapshot_leave_request(leave_request)
        leave_request.start_date += timedelta(days=1)
        rows = record_leave_change(leave_request, before, self.users["ceo"])
        self.assertTrue(rows)
        self.assertTrue(all(row.event_type == "modified" for row in rows))
        self.assertEqual(sum(row.is_owner for row in rows), 1)
        self.assertEqual(sum(row.is_task for row in rows), 4)

    def test_current_budapest_day_controls_leadership_expiry(self):
        leave_request = self.leave()
        principal = self.contracts["principal"].leadership_positions[0]
        principal.start_date = date(2026, 10, 4)
        principal.end_date = date(2026, 10, 4)
        before_midnight = datetime(2026, 10, 4, 21, 59, tzinfo=timezone.utc)
        after_midnight = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)
        self.assertIn(self.users["principal"].id, actionable_recipient_ids(leave_request, now=before_midnight))
        self.assertNotIn(self.users["principal"].id, actionable_recipient_ids(leave_request, now=after_midnight))

    def test_user_deletion_cascades_notification_rows_without_blocking(self):
        leave_request = self.leave()
        record_leave_change(leave_request, None, self.users["employee"])
        db.session.commit()
        self.assertGreater(LeaveNotification.query.count(), 0)
        db.session.delete(self.users["employee"])
        db.session.commit()
        self.assertEqual(LeaveNotification.query.count(), 0)

    def test_privilege_edit_notifies_new_ceo_once_for_existing_tasks(self):
        leave_request = self.leave()
        self.login("ceo")
        data = {"user_id": self.users["hr"].id, "privilege": "ceo"}
        self.assertEqual(self.client.post("/users/privileges", data=data).status_code, 302)
        self.assertEqual([(row.recipient.username, row.event_type) for row in self.rows(leave_request)],
                         [("hr", "reviewer_changed")])
        self.assertEqual(self.client.post("/users/privileges", data=data).status_code, 302)
        self.assertEqual(len(self.rows(leave_request)), 1)

    def test_leadership_edit_notifies_new_scoped_reviewer_without_repeating_noop(self):
        leave_request = self.leave()
        self.login("ceo")
        data = {"legal_entity_id": self.contracts["hr"].legal_entity_id,
                "contract_id": self.contracts["hr"].id, "position": "principal",
                "start_date": (date.today() - timedelta(days=1)).isoformat()}
        self.assertEqual(self.client.post("/leadership", data=data).status_code, 302)
        rows = self.rows(leave_request)
        self.assertEqual([(row.recipient.username, row.event_type) for row in rows], [("hr", "reviewer_changed")])
        data["leadership_id"] = self.contracts["hr"].leadership_positions[0].id
        self.assertEqual(self.client.post("/leadership", data=data).status_code, 302)
        self.assertEqual(len(self.rows(leave_request)), 1)
        expired = self.contracts["expired"].leadership_positions[0]
        data.update(leadership_id=expired.id, contract_id=self.contracts["expired"].id, end_date="")
        self.assertEqual(self.client.post("/leadership", data=data).status_code, 302)
        self.assertEqual([row.recipient.username for row in self.rows(leave_request)], ["hr", "expired"])

    def test_reconciliation_notifies_naturally_starting_leader_once_and_skips_completed(self):
        now = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
        principal = self.contracts["principal"].leadership_positions[0]
        principal.start_date = now.date() + timedelta(days=1)
        leave_request = self.leave(start_date=now.date() + timedelta(days=20))
        record_leave_change(leave_request, None, self.users["employee"], now=now)
        db.session.commit()
        self.assertNotIn(self.users["principal"].id, {row.recipient_id for row in self.rows(leave_request)})
        self.assertEqual(reconcile_pending_leave_tasks(now=now), 0)
        tomorrow = now + timedelta(days=1)
        self.assertEqual(reconcile_pending_leave_tasks(now=tomorrow), 1)
        db.session.commit()
        self.assertEqual(self.rows(leave_request)[-1].recipient_id, self.users["principal"].id)
        self.assertEqual(self.rows(leave_request)[-1].event_type, "reviewer_changed")
        self.assertEqual(reconcile_pending_leave_tasks(now=tomorrow), 0)
        leave_request.status = LeaveRequestStatus.cancelled
        self.assertEqual(reconcile_pending_leave_tasks(now=tomorrow), 0)


if __name__ == "__main__":
    unittest.main()
