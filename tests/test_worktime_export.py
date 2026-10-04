"""Monthly register fidelity, spreadsheet safety and printable pagination."""

import csv
from datetime import date
from io import StringIO
import re
import unittest

from app.worktime_export import DRAFT_LABEL, TEACHER_NOTE, export_worktime_csv, export_worktime_pdf


def register_fixture():
    return {
        "employee_name": "Őri-Tűrő Ágnes",
        "year": 2026, "month": 10, "status": "draft", "is_teacher": True,
        "job_title": "Óvodapedagógus",
        "rows": [
            {"date": date(2026, 10, 1), "start": "08:00", "end": "14:44", "worked_minutes": 384,
             "break_minutes": 20, "teaching_minutes": 312, "workplace": "Első óvoda", "group_name": "Süni"},
            {"date": "2026-10-02", "start": None, "end": None, "worked_minutes": 0,
             "absence_label": "Fizetett szabadság", "note": "Csoportösszevonás: Süni → Pillangó"},
            {"date": "2026-10-05", "start": "08:00", "end": "14:44", "worked_minutes": 384,
             "break_minutes": 20, "teaching_minutes": 312},
        ],
    }


def csv_rows(register):
    raw = export_worktime_csv(register)
    return list(csv.reader(StringIO(raw.decode("utf-8-sig")), delimiter=";"))


class WorktimeExportTests(unittest.TestCase):
    def test_csv_has_bom_hungarian_metadata_draft_and_complete_daily_columns(self):
        fixture = register_fixture()
        raw = export_worktime_csv(fixture)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = csv_rows(fixture)
        self.assertIn(["Munkavállaló", "Őri-Tűrő Ágnes"], rows)
        self.assertIn(["Év", "2026", "Hónap", "október"], rows)
        self.assertIn(["Munkakör", "Óvodapedagógus"], rows)
        self.assertIn(["Állapot", DRAFT_LABEL], rows)
        self.assertIn([TEACHER_NOTE], rows)
        daily = next(row for row in rows if row and row[0] == "2026.10.01.")
        self.assertEqual(daily, [
            "2026.10.01.", "csütörtök", "Óvodapedagógus", "Első óvoda", "Süni",
            "08:00", "14:44", "00:20", "06:24", "05:12", "", "",
        ])

    def test_absence_zero_hours_no_times_and_manual_merge_note_are_retained(self):
        fixture = register_fixture()
        # Even a stale start/end on an absence cannot appear as attendance.
        fixture["rows"][1].update(start="08:00", end="16:20")
        row = next(row for row in csv_rows(fixture) if row and row[0] == "2026.10.02.")
        self.assertEqual(row[5:10], ["", "", "00:00", "00:00", "00:00"])
        self.assertEqual(row[10], "Fizetett szabadság; Csoportösszevonás: Süni → Pillangó")
        self.assertEqual(row[11], "")

    def test_partial_weeks_month_totals_and_distinct_contract_rows(self):
        fixture = register_fixture()
        fixture["rows"].insert(1, {
            "date": "2026-10-01", "start": "15:00", "end": "16:00", "worked_minutes": 60,
            "job_title": "Pedagógiai asszisztens", "workplace": "Második óvoda", "group_name": "Katica",
        })
        rows = csv_rows(fixture)
        day_rows = [row for row in rows if row and row[0] == "2026.10.01."]
        self.assertEqual(len(day_rows), 2)
        self.assertEqual(day_rows[1][2:5], ["Pedagógiai asszisztens", "Második óvoda", "Katica"])
        partial = next(row for row in rows if row and row[0] == "2026 / 40. hét összesen")
        self.assertEqual(partial[8:11], ["07:24", "05:12", "10.01. - 10.04. (havi részlet)"])
        full = next(row for row in rows if row and row[0] == "2026 / 41. hét összesen")
        self.assertNotIn("havi részlet", full[10])
        total = next(row for row in rows if row and row[0] == "Havi összesen")
        self.assertEqual(total[7:10], ["00:40", "13:48", "10:24"])

    def test_week_number_uses_iso_year_and_duration_does_not_wrap(self):
        fixture = register_fixture()
        fixture.update(year=2027, month=1, status="confirmed", is_teacher=False, job_title="Dajka")
        fixture["rows"] = [
            {"date": date(2027, 1, day), "worked_minutes": 480, "start": "08:00", "end": "16:20", "break_minutes": 20}
            for day in (1, 4, 5, 6, 7, 8)
        ]
        rows = csv_rows(fixture)
        self.assertIn(["Állapot", "Jóváhagyott beosztás"], rows)
        self.assertNotIn([TEACHER_NOTE], rows)
        self.assertTrue(any(row[0] == "2026 / 53. hét összesen" for row in rows if row))
        total = next(row for row in rows if row and row[0] == "Havi összesen")
        self.assertEqual(total[8:10], ["48:00", ""])

    def test_empty_all_absent_teacher_and_non_teacher_registers(self):
        fixture = register_fixture()
        fixture["rows"] = []
        self.assertIn([TEACHER_NOTE], csv_rows(fixture))
        self.assertEqual(csv_rows(fixture)[-1][8:10], ["00:00", "00:00"])
        fixture["is_teacher"] = False
        fixture["job_title"] = "Óvodatitkár"
        self.assertNotIn([TEACHER_NOTE], csv_rows(fixture))
        self.assertTrue(export_worktime_pdf(fixture).startswith(b"%PDF-"))

    def test_csv_neutralises_formulas_in_all_editable_fields_and_preserves_newlines(self):
        for payload in ("=HYPERLINK(\"https://example.invalid\")", "+cmd", "-1+cmd", "@SUM(A1)", "  =1+1", "\t=1+1", "\r=1+1", "\u00a0=1+1", "\x00=1+1"):
            with self.subTest(payload=payload):
                fixture = register_fixture()
                fixture.update(employee_name=payload, job_title=payload)
                fixture["rows"][0].update(job_title=payload, workplace=payload, group_name=payload, note=payload)
                rows = csv_rows(fixture)
                self.assertIn(["Munkavállaló", "'" + payload], rows)
                self.assertIn(["Munkakör", "'" + payload], rows)
                daily = next(row for row in rows if row and row[0] == "2026.10.01.")
                for index in (2, 3, 4, 10):
                    self.assertEqual(daily[index], "'" + payload)
        fixture["rows"][0]["note"] = 'Első sor; "idézet"\nMásodik sor őű'
        row = next(row for row in csv_rows(fixture) if row and row[0] == "2026.10.01.")
        self.assertEqual(row[10], fixture["rows"][0]["note"])

    def test_invalid_dates_times_and_minute_amounts_are_not_silently_exported(self):
        for change in ({"date": "2026-11-01"}, {"start": "25:00"}, {"end": "8:00"},
                       {"worked_minutes": -1}, {"worked_minutes": 1.5}, {"break_minutes": True},
                       {"teaching_minutes": 999}):
            with self.subTest(change=change):
                fixture = register_fixture()
                fixture["rows"][0].update(change)
                with self.assertRaises(ValueError):
                    export_worktime_csv(fixture)

    def test_export_does_not_mutate_input_and_sorts_workdays(self):
        fixture = register_fixture()
        fixture["rows"].reverse()
        original = [dict(row) for row in fixture["rows"]]
        result = csv_rows(fixture)
        self.assertEqual(fixture["rows"], original)
        days = [row[0] for row in result if row and re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.", row[0])]
        self.assertEqual(days, ["2026.10.01.", "2026.10.02.", "2026.10.05."])

    def test_pdf_embeds_hungarian_fonts_and_paginates_long_notes_without_clipping(self):
        fixture = register_fixture()
        fixture["employee_name"] = "Őri-Tűrő Ágnes & <teszt> " * 6
        fixture["rows"] = [
            {"date": date(2026, 10, day), "start": "08:00", "end": "14:44", "worked_minutes": 384,
             "break_minutes": 20, "teaching_minutes": 312,
             "workplace": "Micimackó telephely őű", "group_name": "Süni és Pillangó",
             "note": "Csoportösszevonás és hosszú megjegyzés. " * (100 if day == 5 else 4)}
            for day in range(1, 32) if date(2026, 10, day).weekday() < 5
        ]
        payload = export_worktime_pdf(fixture)
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertTrue(payload.rstrip().endswith(b"%%EOF"))
        self.assertGreaterEqual(payload.count(b"/FontFile2"), 2)
        self.assertGreaterEqual(payload.count(b"/ToUnicode"), 2)
        self.assertGreater(len(re.findall(rb"/Type\s*/Page\b", payload)), 2)


if __name__ == "__main__":
    unittest.main()
