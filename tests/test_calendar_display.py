"""Calendar category markers stay short without losing their accessible meaning."""

import os
import re
import unittest
from datetime import date, timedelta
from html import unescape
from unittest.mock import patch

from flask import g

from app import create_app, db
from app.models import (
    Contract, ContractType, LegalEntity, LeaveRequest, LeaveRequestCategory,
    LeaveRequestStatus, PlaceOfWork, User, UserPrivilege,
)


class CalendarDisplayTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "SECRET_KEY": "calendar-test-only"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.month = date.today().replace(day=1)
        user = User(username="calendar", email="calendar@example.invalid", password_hash="unused",
                    privilege=UserPrivilege.employee)
        entity = LegalEntity(name="Nursery", address="Example", om_id="123456", tax_number="12345678901")
        place = PlaceOfWork(legal_entity=entity, address="Example")
        contract = Contract(user=user, employer=entity, place_of_work=place,
                            contract_type=ContractType.teacher, start_date=self.month - timedelta(days=365),
                            job_title="Teacher", working_hours_per_week=40)
        db.session.add(contract)
        for index, category in enumerate(LeaveRequestCategory, start=1):
            db.session.add(LeaveRequest(user=user, contract=contract, category=category,
                                        start_date=self.month.replace(day=index), status=LeaveRequestStatus.approved))
        for index, status in enumerate((LeaveRequestStatus.rejected, LeaveRequestStatus.cancelled), start=7):
            db.session.add(LeaveRequest(user=user, contract=contract, category=LeaveRequestCategory.paid_leave,
                                        start_date=self.month.replace(day=index), status=status))
        db.session.commit()
        self.user_id, self.contract_id = user.id, contract.id

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def test_all_categories_have_short_chips_full_names_and_visible_key(self):
        for locale, abbreviations in (
            ("en", ["PL", "HL", "CSB", "ML", "EX", "UL"]),
            ("hu", ["FSZ", "EÜ", "GYÁP", "SZ", "MAM", "FNSZ"]),
        ):
            with self.subTest(locale=locale):
                with self.client.session_transaction() as session:
                    session.update(_user_id=str(self.user_id), _fresh=True, locale=locale)
                for key in ("_login_user", "_flask_babel", "leave_approval_policy"):
                    g.pop(key, None)
                response = self.client.get(
                    f"/leaves?contract_id={self.contract_id}&year={self.month.year}&month={self.month.month}"
                )
                self.assertEqual(response.status_code, 200)
                chips = re.findall(r'<span class="leave-chip\b([^>]*)>(.*?)</span>', response.text, re.S)
                self.assertEqual([unescape(text.strip()) for _, text in chips], abbreviations)
                self.assertEqual(len(chips), len(LeaveRequestCategory))
                for index, (attributes, short_text) in enumerate(chips, start=1):
                    title = unescape(re.search(r'title="([^"]+)"', attributes).group(1))
                    accessible_name = unescape(re.search(r'aria-label="([^"]+)"', attributes).group(1))
                    self.assertEqual(title, accessible_name)
                    full_category = title.split(" · ")[0]
                    self.assertGreater(len(full_category), len(short_text.strip()))
                    day_button = re.search(
                        rf'data-calendar-date="{self.month.replace(day=index).isoformat()}"\s+aria-label="([^"]+)"',
                        response.text,
                    )
                    self.assertIn(full_category, unescape(day_button.group(1)))
                    self.assertIn(f"<dt>{short_text.strip()}</dt><dd>{full_category}</dd>", response.text)
                self.assertIn('aria-labelledby="calendar-abbreviations-heading"', response.text)


if __name__ == "__main__":
    unittest.main()
