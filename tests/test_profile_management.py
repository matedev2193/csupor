"""Profile filtering and password-confirmed account deletion with real FK checks."""

import os
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from flask import g, template_rendered
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash

from app import create_app, db
from app.models import (
    Contract, ContractLeaveLimit, ContractType, Dependent, DependentType,
    EducationalQualification, Gender, GyapForm, Leadership, LeadershipPosition,
    LegalEntity, LeaveApprovalSettings, LeaveRequest, LeaveRequestCategory,
    LeaveRequestStatus, LeaveType, LeaveYear, MaritalStatus, PlaceOfWork,
    ProfessionalExam, ProfilePhoto, User, UserPrivilege, UserProfile,
)
from app.routes import PROFILE_COMPLETION_FIELDS, _profile_completion_percentage, _profile_completion_state


PASSWORDS = {key: f"{key}-own-password" for key in ("hr", "ceo", "employee", "developer", "target", "other")}
HASHES = {key: generate_password_hash(value, method="pbkdf2:sha256:1000") for key, value in PASSWORDS.items()}


class ProfileManagementTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "SECRET_KEY": "profile-management-test-only"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.assertEqual(db.session.execute(text("PRAGMA foreign_keys")).scalar(), 1)
        self.today = date(2026, 10, 4)
        self.date_patch = patch("app.routes.local_today", return_value=self.today)
        self.date_patch.start()
        self.users = {}
        for key in PASSWORDS:
            self.users[key] = User(
                username=key, email=f"{key}@example.invalid", password_hash=HASHES[key],
                privilege=UserPrivilege(key) if key in {"hr", "ceo", "developer"} else UserPrivilege.employee,
            )
            db.session.add(self.users[key])
        self.entity = LegalEntity(name="Nursery", address="Example", om_id="123456", tax_number="12345678901")
        self.place = PlaceOfWork(legal_entity=self.entity, address="Example")
        db.session.add_all([self.entity, self.place])
        db.session.commit()
        self.login("hr")

    def tearDown(self):
        self.date_patch.stop()
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def login(self, key):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.users[key].id)
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)
        g.pop("leave_approval_policy", None)

    def get_profiles(self, **query):
        templates = []
        def record(sender, template, context, **extra):
            templates.append(context)
        with template_rendered.connected_to(record, self.app):
            response = self.client.get("/users/profiles", query_string=query)
        self.assertEqual(response.status_code, 200)
        return response, templates[-1]

    def delete(self, user=None, password=None, csrf=None):
        self.get_profiles(status="all")
        with self.client.session_transaction() as session:
            token = session["profile_delete_csrf_token"]
        return self.client.post(f"/users/{(user or self.users['target']).id}/delete", data={
            "csrf_token": token if csrf is None else csrf,
            "manager_password": PASSWORDS["hr"] if password is None else password,
        })

    def contract(self, key="target", start=None, end=None):
        contract = Contract(
            user=self.users[key], employer=self.entity, place_of_work=self.place,
            contract_type=ContractType.teacher, job_title="Teacher", working_hours_per_week=40,
            start_date=start or self.today, end_date=end,
        )
        db.session.add(contract)
        db.session.commit()
        return contract

    def complete_profile(self, key="target"):
        values = {field: "Recorded" for field in PROFILE_COMPLETION_FIELDS}
        values.update(date_of_birth=date(1990, 1, 1), gender=Gender.male, marital_status=MaritalStatus.single)
        profile = UserProfile(user=self.users[key], **values)
        db.session.add(profile)
        db.session.commit()
        return profile

    def test_profile_state_requires_every_meaningful_required_field(self):
        self.assertEqual(_profile_completion_state(None), "empty")
        self.assertEqual(_profile_completion_state(UserProfile(full_name=" \t\n")), "empty")
        self.assertEqual(_profile_completion_percentage(UserProfile(full_name=" \t")), 0)
        self.assertEqual(_profile_completion_state(UserProfile(temporary_address="Address")), "partial")
        self.assertEqual(_profile_completion_state(UserProfile(full_name="Name")), "partial")
        profile = self.complete_profile()
        self.assertEqual(_profile_completion_state(profile), "complete")
        self.assertEqual(_profile_completion_percentage(profile), 100)
        for field in PROFILE_COMPLETION_FIELDS:
            with self.subTest(field=field):
                original = getattr(profile, field)
                setattr(profile, field, "  " if isinstance(original, str) else None)
                self.assertEqual(_profile_completion_state(profile), "partial")
                self.assertLess(_profile_completion_percentage(profile), 100)
                setattr(profile, field, original)

    def test_status_filters_use_inclusive_budapest_contract_dates_and_no_duplicate_users(self):
        self.contract(start=self.today, end=self.today)
        self.contract(start=self.today - timedelta(days=1))
        self.contract("other", start=self.today + timedelta(days=1))
        self.contract("employee", start=self.today - timedelta(days=2), end=self.today - timedelta(days=1))
        _, context = self.get_profiles()
        self.assertEqual([user.id for user in context["users"]], [self.users["target"].id])
        self.assertEqual(context["counts"], {"active": 1, "inactive": 5, "all": 6})
        _, inactive = self.get_profiles(status="inactive")
        self.assertNotIn(self.users["target"], inactive["users"])
        self.assertIn(self.users["other"], inactive["users"])
        self.assertIn(self.users["employee"], inactive["users"])
        _, everyone = self.get_profiles(status="all")
        self.assertEqual(len(everyone["users"]), 6)

    def test_search_profile_status_and_counts_are_combined_and_preserved_in_tabs(self):
        self.users["target"].profile = UserProfile(full_name="Árvíztűrő Máté")
        self.contract()
        self.users["other"].profile = UserProfile(full_name="Árvíztűrő Other")
        db.session.commit()
        response, context = self.get_profiles(status="all", q="ÁRVÍZTŰRŐ", profile_state="partial")
        self.assertEqual(context["counts"], {"active": 1, "inactive": 1, "all": 2})
        self.assertEqual(len(context["users"]), 2)
        self.assertIn(b"profile-status-partial", response.data)
        self.assertIn(b"Details partially recorded", response.data)
        self.assertIn(b"profile_state=partial", response.data)
        for query in ("target", "target@example.invalid"):
            _, result = self.get_profiles(status="all", q=query)
            self.assertEqual(result["users"], [self.users["target"]])

    def test_empty_partial_and_complete_filters_have_distinct_badges(self):
        self.complete_profile()
        self.users["other"].profile = UserProfile(phone_number="123")
        db.session.commit()
        for state, expected in (("empty", 4), ("partial", 1), ("complete", 1)):
            with self.subTest(state=state):
                response, context = self.get_profiles(status="all", profile_state=state)
                self.assertEqual(len(context["users"]), expected)
                self.assertIn(f'data-profile-state="{state}"'.encode(), response.data)
        response, context = self.get_profiles(status="all", q="does-not-exist")
        self.assertEqual(context["users"], [])
        self.assertIn(b"No users match the selected filters.", response.data)
        self.assertIn(b"Show all users", response.data)

    def test_list_and_delete_are_restricted_to_hr_and_ceo(self):
        target_id = self.users["target"].id
        for key in ("employee", "developer"):
            self.login(key)
            self.assertEqual(self.client.get("/users/profiles").status_code, 403)
            self.assertEqual(self.client.post(f"/users/{target_id}/delete", data={"manager_password": PASSWORDS[key]}).status_code, 403)
            self.assertIsNotNone(db.session.get(User, target_id))
        self.login("hr")
        self.assertEqual(self.client.get(f"/users/{target_id}/delete").status_code, 405)

    def test_wrong_or_target_password_cannot_delete_or_leak_password(self):
        for password in ("wrong-password", PASSWORDS["target"], ""):
            with self.subTest(password_kind="empty" if not password else "incorrect"):
                response = self.delete(password=password)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn(password or "manager_password", response.location)
                self.assertIsNotNone(db.session.get(User, self.users["target"].id))
                with self.client.session_transaction() as session:
                    self.assertIn("Your password is incorrect", session["_flashes"][-1][1])

    def test_missing_or_invalid_csrf_preserves_account_even_with_correct_password(self):
        for token in ("", "invalid", "árvíz"):
            response = self.delete(csrf=token)
            self.assertEqual(response.status_code, 400)
            self.assertIsNotNone(db.session.get(User, self.users["target"].id))

    def test_self_and_last_ceo_deletion_are_blocked(self):
        self.assertEqual(self.delete(user=self.users["hr"]).status_code, 302)
        self.assertIsNotNone(db.session.get(User, self.users["hr"].id))
        self.assertEqual(self.delete(user=self.users["ceo"]).status_code, 302)
        self.assertIsNotNone(db.session.get(User, self.users["ceo"].id))
        with self.client.session_transaction() as session:
            self.assertIn("last CEO", session["_flashes"][-1][1])

    def test_hr_can_delete_another_ceo_when_a_ceo_will_remain(self):
        self.users["other"].privilege = UserPrivilege.ceo
        db.session.commit()
        deleted_id = self.users["ceo"].id
        self.assertEqual(self.delete(user=self.users["ceo"]).status_code, 302)
        self.assertIsNone(db.session.get(User, deleted_id))
        self.assertIsNotNone(db.session.get(User, self.users["other"].id))

    def test_ceo_can_delete_with_own_password(self):
        self.login("ceo")
        deleted_id = self.users["target"].id
        self.assertEqual(self.delete(password=PASSWORDS["ceo"]).status_code, 302)
        self.assertIsNone(db.session.get(User, deleted_id))

    def seed_owned_and_shared_records(self):
        target = self.users["target"]
        contract = self.contract()
        other_contract = self.contract("other")
        self.complete_profile()
        target_id = target.id
        own_request = LeaveRequest(user=target, contract=contract, category=LeaveRequestCategory.health_leave,
                                   start_date=self.today, status=LeaveRequestStatus.approved)
        other_request = LeaveRequest(user=self.users["other"], contract=other_contract, category=LeaveRequestCategory.health_leave,
                                     start_date=self.today, status=LeaveRequestStatus.approved, ceo_approved_by_id=target_id,
                                     leadership_approved_by_id=target_id, decided_by_id=target_id)
        db.session.add_all([
            Dependent(user=target, name="Child", dependent_type=DependentType.child, date_of_birth=date(2020, 1, 1),
                      social_security_number="123456789", dependency_start=date(2020, 1, 1)),
            EducationalQualification(user=target, level_or_type="Degree", qualification_name="Teacher", institution_name="University",
                                     degree_number="ABC", year_obtained=2020, highest=True),
            ProfessionalExam(user=target, qualification_name="Exam", year_obtained=2021, degree_number="XYZ"),
            ProfilePhoto(user=target, version="a" * 32, mime_type="image/jpeg", width=1, height=1, size_bytes=3, data=b"abc", updated_at=datetime.now()),
            ContractLeaveLimit(contract=contract, calendar_year=2026, leave_type=LeaveType.basic_leave, limit_days=20),
            Leadership(contract=contract, legal_entity=self.entity, position=LeadershipPosition.principal, start_date=self.today),
            LeaveYear(year=2026, is_open=True, imported_by=target),
            GyapForm(year=2026, filename="blank.pdf", mime_type="application/pdf", size_bytes=3, data=b"pdf", uploaded_by=target, uploaded_at=datetime.now()),
            own_request, other_request,
        ])
        db.session.get(LeaveApprovalSettings, 1).updated_by = target
        db.session.commit()
        return contract.id, other_contract.id, other_request.id

    def test_delete_cascades_owned_records_and_preserves_other_users_and_shared_forms(self):
        contract_id, other_contract_id, other_request_id = self.seed_owned_and_shared_records()
        target_id = self.users["target"].id
        response = self.delete()
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(User, target_id))
        for model in (UserProfile, Dependent, EducationalQualification, ProfessionalExam, ProfilePhoto):
            self.assertEqual(model.query.filter_by(user_id=target_id).count(), 0)
        self.assertIsNone(db.session.get(Contract, contract_id))
        self.assertEqual(ContractLeaveLimit.query.filter_by(contract_id=contract_id).count(), 0)
        self.assertEqual(Leadership.query.filter_by(contract_id=contract_id).count(), 0)
        self.assertEqual(LeaveRequest.query.filter_by(user_id=target_id).count(), 0)
        self.assertIsNotNone(db.session.get(Contract, other_contract_id))
        other_request = db.session.get(LeaveRequest, other_request_id)
        self.assertEqual(other_request.status, LeaveRequestStatus.approved)
        self.assertEqual(other_request.user_id, self.users["other"].id)
        self.assertIsNone(other_request.ceo_approved_by_id)
        self.assertIsNone(other_request.leadership_approved_by_id)
        self.assertIsNone(other_request.decided_by_id)
        self.assertIsNone(db.session.get(LeaveYear, 2026).imported_by_id)
        self.assertIsNone(db.session.get(LeaveApprovalSettings, 1).updated_by_id)
        self.assertIsNone(db.session.get(GyapForm, 2026).uploaded_by_id)
        self.assertEqual(db.session.get(GyapForm, 2026).data, b"pdf")
        self.assertEqual(LegalEntity.query.count(), 1)
        self.assertEqual(PlaceOfWork.query.count(), 1)

    def test_database_failure_rolls_back_owned_deletion_and_external_reference_changes(self):
        _, _, other_request_id = self.seed_owned_and_shared_records()
        target_id = self.users["target"].id
        with patch.object(db.session, "commit", side_effect=SQLAlchemyError("simulated failure")):
            response = self.delete()
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(db.session.get(User, target_id))
        self.assertEqual(db.session.get(LeaveRequest, other_request_id).decided_by_id, target_id)
        self.assertEqual(db.session.get(GyapForm, 2026).uploaded_by_id, target_id)
        self.assertEqual(ProfilePhoto.query.filter_by(user_id=target_id).count(), 1)

    def test_inconsistent_ownership_cannot_cascade_delete_another_users_request(self):
        contract = self.contract()
        request = LeaveRequest(user=self.users["other"], contract=contract, category=LeaveRequestCategory.health_leave,
                               start_date=self.today, status=LeaveRequestStatus.approved)
        db.session.add(request)
        db.session.commit()
        request_id = request.id
        target_id = self.users["target"].id
        self.delete()
        self.assertIsNotNone(db.session.get(User, target_id))
        self.assertIsNotNone(db.session.get(LeaveRequest, request_id))


if __name__ == "__main__":
    unittest.main()
