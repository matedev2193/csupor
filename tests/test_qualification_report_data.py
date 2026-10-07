"""School-year boundaries, honest legacy reporting and distinct-person counts."""

from datetime import date
from types import SimpleNamespace
import unittest

from flask import Flask
from flask_babel import Babel
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import BadRequest

from app.qualification_reports import (
    _parse_filters, _record, _type_options, build_report, school_year_start,
)


class QualificationReportDataTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        Babel(app, locale_selector=lambda: "en")
        self.context = app.app_context()
        self.context.push()
        self.today = date(2026, 10, 7)
        self.next_id = 0
        self.users = [{"id": 1, "name": "Ágnes One", "username": "agnes"},
                      {"id": 2, "name": "Bea Two", "username": "bea"}]

    def tearDown(self):
        self.context.pop()

    def record(self, completed=date(2026, 9, 1), user=1, kind="qualification", **overrides):
        self.next_id += 1
        values = dict(id=self.next_id, user_id=user, level_or_type="Certificate", qualification_name="First aid",
                      institution_name="Example College", degree_number="CERT-123", date_obtained=completed,
                      year_obtained=completed.year if completed else 2026, highest=False)
        values.update(overrides)
        return _record(SimpleNamespace(**values), self.users[user - 1], kind)

    def report(self, records, **changes):
        filters = {"year": "2026", "user_id": "", "kind": "all", "type": "", "q": ""}
        filters.update(changes)
        return build_report(records, filters, self.today)

    def test_school_year_uses_september_boundary_and_full_leap_date(self):
        self.assertEqual(school_year_start(date(2026, 8, 31)), 2025)
        self.assertEqual(school_year_start(date(2026, 9, 1)), 2026)
        self.assertEqual(school_year_start(date(2024, 2, 29)), 2023)
        self.assertIsNone(school_year_start(None))
        records = [self.record(date(2026, 8, 31)), self.record(date(2026, 9, 1)),
                   self.record(date(2024, 2, 29))]
        current = self.report(records)
        self.assertEqual([row["date_obtained"] for row in current["records"]], [date(2026, 9, 1)])
        leap_year = self.report(records, year="2023")
        self.assertEqual(leap_year["totals"]["total"], 1)
        self.assertEqual(next(row["total"] for row in leap_year["by_month"] if row["value"] == "2024-02"), 1)

    def test_months_include_zero_counts_in_september_to_august_order(self):
        report = self.report([self.record(date(2027, 8, 31))])
        self.assertEqual([row["value"] for row in report["by_month"]], [
            "2026-09", "2026-10", "2026-11", "2026-12", "2027-01", "2027-02",
            "2027-03", "2027-04", "2027-05", "2027-06", "2027-07", "2027-08",
        ])
        self.assertEqual([row["total"] for row in report["by_month"]], [0] * 11 + [1])
        self.assertEqual(len(self.report([])["by_month"]), 12)

    def test_unknown_dates_never_invent_year_or_month_and_keep_legacy_year(self):
        undated = self.record(None, year_obtained=2026)
        dated = self.record(date(2026, 9, 1))
        for selected_year in ("2025", "2026"):
            report = self.report([undated], year=selected_year)
            self.assertEqual(report["totals"]["total"], 0)
            self.assertEqual(report["unknown_count"], 1)
            self.assertFalse(report["by_year"])
            self.assertTrue(all(row["total"] == 0 for row in report["by_month"]))
        report = self.report([undated, dated], year="all")
        self.assertEqual(report["totals"]["total"], 2)
        self.assertEqual({row["value"]: row["total"] for row in report["by_year"]}, {"2026": 1, "unknown": 1})
        self.assertEqual(sum(row["total"] for row in report["by_month"]), 1)
        unknown = self.report([undated, dated], year="unknown")
        self.assertEqual(unknown["records"], [undated])
        self.assertEqual(unknown["records"][0]["year_obtained"], 2026)
        self.assertIsNone(unknown["records"][0]["school_year"])
        self.assertFalse(unknown["by_month"])

    def test_each_group_counts_distinct_people_separately_from_completions(self):
        report = self.report([self.record(), self.record(), self.record(user=2), self.record(kind="exam")])
        self.assertEqual(report["totals"], {"employees": 2, "total": 4, "qualifications": 3, "exams": 1})
        self.assertEqual(report["by_month"][0]["employees"], 2)
        self.assertEqual(report["by_month"][0]["total"], 4)
        certificate = next(row for row in report["by_type"] if row["value"] == "qualification:certificate")
        self.assertEqual((certificate["employees"], certificate["total"]), (2, 3))

    def test_whitespace_case_and_unicode_spelling_do_not_split_type_or_name(self):
        records = [self.record(level_or_type="  TAnúsítvány  ", qualification_name="  Elsősegély   oktatás "),
                   self.record(user=2, level_or_type="tanúsítvány", qualification_name="elsősegély oktatás"),
                   self.record(level_or_type="TANÚSÍTVÁNY", qualification_name="ELSO\u030bSEGÉLY OKTATÁS")]
        report = self.report(records)
        self.assertEqual(len(report["by_type"]), 1)
        self.assertEqual(len(report["by_name"]), 1)
        self.assertEqual(report["by_name"][0]["total"], 3)
        reversed_report = self.report(list(reversed(records)))
        self.assertEqual(report["by_type"], reversed_report["by_type"])
        self.assertEqual(report["by_name"], reversed_report["by_name"])

    def test_exam_and_qualification_with_same_name_and_type_stay_separate(self):
        report = self.report([self.record(level_or_type="Professional exam"), self.record(kind="exam")])
        self.assertEqual(len(report["by_type"]), 2)
        self.assertEqual(len(report["by_name"]), 2)
        self.assertEqual({row["value"] for row in report["by_type"]}, {"exam", "qualification:professional exam"})

    def test_employee_kind_type_and_search_filters_combine_and_scope_unknown_notice(self):
        target = self.record(user=2, qualification_name="Paediatric first aid", degree_number="DOC-987")
        records = [target, self.record(), self.record(None), self.record(user=2, kind="exam")]
        report = self.report(records, user_id="2", kind="qualification", type="qualification:certificate", q="PAEDIATRIC")
        self.assertEqual(report["records"], [target])
        self.assertEqual(report["unknown_count"], 0)
        for search in ("Example College", "Bea Two", "bea", "doc-987"):
            filtered = self.report(records, user_id="2", kind="qualification", q=search)
            self.assertEqual(filtered["records"], [target])

    def test_all_years_months_are_chronological_and_have_no_fabricated_empty_months(self):
        records = [self.record(date(2026, 9, 1)), self.record(date(2023, 12, 1)), self.record(None), self.record(date(2024, 2, 29))]
        report = self.report(records, year="all")
        self.assertEqual([row["value"] for row in report["by_month"]], ["2023-12", "2024-02", "2026-09"])
        self.assertEqual([row["value"] for row in report["by_year"]], ["2026", "2023", "unknown"])

    def test_filter_defaults_and_valid_empty_year_are_explicit(self):
        records = [self.record()]
        options = _type_options(records)
        for today, expected in ((date(2026, 8, 31), "2025"), (date(2026, 9, 1), "2026")):
            filters = _parse_filters(MultiDict(), self.users, options, today)
            self.assertEqual(filters["year"], expected)
        filters = _parse_filters(MultiDict({"year": "2024", "user_id": "02", "type": "qualification: CERTIFICATE "}), self.users, options, self.today)
        self.assertEqual(filters["user_id"], "2")
        self.assertEqual(filters["type"], "qualification:certificate")
        report = build_report(records, filters, self.today)
        self.assertEqual(report["totals"]["total"], 0)
        self.assertIn({"value": "2024", "label": "2024/2025"}, report["school_years"])

    def test_invalid_filters_are_rejected_instead_of_silently_changing_report(self):
        options = _type_options([self.record()])
        for args in (
            {"year": "2026/2027"}, {"year": "２０２６"}, {"year": ""}, {"year": "9999"},
            {"year": "1898"}, {"year": "undefined"}, {"user_id": "9999"}, {"user_id": "-1"},
            {"user_id": "1.5"}, {"kind": "all records"}, {"type": "imaginary"}, {"q": "x" * 201},
            {"q": "bad\x00query"}, [("year", "all"), ("year", "2026")], [("q", "first"), ("q", "second")],
        ):
            with self.subTest(args=args), self.assertRaises(BadRequest):
                _parse_filters(MultiDict(args), self.users, options, self.today)


if __name__ == "__main__":
    unittest.main()
