"""People-facing workflows, approver identities and statutory explanations."""
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from flask import g, session

from app import create_app, db
from app.approval_display import approval_description
from app.leave_basis import age_supplement_days, eligible_children, leave_basis, paternity_deadline
from app.models import (Contract, ContractType, Dependent, DependentType, Gender, Leadership,
                        LeadershipPosition, LegalEntity, LeaveApprovalPolicy, LeaveType,
                        PlaceOfWork, User, UserPrivilege, UserProfile)
from app.people import birthday_context, local_today
from app.routes import _calculated_calendar_leave_limits


class PeopleContextTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "SECRET_KEY": "people-test-only"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context(); self.context.push()
        self.client = self.app.test_client()
        self.today = date(2026, 9, 28)
        self.users = {}
        for key, privilege in [("employee", UserPrivilege.employee), ("director", UserPrivilege.ceo),
                               ("head", UserPrivilege.employee), ("deputy", UserPrivilege.employee),
                               ("outsider", UserPrivilege.employee), ("hr", UserPrivilege.hr)]:
            user = User(username=key, email=key+"@example.invalid", privilege=privilege, password_hash="unused")
            user.profile = UserProfile(full_name=key.title()+" Test", date_of_birth=date(1990, 2, 1), gender=Gender.male)
            db.session.add(user);self.users[key] = user
        self.entity = LegalEntity(name="Nursery", address="Example", om_id="123456", tax_number="12345678901")
        self.place = PlaceOfWork(legal_entity=self.entity, address="Same workplace")
        self.other_place = PlaceOfWork(legal_entity=self.entity, address="Other workplace")
        self.contracts = {}
        for key, user in self.users.items():
            self.contracts[key] = self.contract(user, self.other_place if key == "outsider" else self.place)
        self.head_role = Leadership(contract=self.contracts["head"], legal_entity=self.entity,
                                    position=LeadershipPosition.principal, start_date=date(2025, 1, 1))
        self.deputy_role = Leadership(contract=self.contracts["deputy"], legal_entity=self.entity,
                                      position=LeadershipPosition.deputy_principal, start_date=date(2025, 1, 1))
        db.session.add_all([self.head_role, self.deputy_role]);db.session.commit()
        self.login("employee")

    def contract(self, user, place, start=date(2025, 1, 1), end=None, kind=ContractType.teacher):
        contract = Contract(user=user, employer=place.legal_entity, place_of_work=place, contract_type=kind,
                            start_date=start, end_date=end, job_title="Teacher", working_hours_per_week=40)
        db.session.add(contract)
        return contract

    def child(self, birthday, user=None, **kwargs):
        record = Dependent(user=user or self.users["employee"], name="Child", date_of_birth=birthday,
                           dependent_type=DependentType.child, social_security_number="123456789",
                           dependency_start=birthday, **kwargs)
        db.session.add(record);db.session.commit()
        return record

    def login(self, role, locale="en"):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.users[role].get_id();session["_fresh"] = True;session["locale"] = locale
        for key in ("_login_user", "_flask_babel", "leave_approval_policy"):
            g.pop(key, None)

    def tearDown(self):
        db.session.remove();db.engine.dispose();self.context.pop()

    def csrf(self):
        self.assertEqual(self.client.get("/dependents/add").status_code, 200)
        with self.client.session_transaction() as session:return session["dependent_csrf_token"]

    def form(self, **changes):
        return dict(name="Test Child", dependent_type="child", date_of_birth="2020-02-29",
                    dependency_start="2020-02-29", social_security_number="123456789", disability="", csrf_token=self.csrf()) | changes

    def description(self, policy, applicant="employee", contract=None):
        with self.app.test_request_context():
            session["locale"] = "en"
            return approval_description(contract or self.contracts[applicant], self.users[applicant], policy, self.today)

    def test_dependents_list_create_edit_preserves_owner_and_empty_note(self):
        self.login("employee", "hu")
        response = self.client.post("/dependents/add", data=self.form(), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Test Child", response.text)
        child = Dependent.query.filter_by(user_id=self.users["employee"].id).one()
        self.assertIsNone(child.disability)
        response = self.client.post(f"/dependents/{child.id}/edit", data=self.form(name="Updated Child", user_id=self.users["outsider"].id), follow_redirects=True)
        self.assertIn("Az eltartott adatai frissítve.", response.text)
        db.session.refresh(child)
        self.assertEqual(child.name, "Updated Child")
        self.assertEqual(child.user_id, self.users["employee"].id)
        self.assertEqual(Dependent.query.count(), 1)
        self.assertIn(f'/dependents/{child.id}/edit', self.client.get("/dashboard").text)
        self.assertIn('href="/dependents"', self.client.get("/profile").text)

    def test_dependents_cannot_view_or_edit_another_users_records(self):
        child = self.child(date(2020, 1, 1), user=self.users["outsider"])
        self.assertNotIn(f'/dependents/{child.id}/edit', self.client.get('/dependents').text)
        for role in ("employee", "hr", "director"):
            self.login(role)
            self.assertEqual(self.client.get(f"/dependents/{child.id}/edit").status_code, 404)
            self.assertEqual(self.client.post(f"/dependents/{child.id}/edit", data=self.form()).status_code, 404)
        self.assertEqual(child.name, "Child")

    def test_dependent_validation_retains_input_without_partial_write(self):
        child = self.child(date(2020, 1, 1))
        for invalid in ({"social_security_number": "１２３４５６７８９"}, {"date_of_birth": "2020-02-30"},
                        {"date_of_birth": "2099-01-01"}, {"dependency_start": "2010-01-01"},
                        {"dependent_type": "invalid"}, {"social_security_number": ""}):
            response = self.client.post(f"/dependents/{child.id}/edit", data=self.form(name="Keep this input") | invalid)
            self.assertEqual(response.status_code, 200)
            self.assertIn('value="Keep this input"', response.text)
            db.session.refresh(child);self.assertEqual(child.name, "Child")
        self.assertEqual(self.client.post(f"/dependents/{child.id}/edit", data=self.form(csrf_token="wrong")).status_code, 400)
        self.assertEqual(Dependent.query.count(), 1)

    def test_dependent_birth_date_picker_uses_budapest_today_for_create_and_edit(self):
        child = self.child(date(2020, 1, 1))
        with patch("app.people.datetime") as clock:
            clock.now.side_effect = lambda tz: datetime(2026, 9, 27, 22, 30, tzinfo=timezone.utc).astimezone(tz)
            for path in ("/dependents/add", f"/dependents/{child.id}/edit"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertRegex(response.text, r'<input id="date_of_birth"[^>]*max="2026-09-28"')

    def test_future_dependent_birth_is_rejected_without_partial_create_or_edit(self):
        child = self.child(date(2020, 1, 1), disability="Existing note")
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        original = (child.name, child.date_of_birth, child.dependency_start,
                    child.social_security_number, child.disability, child.user_id)
        with patch("app.routes.local_today", return_value=self.today):
            for path in ("/dependents/add", f"/dependents/{child.id}/edit"):
                with self.subTest(path=path):
                    response = self.client.post(path, data=self.form(
                        name="Keep future input", date_of_birth=tomorrow, dependency_start=tomorrow,
                        social_security_number="987654321", disability="Changed note"))
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("The date of birth cannot be in the future.", response.text)
                    self.assertIn('value="Keep future input"', response.text)
                    self.assertRegex(response.text, rf'<input id="date_of_birth"[^>]*value="{tomorrow}"')
                    self.assertIn('value="Changed note"', response.text)
                    self.assertEqual(Dependent.query.count(), 1)
                    db.session.refresh(child)
                    self.assertEqual((child.name, child.date_of_birth, child.dependency_start,
                                      child.social_security_number, child.disability, child.user_id), original)

    def test_dependent_born_today_can_be_created_and_edited_with_future_dependency_start(self):
        start = self.today + timedelta(days=1)
        with patch("app.routes.local_today", return_value=self.today):
            response = self.client.post("/dependents/add", data=self.form(
                name="Born today", date_of_birth=self.today.isoformat(), dependency_start=start.isoformat()))
            self.assertEqual(response.status_code, 302)
            child = Dependent.query.one()
            self.assertEqual(child.date_of_birth, self.today)
            self.assertEqual(child.dependency_start, start)
            response = self.client.post(f"/dependents/{child.id}/edit", data=self.form(
                name="Updated newborn", date_of_birth=self.today.isoformat(), dependency_start=start.isoformat()))
            self.assertEqual(response.status_code, 302)
            db.session.refresh(child)
            self.assertEqual(child.name, "Updated newborn")
            self.assertEqual(child.date_of_birth, self.today)
            self.assertEqual(child.dependency_start, start)
            self.assertEqual(Dependent.query.count(), 1)

    def test_named_approvals_for_all_four_policies(self):
        for policy, expected, excluded in [
            (LeaveApprovalPolicy.ceo_only, ["Director Test"], ["Head Test", "Deputy Test"]),
            (LeaveApprovalPolicy.leadership_only, ["Head Test", "Deputy Test"], ["Director Test"]),
            (LeaveApprovalPolicy.both, ["Director Test", "Head Test", "Deputy Test"], []),
            (LeaveApprovalPolicy.either, ["Director Test", "Head Test", "Deputy Test"], []),
        ]:
            text = self.description(policy)
            for name in expected:self.assertEqual(text.count(name), 1)
            for name in excluded:self.assertNotIn(name, text)
        self.assertIn("Both", self.description(LeaveApprovalPolicy.both))
        self.assertIn("any one", self.description(LeaveApprovalPolicy.either))

    def test_same_person_in_both_roles_appears_once_without_extra_approver(self):
        self.head_role.contract = self.contracts["director"]
        db.session.commit()
        text = self.description(LeaveApprovalPolicy.both)
        self.assertEqual(text.count("Director Test"), 1)
        self.assertNotIn("Deputy Test", text)
        self.assertNotIn("Head Test", text)
        self.assertNotIn("Both", text)

    def test_dual_role_and_multiple_directors_preserve_alternative_approval_paths(self):
        self.head_role.contract = self.contracts["director"]
        self.users["head"].privilege = UserPrivilege.ceo
        db.session.commit()
        text = self.description(LeaveApprovalPolicy.both)
        for name in ("Director Test", "Head Test", "Deputy Test"):
            self.assertEqual(text.count(name), 1)
        self.assertIn("alone, or", text)

    def test_expired_other_entity_and_self_deputy_are_not_named(self):
        self.head_role.end_date = self.today - timedelta(days=1)
        db.session.commit()
        text = self.description(LeaveApprovalPolicy.leadership_only, applicant="deputy")
        self.assertIn("No eligible", text)
        other = LegalEntity(name="Other entity", address="Other", om_id="999999", tax_number="99999999999")
        self.deputy_role.legal_entity = other;db.session.commit()
        self.assertIn("No eligible", self.description(LeaveApprovalPolicy.leadership_only))
        self.assertIn("Director Test", self.description(LeaveApprovalPolicy.either))

    def test_birthdays_use_active_shared_workplaces_and_deduplicate_people(self):
        for key in ("head", "deputy", "outsider", "hr"):
            self.users[key].profile.date_of_birth = date(1985, 9, 28)
        self.contracts["hr"].end_date = self.today - timedelta(days=1)
        self.contract(self.users["head"], self.place)
        db.session.commit()
        data = birthday_context(self.users["employee"], self.today)
        self.assertFalse(data["is_own_birthday"])
        self.assertEqual({user.username for user in data["birthday_colleagues"]}, {"head", "deputy"})
        self.assertEqual(len(data["birthday_colleagues"]), 2)
        self.contracts["employee"].end_date = self.today - timedelta(days=1);db.session.commit()
        self.assertEqual(birthday_context(self.users["employee"], self.today)["birthday_colleagues"], [])

    def test_director_birthdays_are_visible_without_a_shared_or_active_contract(self):
        self.users["director"].profile.date_of_birth = date(1985, 9, 28)
        self.users["outsider"].privilege = UserPrivilege.ceo
        self.users["outsider"].profile.date_of_birth = date(1980, 9, 28)
        self.contracts["director"].end_date = self.today - timedelta(days=1)
        db.session.delete(self.contracts["outsider"])
        self.contracts["employee"].end_date = self.today - timedelta(days=1)
        db.session.commit()
        for viewer in ("employee", "hr", "head"):
            with self.subTest(viewer=viewer):
                context = birthday_context(self.users[viewer], self.today)
                self.assertEqual([person.username for person in context["birthday_colleagues"]], ["director", "outsider"])

    def test_director_sees_active_users_across_workplaces_and_other_directors(self):
        other_entity = LegalEntity(name="Other nursery", address="Other", om_id="999999", tax_number="99999999999")
        other_place = PlaceOfWork(legal_entity=other_entity, address="Different organisation")
        self.contracts["outsider"].employer = other_entity
        self.contracts["outsider"].place_of_work = other_place
        for key in ("employee", "head", "deputy", "outsider", "hr", "director"):
            self.users[key].profile.date_of_birth = date(1985, 9, 28)
        self.contracts["employee"].start_date = self.today
        self.contracts["outsider"].end_date = self.today
        self.contracts["head"].start_date = self.today + timedelta(days=1)
        self.contracts["hr"].end_date = self.today - timedelta(days=1)
        self.users["deputy"].privilege = UserPrivilege.ceo
        self.contracts["deputy"].end_date = self.today - timedelta(days=1)
        self.contract(self.users["employee"], self.place)
        db.session.delete(self.contracts["director"])
        db.session.commit()
        context = birthday_context(self.users["director"], self.today)
        self.assertTrue(context["is_own_birthday"])
        self.assertEqual([person.username for person in context["birthday_colleagues"]], ["deputy", "employee", "outsider"])

    def test_birthday_visibility_still_requires_a_valid_birthday_today(self):
        self.users["director"].profile.date_of_birth = date(2030, 9, 28)
        self.users["head"].profile.date_of_birth = None
        self.users["deputy"].profile.date_of_birth = date(1985, 9, 27)
        db.session.commit()
        for viewer in ("employee", "director"):
            with self.subTest(viewer=viewer):
                context = birthday_context(self.users[viewer], self.today)
                self.assertFalse(context["is_own_birthday"])
                self.assertEqual(context["birthday_colleagues"], [])

    def test_own_birthday_keeps_colleague_announcements_and_handles_leap_days(self):
        for key in ("employee", "head"):
            self.users[key].profile.date_of_birth = date(2000, 2, 29)
        db.session.commit()
        self.assertFalse(birthday_context(self.users["employee"], date(2026, 2, 28))["is_own_birthday"])
        context = birthday_context(self.users["employee"], date(2028, 2, 29))
        self.assertTrue(context["is_own_birthday"])
        self.assertEqual([person.username for person in context["birthday_colleagues"]], ["head"])
        with patch("app.people.local_today", return_value=date(2028, 2, 29)):
            self.login("employee", "hu")
            text = self.client.get("/dashboard").text
            self.assertIn("Boldog születésnapot, Employee Test!", text)
            self.assertIn("Mai születésnapok", text)
            self.assertIn("<strong>Head Test</strong>", text)

    def test_single_birthday_announcement_has_requested_wording_and_bold_name(self):
        self.users["head"].profile.date_of_birth = date(1985, 9, 28)
        self.users["head"].profile.full_name = "Kiss Anna"
        db.session.commit()
        expected_by_locale = {
            "hu": "Ma ünnepli születésnapját <strong>Kiss Anna</strong> munkatársad. Ne felejtsd el felköszönteni őt!",
            "en": "Your colleague <strong>Kiss Anna</strong> is celebrating their birthday today. Don't forget to wish them a happy birthday!",
        }
        with patch("app.people.local_today", return_value=self.today):
            for locale, expected in expected_by_locale.items():
                with self.subTest(locale=locale):
                    self.login("employee", locale)
                    response = self.client.get("/dashboard")
                    self.assertEqual(response.status_code, 200)
                    birthday_card = response.text.split('aria-labelledby="colleague-birthday-heading"', 1)[1].split("</section>", 1)[0]
                    self.assertIn(expected, birthday_card)
                    self.assertNotIn("1985", birthday_card)
                    self.assertNotIn("1985-09-28", response.text)

    def test_multiple_birthday_names_are_joined_bold_and_html_escaped(self):
        self.users["head"].profile.full_name = "Anna <script>alert(1)</script>"
        self.users["deputy"].profile.full_name = "Béla & Társa"
        self.users["director"].profile.full_name = None
        for key in ("head", "deputy"):
            self.users[key].profile.date_of_birth = date(1985, 9, 28)
        db.session.commit()
        anna = "<strong>Anna &lt;script&gt;alert(1)&lt;/script&gt;</strong>"
        bela = "<strong>Béla &amp; Társa</strong>"
        with patch("app.people.local_today", return_value=self.today):
            self.login("employee", "hu")
            for count, names in ((2, f"{anna} és {bela}"), (3, f"{anna}, {bela} és <strong>director</strong>")):
                with self.subTest(count=count):
                    if count == 3:
                        self.users["director"].profile.date_of_birth = date(1985, 9, 28)
                        db.session.commit()
                    response = self.client.get("/dashboard")
                    self.assertEqual(response.status_code, 200)
                    birthday_card = response.text.split('aria-labelledby="colleague-birthday-heading"', 1)[1].split("</section>", 1)[0]
                    self.assertIn(f"Ma ünneplik születésnapjukat {names} munkatársaid. Ne felejtsd el felköszönteni őket!", birthday_card)
                    self.assertNotIn("<script>alert(1)</script>", response.text)

    def test_birthday_date_is_budapest_local_time(self):
        with patch("app.people.datetime") as clock:
            clock.now.side_effect = lambda tz: datetime(2026, 9, 27, 22, 30, tzinfo=timezone.utc).astimezone(tz)
            self.assertEqual(local_today(), date(2026, 9, 28))

    def test_legal_bases_distinguish_employment_regimes_and_recorded_limits(self):
        self.child(date(2010, 1, 1));self.child(date(2020, 1, 1));self.child(date(2009, 1, 1))
        future = self.child(date(2021, 1, 1));future.dependency_start = date(2027, 1, 1)
        db.session.commit()
        self.assertEqual(len(eligible_children(self.users["employee"], 2026)), 2)
        for kind, law, base_days in ((ContractType.teacher, "Púétv.", "50"), (ContractType.employee_under_the_labour_code, "Mt.", "20")):
            contract = self.contracts["employee"];contract.contract_type = kind
            with self.app.test_request_context():
                session["locale"] = "en"
                basic = leave_basis(contract, 2026, LeaveType.basic_leave)
                children = leave_basis(contract, 2026, LeaveType.supplementary_leave_for_children)
                sick = leave_basis(contract, 2026, LeaveType.sick_leave)
            self.assertTrue(basic["reference"].startswith(law));self.assertIn(base_days, " ".join(basic["paragraphs"]))
            self.assertIn("2 eligible children", " ".join(children["paragraphs"]))
            self.assertIn("4 working days", " ".join(children["paragraphs"]))
            self.assertIn("15 working days", " ".join(sick["paragraphs"]))
        # Rendering explanations never imports or overwrites saved limits.
        self.assertEqual(contract.leave_limits, [])

    def test_paternity_dates_multiple_children_and_cross_year_deadlines(self):
        self.assertEqual(paternity_deadline(date(2026, 10, 31)), date(2027, 2, 28))
        self.assertEqual(paternity_deadline(date(2027, 10, 31)), date(2028, 2, 29))
        self.child(date(2025, 12, 20));self.child(date(2026, 8, 10));self.child(date(2020, 1, 1))
        for kind, law in ((ContractType.teacher, "Púétv."), (ContractType.employee_under_the_labour_code, "Mt.")):
            self.contracts["employee"].contract_type = kind
            with self.app.test_request_context():
                session["locale"] = "en"
                basis = leave_basis(self.contracts["employee"], 2026, LeaveType.paternity_leave)
            self.assertTrue(basis["reference"].startswith(law));self.assertEqual(len(basis["paragraphs"]), 2)
            self.assertIn("April 30, 2026", basis["paragraphs"][0])
            self.assertIn("December 31, 2026", basis["paragraphs"][1])

    def test_age_threshold_years_and_calendar_import_agree(self):
        for age, days in [(24, 0), (25, 1), (27, 1), (28, 2), (31, 3), (33, 4), (35, 5), (37, 6), (39, 7), (41, 8), (43, 9), (45, 10)]:
            self.assertEqual(age_supplement_days(age), days)
        contract = self.contracts["employee"]
        contract.contract_type = ContractType.employee_under_the_labour_code
        self.users["employee"].profile.date_of_birth = date(2001, 12, 31)
        db.session.commit()
        self.assertEqual(_calculated_calendar_leave_limits(contract, 2026)[LeaveType.supplementary_leave_based_on_age], 1)
        self.assertEqual(_calculated_calendar_leave_limits(contract, 2026)[LeaveType.basic_leave], 20)

    def test_leave_limit_page_has_legal_information_and_translates_new_labels(self):
        self.child(date(2026, 8, 10))
        self.login("hr", "hu")
        response = self.client.get(f'/leave-limits?user_id={self.users["employee"].id}&contract_id={self.contracts["employee"].id}&calendar_year=2026')
        self.assertEqual(response.status_code, 200)
        for text in ("Jogalap és jogosultság", "Púétv. 90. § (11), (13)", "1 figyelembe vehető gyermeke", "2026. december 31."):
            self.assertIn(text, response.text)
        profile = self.client.get('/profile').text
        for text in ("Csoportszintű Unifikált Pihenőnap-kezelő Óvodai Rendszer", "Tartózkodási hely", "Ha nincs, hagyd üresen."):
            self.assertIn(text, profile)
        self.assertNotIn("legfeljebb 64 karakter", profile)
