"""End-to-end permission and state-transition tests for configurable approvals."""

import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from flask import g

from app import create_app, db
from app.leave_approval import initialise_leave_approval_settings
from app.models import (
    Contract, ContractType, Leadership, LeadershipPosition, LegalEntity,
    LeaveApprovalPolicy, LeaveApprovalSettings, LeaveRequest,
    LeaveRequestCategory, LeaveRequestStatus, LeaveYear, PlaceOfWork,
    User, UserPrivilege,
)
from app.routes import _manager_review_leave_requests


class LeaveApprovalTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "SECRET_KEY": "approval-test-only"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        self.contracts = {}
        today = date.today()
        for key in ("employee", "ceo", "hr", "developer", "principal", "deputy", "outsider", "expired", "dual"):
            privilege = UserPrivilege(key) if key in ("ceo", "hr", "developer") else UserPrivilege.employee
            if key == "dual":
                privilege = UserPrivilege.ceo
            user = User(username=key, email=f"{key}@example.invalid", password_hash="unused", privilege=privilege)
            db.session.add(user)
            self.users[key] = user
        for number in (1, 2):
            entity = LegalEntity(name=f"Entity {number}", address="Example", om_id=f"{number:06}", tax_number=f"{number:011}")
            place = PlaceOfWork(legal_entity=entity, address="Example")
            for key, user in self.users.items():
                if (key == "outsider") != (number == 2):
                    continue
                contract = Contract(
                    user=user, employer=entity, place_of_work=place, contract_type=ContractType.teacher,
                    start_date=today - timedelta(days=365), job_title="Teacher", working_hours_per_week=40,
                )
                self.contracts[key] = contract
                db.session.add(contract)
                if key in ("principal", "deputy", "outsider", "expired", "dual"):
                    db.session.add(Leadership(
                        legal_entity=entity, contract=contract,
                        position=LeadershipPosition.deputy_principal if key == "deputy" else LeadershipPosition.principal,
                        start_date=today - timedelta(days=300),
                        end_date=today - timedelta(days=1) if key == "expired" else None,
                    ))
        db.session.add_all([LeaveYear(year=today.year, is_open=True), LeaveYear(year=today.year + 1, is_open=True)])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def login(self, key):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.users[key].id)
            session["_fresh"] = True
        # The tests hold an app context; real requests have fresh contexts.
        g.pop("_login_user", None)
        g.pop("leave_approval_policy", None)

    def set_policy(self, policy):
        db.session.get(LeaveApprovalSettings, 1).policy = policy
        db.session.commit()
        g.pop("leave_approval_policy", None)

    def leave(self, key="employee", **kwargs):
        data = dict(
            user=self.users[key], contract=self.contracts[key],
            category=LeaveRequestCategory.health_leave, start_date=date.today(),
            status=LeaveRequestStatus.pending_approval,
        )
        data.update(kwargs)
        leave_request = LeaveRequest(**data)
        db.session.add(leave_request)
        db.session.commit()
        return leave_request

    def act(self, leave_request, key, action="approve"):
        self.login(key)
        response = self.client.post("/leaves/manage", data={"leave_request_id": leave_request.id, "action": action})
        db.session.refresh(leave_request)
        return response

    def save_policy(self, new_policy, **extra):
        self.login("ceo")
        self.assertEqual(self.client.get("/leaves/approval-settings").status_code, 200)
        with self.client.session_transaction() as session:
            token = session["leave_approval_csrf_token"]
        data = {
            "csrf_token": token, "policy": new_policy.value,
            "previous_policy": db.session.get(LeaveApprovalSettings, 1).policy.value,
        }
        data.update(extra)
        response = self.client.post("/leaves/approval-settings", data=data)
        g.pop("leave_approval_policy", None)
        return response

    def test_default_both_and_startup_preserves_saved_policy(self):
        self.assertEqual(db.session.get(LeaveApprovalSettings, 1).policy, LeaveApprovalPolicy.both)
        self.set_policy(LeaveApprovalPolicy.either)
        initialise_leave_approval_settings()
        self.assertEqual(db.session.get(LeaveApprovalSettings, 1).policy, LeaveApprovalPolicy.either)

    def test_settings_menu_and_get_post_are_ceo_only(self):
        for key in self.users:
            with self.subTest(user=key):
                self.login(key)
                expected = 200 if key in ("ceo", "dual") else 403
                self.assertEqual(self.client.get("/leaves/approval-settings").status_code, expected)
                dashboard = self.client.get("/dashboard")
                self.assertEqual(dashboard.status_code, 200)
                self.assertEqual(b'/leaves/approval-settings' in dashboard.data, expected == 200)
                if expected == 403:
                    self.assertEqual(self.client.post("/leaves/approval-settings", data={"policy": "either"}).status_code, 403)
        self.client.get("/logout")
        g.pop("_login_user", None)
        self.assertEqual(self.client.get("/leaves/approval-settings").status_code, 302)
        self.assertEqual(self.client.post("/leaves/approval-settings", data={"policy": "either"}).status_code, 302)

    def test_settings_validation_csrf_and_stale_form(self):
        self.assertEqual(self.save_policy(LeaveApprovalPolicy.either, csrf_token="").status_code, 400)
        self.assertEqual(self.save_policy(LeaveApprovalPolicy.either, csrf_token="árvíz").status_code, 400)
        self.assertEqual(self.save_policy(LeaveApprovalPolicy.either, policy="invalid").status_code, 400)
        response = self.save_policy(LeaveApprovalPolicy.either, previous_policy="ceo_only")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(LeaveApprovalSettings, 1).policy, LeaveApprovalPolicy.both)
        self.assertEqual(self.save_policy(LeaveApprovalPolicy.either).status_code, 302)
        db.session.expire_all()
        settings = db.session.get(LeaveApprovalSettings, 1)
        self.assertEqual(settings.policy, LeaveApprovalPolicy.either)
        self.assertEqual(settings.updated_by_id, self.users["ceo"].id)
        self.assertIsNotNone(settings.updated_at)

    def test_approval_truth_table_and_disabled_role_cannot_write(self):
        for policy in LeaveApprovalPolicy:
            for role in ("ceo", "principal", "deputy"):
                with self.subTest(policy=policy, role=role):
                    self.set_policy(policy)
                    leave_request = self.leave()
                    response = self.act(leave_request, role)
                    self.assertEqual(response.status_code, 302)
                    allowed = not (
                        policy == LeaveApprovalPolicy.ceo_only and role != "ceo"
                        or policy == LeaveApprovalPolicy.leadership_only and role == "ceo"
                    )
                    expected = LeaveRequestStatus.approved if allowed and policy != LeaveApprovalPolicy.both else LeaveRequestStatus.pending_approval
                    self.assertEqual(leave_request.status, expected)
                    self.assertEqual(leave_request.ceo_approved_by_id, self.users[role].id if allowed and role == "ceo" else None)
                    self.assertEqual(leave_request.leadership_approved_by_id, self.users[role].id if allowed and role != "ceo" else None)
                    if expected == LeaveRequestStatus.approved:
                        self.assertEqual(leave_request.decided_by_id, self.users[role].id)
                    elif allowed:
                        self.act(leave_request, "principal" if role == "ceo" else "ceo")
                        self.assertEqual(leave_request.status, LeaveRequestStatus.approved)

    def test_cannot_override_a_finished_decision_or_approve_twice(self):
        leave_request = self.leave()
        self.act(leave_request, "ceo")
        self.act(leave_request, "ceo")
        self.assertEqual(leave_request.status, LeaveRequestStatus.pending_approval)
        self.act(leave_request, "principal", "reject")
        self.act(leave_request, "ceo")
        self.assertEqual(leave_request.status, LeaveRequestStatus.rejected)
        self.set_policy(LeaveApprovalPolicy.either)
        leave_request = self.leave()
        self.act(leave_request, "ceo")
        self.act(leave_request, "principal")
        self.assertEqual(leave_request.status, LeaveRequestStatus.approved)
        self.assertIsNone(leave_request.leadership_approved_by_id)
        self.assertEqual(leave_request.decided_by_id, self.users["ceo"].id)

    def test_entity_expiry_and_self_approval_boundaries(self):
        for policy in LeaveApprovalPolicy:
            self.set_policy(policy)
            leave_request = self.leave()
            for key in ("employee", "hr", "developer", "expired", "outsider"):
                with self.subTest(policy=policy, user=key):
                    self.assertIn(self.act(leave_request, key).status_code, (302, 403))
                    self.assertIsNone(leave_request.ceo_approved_by_id)
                    self.assertIsNone(leave_request.leadership_approved_by_id)
            deputy_leave = self.leave("deputy")
            self.act(deputy_leave, "deputy")
            self.assertEqual(deputy_leave.status, LeaveRequestStatus.pending_approval)
            self.assertIsNone(deputy_leave.leadership_approved_by_id)

    def test_automatic_approvals_on_real_submission_for_every_policy(self):
        for policy in LeaveApprovalPolicy:
            self.set_policy(policy)
            for key in ("employee", "ceo", "principal", "deputy", "dual"):
                with self.subTest(policy=policy, applicant=key):
                    self.login(key)
                    day = date.today() + timedelta(days=list(LeaveApprovalPolicy).index(policy))
                    response = self.client.post("/leaves", data={
                        "contract_id": self.contracts[key].id,
                        "category": LeaveRequestCategory.health_leave.value,
                        "start_date": day.isoformat(),
                    })
                    self.assertEqual(response.status_code, 302)
                    leave_request = LeaveRequest.query.filter_by(user_id=self.users[key].id, start_date=day).one()
                    ceo = key in ("ceo", "dual") and policy != LeaveApprovalPolicy.leadership_only
                    principal = key in ("principal", "dual") and policy != LeaveApprovalPolicy.ceo_only
                    approved = (ceo and principal) if policy == LeaveApprovalPolicy.both else (ceo or principal)
                    self.assertEqual(leave_request.status, LeaveRequestStatus.approved if approved else LeaveRequestStatus.pending_approval)
                    self.assertEqual(leave_request.ceo_approved_by_id is not None, ceo)
                    self.assertEqual(leave_request.leadership_approved_by_id is not None, principal)

    def test_dashboard_review_queue_follows_policy(self):
        leave_request = self.leave()
        for policy in LeaveApprovalPolicy:
            self.set_policy(policy)
            for key in ("ceo", "principal", "deputy", "outsider"):
                with self.subTest(policy=policy, user=key):
                    expected = key != "outsider" and not (
                        policy == LeaveApprovalPolicy.ceo_only and key != "ceo"
                        or policy == LeaveApprovalPolicy.leadership_only and key == "ceo"
                    )
                    self.assertEqual(leave_request in _manager_review_leave_requests(self.users[key]), expected)
        self.set_policy(LeaveApprovalPolicy.both)
        self.act(leave_request, "ceo")
        self.assertNotIn(leave_request, _manager_review_leave_requests(self.users["ceo"]))
        self.assertIn(leave_request, _manager_review_leave_requests(self.users["principal"]))

    def test_policy_change_reconciles_only_pending_recorded_approvals(self):
        for policy in LeaveApprovalPolicy:
            self.set_policy(LeaveApprovalPolicy.both if policy != LeaveApprovalPolicy.both else LeaveApprovalPolicy.either)
            none = self.leave()
            ceo = self.leave(ceo_approved_by_id=self.users["ceo"].id)
            leader = self.leave(leadership_approved_by_id=self.users["principal"].id)
            both = self.leave(ceo_approved_by_id=self.users["ceo"].id, leadership_approved_by_id=self.users["principal"].id)
            completed = [self.leave(status=status, ceo_approved_by_id=self.users["ceo"].id) for status in LeaveRequestStatus if status != LeaveRequestStatus.pending_approval]
            statuses = [item.status for item in completed]
            self.save_policy(policy)
            db.session.expire_all()
            self.assertEqual(none.status, LeaveRequestStatus.pending_approval)
            self.assertEqual(ceo.status == LeaveRequestStatus.approved, policy in (LeaveApprovalPolicy.ceo_only, LeaveApprovalPolicy.either))
            self.assertEqual(leader.status == LeaveRequestStatus.approved, policy in (LeaveApprovalPolicy.leadership_only, LeaveApprovalPolicy.either))
            self.assertEqual(both.status, LeaveRequestStatus.approved)
            self.assertEqual(both.decided_by_id, self.users["ceo"].id)
            self.assertEqual([item.status for item in completed], statuses)
            self.assertTrue(all(item.decided_by_id is None for item in completed))
            self.assertEqual(leader.leadership_approved_by_id, self.users["principal"].id)

    def test_only_eligible_reviewer_can_reject_pending_and_review_cancellation(self):
        for policy, excluded, allowed in [
            (LeaveApprovalPolicy.ceo_only, "principal", "ceo"),
            (LeaveApprovalPolicy.leadership_only, "ceo", "principal"),
        ]:
            self.set_policy(policy)
            leave_request = self.leave()
            self.assertEqual(self.act(leave_request, excluded, "reject").status_code, 403)
            self.assertEqual(leave_request.status, LeaveRequestStatus.pending_approval)
            self.act(leave_request, allowed, "reject")
            self.assertEqual(leave_request.status, LeaveRequestStatus.rejected)
            cancel = self.leave(status=LeaveRequestStatus.pending_cancellation)
            self.assertEqual(self.act(cancel, excluded, "reject").status_code, 403)
            self.assertEqual(cancel.status, LeaveRequestStatus.pending_cancellation)
            self.act(cancel, allowed, "reject")
            self.assertEqual(cancel.status, LeaveRequestStatus.approved)
            self.assertEqual(self.act(cancel, excluded, "cancel").status_code, 403)
            self.assertEqual(cancel.status, LeaveRequestStatus.approved)
            self.act(cancel, allowed, "cancel")
            self.assertEqual(cancel.status, LeaveRequestStatus.cancelled)

    def test_localised_settings_and_required_approval_badges(self):
        self.leave()
        self.login("ceo")
        for locale, expected in (("en", "Approval settings"), ("hu", "Jóváhagyási beállítások")):
            with self.client.session_transaction() as session:
                session["locale"] = locale
            g.pop("_flask_babel", None)
            response = self.client.get("/leaves/approval-settings")
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected, response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            session["locale"] = "en"
        g.pop("_flask_babel", None)
        self.set_policy(LeaveApprovalPolicy.ceo_only)
        self.assertIn(b"Not required", self.client.get("/leaves/manage").data)
        self.set_policy(LeaveApprovalPolicy.either)
        self.assertIn(b"Either reviewer", self.client.get("/leaves/manage").data)


if __name__ == "__main__":
    unittest.main()
