"""Route regressions for year openings and date-valid paid leave balances."""

import os
import unittest
from datetime import date
from unittest.mock import patch

from app import create_app, db
from app.models import (
    Contract, ContractLeaveLimit, ContractType, LegalEntity, LeaveRequest,
    LeaveRequestCategory, LeaveRequestStatus, LeaveType, LeaveYear, PlaceOfWork,
    User, UserPrivilege,
)
from app.routes import _paid_leave_remaining_by_limit, _paid_leave_used_days


class LeaveYearBoundaryTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "SECRET_KEY": "year-boundary-test-only"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.user = User(
            username="employee", email="employee@example.invalid",
            password_hash="unused", privilege=UserPrivilege.employee,
        )
        entity = LegalEntity(name="Example", address="Example", om_id="123456", tax_number="12345678901")
        place = PlaceOfWork(legal_entity=entity, address="Example")
        self.contract = Contract(
            user=self.user, employer=entity, place_of_work=place,
            contract_type=ContractType.teacher, start_date=date(2020, 1, 1),
            job_title="Teacher", working_hours_per_week=40,
        )
        db.session.add_all([self.user, self.contract, LeaveYear(year=2026, is_open=True)])
        db.session.commit()
        with self.client.session_transaction() as session:
            session["_user_id"] = self.user.get_id()
            session["_fresh"] = True
            session["locale"] = "en"

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def limit(self, year, days, **kwargs):
        values = dict(
            contract=self.contract, calendar_year=year,
            leave_type=LeaveType.basic_leave, limit_days=days,
        )
        values.update(kwargs)
        record = ContractLeaveLimit(**values)
        db.session.add(record)
        db.session.commit()
        return record

    def open_year(self, year):
        db.session.add(LeaveYear(year=year, is_open=True))
        db.session.commit()

    def submit(self, start="2026-12-30", end="2027-01-05", category=LeaveRequestCategory.paid_leave):
        response = self.client.post("/leaves", data={
            "contract_id": self.contract.id,
            "category": category.value, "start_date": start, "end_date": end,
        })
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            return session.get("_flashes", [])[-1]

    def existing(self, start, end=None, status=LeaveRequestStatus.approved):
        record = LeaveRequest(
            user=self.user, contract=self.contract, category=LeaveRequestCategory.paid_leave,
            start_date=start, end_date=end or start, status=status,
        )
        db.session.add(record)
        db.session.commit()
        return record

    def test_unopened_second_year_is_named_before_balance_validation_without_writes(self):
        limit = self.limit(2026, 44)
        category, message = self.submit()
        self.assertEqual(category, "error")
        self.assertIn("not open for leave requests: 2027", message)
        self.assertNotIn("remain", message)
        self.assertEqual(LeaveRequest.query.count(), 0)
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2026), {limit.id: 44})

    def test_explicitly_closed_year_is_rejected_for_every_request_category(self):
        self.limit(2026, 44)
        self.limit(2027, 44)
        db.session.add(LeaveYear(year=2027, is_open=False))
        db.session.commit()
        for category in (LeaveRequestCategory.paid_leave, LeaveRequestCategory.health_leave):
            with self.subTest(category=category):
                severity, message = self.submit(category=category)
                self.assertEqual(severity, "error")
                self.assertIn("not open for leave requests: 2027", message)
                self.assertEqual(LeaveRequest.query.count(), 0)

    def test_closed_middle_year_is_rejected_even_when_both_endpoint_years_are_open(self):
        self.open_year(2028)
        category, message = self.submit(end="2028-01-03")
        self.assertEqual(category, "error")
        self.assertIn("not open for leave requests: 2027", message)
        self.assertEqual(LeaveRequest.query.count(), 0)

    def test_all_unopened_years_are_listed(self):
        category, message = self.submit(end="2028-01-03")
        self.assertEqual(category, "error")
        self.assertIn("not open for leave requests: 2027, 2028", message)
        self.assertEqual(LeaveRequest.query.count(), 0)

    def test_open_years_use_each_annual_balance_and_preserve_single_approval_request(self):
        self.open_year(2027)
        first = self.limit(2026, 3)
        second = self.limit(2027, 5)
        category, _ = self.submit()
        self.assertEqual(category, "success")
        request = LeaveRequest.query.one()
        self.assertEqual(request.start_date, date(2026, 12, 30))
        self.assertEqual(request.end_date, date(2027, 1, 5))
        self.assertEqual(request.status, LeaveRequestStatus.pending_approval)
        self.assertEqual(_paid_leave_used_days(self.contract, 2026), 2)
        self.assertEqual(_paid_leave_used_days(self.contract, 2027), 2)
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2026), {first.id: 1})
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2027), {second.id: 3})

    def test_second_year_shortage_cannot_spend_first_year_balance(self):
        self.open_year(2027)
        first = self.limit(2026, 44)
        second = self.limit(2027, 1)
        category, message = self.submit()
        self.assertEqual(category, "error")
        self.assertEqual(message, "Paid leave requested for 2027 needs 2 days, but only 1 days remain for that year.")
        self.assertEqual(LeaveRequest.query.count(), 0)
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2026), {first.id: 44})
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2027), {second.id: 1})

    def test_first_year_shortage_cannot_borrow_next_year_balance(self):
        self.open_year(2027)
        self.limit(2026, 1)
        self.limit(2027, 44)
        category, message = self.submit()
        self.assertEqual(category, "error")
        self.assertIn("for 2026 needs 2 days, but only 1 days remain", message)
        self.assertEqual(LeaveRequest.query.count(), 0)

    def test_validity_mismatch_does_not_claim_a_numeric_shortage(self):
        self.limit(2026, 44, period_start=date(2026, 1, 1), period_end=date(2026, 12, 29))
        category, message = self.submit(end="2026-12-31")
        self.assertEqual(category, "error")
        self.assertIn("valid on the requested dates in 2026", message)
        self.assertIn("validity periods", message)
        self.assertNotIn("only 44", message)
        self.assertEqual(LeaveRequest.query.count(), 0)

    def test_same_year_success_and_insufficient_balance_still_work(self):
        limit = self.limit(2026, 2)
        category, _ = self.submit(end="2026-12-31")
        self.assertEqual(category, "success")
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2026), {limit.id: 0})
        category, message = self.submit(start="2026-12-29", end="2026-12-29")
        self.assertEqual(category, "error")
        self.assertIn("for 2026 needs 1 days, but only 0 days remain", message)
        self.assertEqual(LeaveRequest.query.count(), 1)

    def test_cross_year_custom_limit_is_not_reset_between_years(self):
        self.open_year(2027)
        shared = self.limit(
            2026, 3, leave_type=LeaveType.parental_leave,
            period_start=date(2026, 1, 1), period_end=date(2027, 12, 31),
        )
        category, message = self.submit()
        self.assertEqual(category, "error")
        self.assertIn("for 2027 needs 2 days, but only 1 days remain", message)
        self.assertEqual(LeaveRequest.query.count(), 0)
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2027), {shared.id: 3})

    def test_shared_limit_accounts_for_existing_requests_in_other_years(self):
        self.open_year(2027)
        shared = self.limit(
            2026, 2, leave_type=LeaveType.parental_leave,
            period_start=date(2026, 1, 1), period_end=date(2027, 12, 31),
        )
        self.existing(date(2026, 12, 29))
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2027), {shared.id: 1})
        category, message = self.submit(start="2027-01-04", end="2027-01-05")
        self.assertEqual(category, "error")
        self.assertIn("for 2027 needs 2 days, but only 1 days remain", message)
        self.assertEqual(LeaveRequest.query.count(), 1)
        self.assertEqual(self.submit(start="2027-01-04", end="2027-01-04")[0], "success")
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2026), {shared.id: 0})
        self.assertEqual(_paid_leave_remaining_by_limit(self.contract, 2027), {shared.id: 0})

    def test_overlap_and_cancelled_requests_keep_existing_behaviour(self):
        self.open_year(2027)
        self.limit(2026, 2)
        self.limit(2027, 2)
        existing = self.existing(date(2026, 12, 30), date(2027, 1, 5))
        category, message = self.submit()
        self.assertEqual(category, "error")
        self.assertIn("overlaps with an existing", message)
        self.assertEqual(LeaveRequest.query.count(), 1)
        existing.status = LeaveRequestStatus.cancelled
        db.session.commit()
        self.assertEqual(self.submit()[0], "success")
        self.assertEqual(LeaveRequest.query.count(), 2)


if __name__ == "__main__":
    unittest.main()
