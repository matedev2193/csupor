"""Page grants govern actual routes, reviewer tasks and safe landing routes."""

import unittest
from datetime import date
from unittest.mock import patch

import test_leave_approval as fixtures
from app import db
from app.approval_display import approval_description
from app.models import LegalEntity, LeaveApprovalPolicy, LeaveApprovalSettings, LeaveRequest, LeaveRequestStatus, User, UserPrivilege
from app.notification_events import actionable_recipient_ids, notification_is_actionable, record_leave_change
from app.notification_models import LeaveNotification
from app.page_access import ALL_ROLES, PAGE_DEFINITIONS, invalidate_access_cache
from app.page_access_models import PageAccessSettings, PageRolePermission
from app.routes import _manager_review_leave_requests


class PageAccessRouteTests(unittest.TestCase):
    setUp = fixtures.LeaveApprovalTests.setUp
    tearDown = fixtures.LeaveApprovalTests.tearDown
    login = fixtures.LeaveApprovalTests.login
    set_policy = fixtures.LeaveApprovalTests.set_policy
    leave = fixtures.LeaveApprovalTests.leave
    act = fixtures.LeaveApprovalTests.act

    def allow(self, page, role, allowed):
        row = db.session.get(PageRolePermission, (page, role))
        if row is None:
            row = PageRolePermission(page_key=page, role=role)
            db.session.add(row)
        row.allowed = allowed
        db.session.commit()
        invalidate_access_cache()

    def matrix_save(self, changes):
        self.login("developer")
        self.assertEqual(self.client.get("/page-access").status_code, 200)
        with self.client.session_transaction() as session:
            token = session["page_access_csrf_token"]
        rules = {(row.page_key, row.role): row.allowed for row in PageRolePermission.query.all()}
        rules.update(changes)
        permissions = [f"{page['key']}:{role}" for page in PAGE_DEFINITIONS for role in ALL_ROLES
                       if rules.get((page["key"], role), role in page["default_roles"])]
        with patch("app.notification_delivery.wake_notifications"):
            return self.client.post("/page-access", data={
                "csrf_token": token, "revision": db.session.get(PageAccessSettings, 1).revision,
                "permissions": permissions,
            })

    def test_employee_grant_opens_selected_management_page_and_its_write_routes(self):
        self.allow("manage_legal_entities", "employee", True)
        self.login("employee")
        self.assertEqual(self.client.get("/legal-entities").status_code, 200)
        self.assertEqual(self.client.get("/legal-entities/new").status_code, 200)
        response = self.client.post("/legal-entities/new", data={
            "name": "Granted entity", "address": "Example", "om_id": "123456", "tax_number": "12345678901",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(LegalEntity.query.filter_by(name="Granted entity").first())
        self.assertEqual(self.client.get("/places-of-work").status_code, 403)
        self.assertEqual(self.client.post("/places-of-work/new", data={"address": "Forbidden"}).status_code, 403)
        self.assertEqual(self.client.get("/contracts").status_code, 403)

    def test_revoked_hr_page_blocks_list_create_edit_and_direct_post(self):
        self.allow("manage_legal_entities", "hr", False)
        self.login("hr")
        entity = LegalEntity.query.first()
        original_name = entity.name
        for path in ("/legal-entities", "/legal-entities/new", f"/legal-entities/{entity.id}/edit"):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post(f"/legal-entities/{entity.id}/edit", data={
            "name": "Not allowed", "address": "Example", "tax_number": "12345678901",
        }).status_code, 403)
        db.session.refresh(entity)
        self.assertEqual(entity.name, original_name)
        self.assertEqual(self.client.get("/places-of-work").status_code, 200)
        self.assertEqual(self.client.get("/contracts").status_code, 200)

    def test_profile_management_grant_and_revocation_cover_edit_and_delete(self):
        target = self.users["ceo"]
        self.allow("manage_user_profiles", "employee", True)
        self.login("employee")
        self.assertEqual(self.client.get("/users/profiles").status_code, 200)
        self.assertEqual(self.client.get(f"/users/{target.id}/profile").status_code, 200)
        self.allow("manage_user_profiles", "employee", False)
        for path in ("/users/profiles", f"/users/{target.id}/profile"):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post(f"/users/{target.id}/delete", data={"manager_password": "anything"}).status_code, 403)
        self.assertIsNotNone(db.session.get(User, target.id))

    def test_approval_settings_grant_can_be_used_by_another_role(self):
        self.allow("leave_approval_settings", "hr", True)
        self.login("hr")
        self.assertEqual(self.client.get("/leaves/approval-settings").status_code, 200)
        with self.client.session_transaction() as session:
            token = session["leave_approval_csrf_token"]
        response = self.client.post("/leaves/approval-settings", data={
            "csrf_token": token, "policy": "either", "previous_policy": "both",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(LeaveApprovalSettings, 1).policy, LeaveApprovalPolicy.either)
        self.assertEqual(db.session.get(LeaveApprovalSettings, 1).updated_by_id, self.users["hr"].id)

    def test_reviewer_page_grant_preserves_role_entity_and_self_approval_rules(self):
        for role in ALL_ROLES:
            self.allow("manage_leaves", role, True)
        leave_request = self.leave()
        for role in ("employee", "hr", "developer", "expired"):
            self.assertEqual(self.act(leave_request, role).status_code, 403)
        self.assertEqual(self.act(leave_request, "outsider").status_code, 302)
        self.assertIsNone(leave_request.ceo_approved_by_id)
        self.assertIsNone(leave_request.leadership_approved_by_id)
        deputy_leave = self.leave("deputy")
        self.assertEqual(self.act(deputy_leave, "deputy").status_code, 302)
        self.assertIsNone(deputy_leave.leadership_approved_by_id)

    def test_revoking_review_access_blocks_pending_and_cancellation_actions(self):
        self.allow("manage_leaves", "ceo", False)
        for status, actions in ((LeaveRequestStatus.pending_approval, ("approve", "reject")),
                                (LeaveRequestStatus.pending_cancellation, ("cancel", "reject"))):
            leave_request = self.leave(status=status)
            for action in actions:
                self.assertEqual(self.act(leave_request, "ceo", action).status_code, 403)
            self.assertEqual(leave_request.status, status)
            self.assertEqual(LeaveNotification.query.filter_by(leave_request_id=leave_request.id).count(), 0)
        self.assertEqual(_manager_review_leave_requests(self.users["ceo"]), [])
        self.assertEqual(self.client.get("/leaves/manage").status_code, 403)

    def test_notification_recipient_list_and_send_time_checks_exclude_revoked_reviewers(self):
        leave_request = self.leave()
        rows = record_leave_change(leave_request, None, self.users["employee"])
        db.session.commit()
        ceo_row = next(row for row in rows if row.recipient_id == self.users["ceo"].id)
        self.assertTrue(notification_is_actionable(ceo_row, self.users["ceo"]))
        self.allow("manage_leaves", "ceo", False)
        self.assertFalse(notification_is_actionable(ceo_row, self.users["ceo"]))
        recipients = actionable_recipient_ids(leave_request)
        self.assertEqual(recipients, {self.users[key].id for key in ("principal", "deputy")})
        self.allow("manage_leaves", "employee", False)
        self.assertEqual(actionable_recipient_ids(leave_request), set())

    def test_matrix_regrant_queues_new_tasks_for_pending_requests(self):
        leave_request = self.leave()
        record_leave_change(leave_request, None, self.users["employee"])
        db.session.commit()
        original_count = LeaveNotification.query.count()
        self.assertEqual(self.matrix_save({("manage_leaves", "ceo"): False}).status_code, 302)
        self.assertEqual(LeaveNotification.query.count(), original_count)
        self.assertEqual(self.matrix_save({("manage_leaves", "ceo"): True}).status_code, 302)
        added = LeaveNotification.query.filter_by(event_type="reviewer_changed").all()
        self.assertEqual({row.recipient_id for row in added}, {self.users[key].id for key in ("ceo", "dual")})
        self.assertTrue(all(row.is_task and not row.is_owner for row in added))
        self.assertEqual(self.matrix_save({("manage_leaves", "ceo"): True}).status_code, 302)
        self.assertEqual(LeaveNotification.query.count(), original_count + 2)

    def test_approval_names_only_list_people_who_can_act(self):
        self.allow("manage_leaves", "ceo", False)
        with self.app.test_request_context():
            description = approval_description(self.contracts["employee"], self.users["employee"], LeaveApprovalPolicy.either)
        self.assertNotIn("ceo", description)
        self.assertNotIn("dual", description)
        self.assertIn("principal", description)
        self.assertIn("deputy", description)

    def test_own_automatic_approvals_keep_business_policy_without_manual_page(self):
        for key, policy, role in (("ceo", LeaveApprovalPolicy.ceo_only, "ceo"),
                                  ("principal", LeaveApprovalPolicy.leadership_only, "employee")):
            self.set_policy(policy)
            self.allow("manage_leaves", role, False)
            self.login(key)
            response = self.client.post("/leaves", data={
                "contract_id": self.contracts[key].id, "category": "health leave",
                "start_date": date.today().isoformat(),
            })
            self.assertEqual(response.status_code, 302)
            created = LeaveRequest.query.filter_by(user_id=self.users[key].id).order_by(LeaveRequest.id.desc()).first()
            self.assertEqual(created.status, LeaveRequestStatus.approved)
            self.assertEqual(created.decided_by_id, self.users[key].id)

    def test_blocked_dashboard_login_and_index_use_first_accessible_page(self):
        self.allow("dashboard", "employee", False)
        self.users["employee"].set_password("Known-local-password")
        db.session.commit()
        response = self.client.post("/login", data={"login": "employee", "password": "Known-local-password"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/profile")
        self.assertEqual(self.client.get("/").headers["Location"], "/profile")
        self.assertEqual(self.client.get("/dashboard").status_code, 403)
        self.assertEqual(self.client.get("/profile").status_code, 200)

    def test_account_with_every_page_disabled_has_a_working_landing_and_logout(self):
        for page in PAGE_DEFINITIONS:
            self.allow(page["key"], "employee", False)
        self.login("employee")
        response = self.client.get("/", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, "/access-unavailable")
        self.assertEqual(self.client.get("/dashboard").status_code, 403)
        self.assertEqual(self.client.get("/logout").headers["Location"], "/login")

    def test_registration_skips_forbidden_profile_and_dashboard(self):
        self.allow("dashboard", "employee", False)
        self.allow("edit_profile", "employee", False)
        response = self.client.post("/register", data={
            "username": "new-member", "email": "new-member@example.invalid", "password": "Local-only",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dependents")
        self.assertEqual(self.client.get(response.headers["Location"]).status_code, 200)

    def test_password_save_falls_back_when_dashboard_is_forbidden(self):
        self.allow("dashboard", "employee", False)
        self.users["employee"].set_password("Before-change")
        db.session.commit()
        self.login("employee")
        response = self.client.post("/password", data={
            "current_password": "Before-change", "new_password": "After-change", "new_password_confirm": "After-change",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/profile")
        self.assertTrue(self.users["employee"].check_password("After-change"))

    def test_developer_cannot_demote_self_and_lose_access_matrix(self):
        self.login("developer")
        for role in ("employee", "hr", "ceo"):
            response = self.client.post("/users/privileges", data={
                "user_id": self.users["developer"].id, "privilege": role,
            })
            self.assertEqual(response.status_code, 302)
            db.session.refresh(self.users["developer"])
            self.assertEqual(self.users["developer"].privilege, UserPrivilege.developer)
            self.assertEqual(self.client.get("/page-access").status_code, 200)

    def test_other_role_self_change_redirects_to_an_accessible_page(self):
        self.login("hr")
        response = self.client.post("/users/privileges", data={
            "user_id": self.users["hr"].id, "privilege": "employee",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard")
        self.assertEqual(self.client.get("/users/privileges").status_code, 403)
        self.assertEqual(self.client.get(response.headers["Location"]).status_code, 200)


if __name__ == "__main__":
    unittest.main()
