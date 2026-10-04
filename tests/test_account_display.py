"""Account labels follow current contracts without leaking future positions."""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from app.account_display import account_display


class AccountDisplayTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 10, 4)
        self.user = SimpleNamespace(
            username="sample", profile=SimpleNamespace(full_name="Minta Máté"),
            privilege="employee", contracts=[],
        )
        label_patch = patch("app.account_display.enum_label", return_value="Employee")
        label_patch.start()
        self.addCleanup(label_patch.stop)

    def contract(self, identifier, start, end, title):
        return SimpleNamespace(id=identifier, start_date=start, end_date=end, job_title=title)

    def test_latest_active_contract_wins_including_its_first_and_last_day(self):
        self.user.contracts = [
            self.contract(1, date(2025, 1, 1), None, "Previous position"),
            self.contract(2, self.today, self.today, "Current position"),
            self.contract(3, date(2026, 10, 5), None, "Future position"),
            self.contract(4, date(2026, 9, 1), date(2026, 10, 3), "Ended position"),
        ]
        result = account_display(self.user, self.today)
        self.assertEqual(result["job_title"], "Current position")
        self.assertEqual(result["name"], "Minta Máté")
        self.assertEqual(result["initial"], "M")

    def test_same_start_date_uses_newest_contract_id(self):
        self.user.contracts = [
            self.contract(8, self.today, None, "Newest position"),
            self.contract(5, self.today, None, "Older position"),
        ]
        self.assertEqual(account_display(self.user, self.today)["job_title"], "Newest position")

    def test_missing_or_blank_name_and_inactive_contracts_use_account_fallbacks(self):
        self.user.contracts = [self.contract(1, date(2025, 1, 1), date(2026, 10, 3), "Ended position")]
        for profile in (None, SimpleNamespace(full_name=" "), SimpleNamespace(full_name=None)):
            self.user.profile = profile
            result = account_display(self.user, self.today)
            self.assertEqual(result, {"name": "sample", "initial": "S", "job_title": "Employee"})

    def test_default_date_comes_from_budapest_calendar_helper(self):
        self.user.contracts = [self.contract(1, self.today, self.today, "Today only")]
        with patch("app.account_display.local_today", return_value=self.today) as local_today:
            self.assertEqual(account_display(self.user)["job_title"], "Today only")
        local_today.assert_called_once_with()
