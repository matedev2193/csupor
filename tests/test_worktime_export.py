"""Monthly register fidelity, spreadsheet safety and one-page portrait output."""

import csv
from datetime import date
from io import StringIO
import re
import unittest
from unittest.mock import patch

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
            "2026.10.01.", "csütörtök",
            "08:00", "14:44", "00:20", "06:24", "05:12", "[B1]", "",
        ])

    def test_absence_zero_hours_no_times_and_manual_merge_note_are_retained(self):
        fixture = register_fixture()
        # Even a stale start/end on an absence cannot appear as attendance.
        fixture["rows"][1].update(start="08:00", end="16:20")
        row = next(row for row in csv_rows(fixture) if row and row[0] == "2026.10.02.")
        self.assertEqual(row[2:7], ["", "", "00:00", "00:00", "00:00"])
        self.assertEqual(row[7], "[B2] Fizetett szabadság; Csoportösszevonás: Süni → Pillangó")
        self.assertEqual(row[8], "")

    def test_partial_weeks_month_totals_and_distinct_contract_rows(self):
        fixture = register_fixture()
        fixture["rows"].insert(1, {
            "date": "2026-10-01", "start": "15:00", "end": "16:00", "worked_minutes": 60,
            "job_title": "Pedagógiai asszisztens", "workplace": "Második óvoda", "group_name": "Katica",
        })
        rows = csv_rows(fixture)
        day_rows = [row for row in rows if row and row[0] == "2026.10.01."]
        self.assertEqual(len(day_rows), 2)
        self.assertEqual(day_rows[1][7], "[B2]")
        assignment = next(row for row in rows if row and row[0] == "B2")
        self.assertEqual(assignment[1:4], ["Pedagógiai asszisztens", "Második óvoda", "Katica"])
        self.assertIn("2026.10.01. 15:00 - 16:00", assignment[5])
        partial = next(row for row in rows if row and row[0] == "2026 / 40. hét összesen")
        self.assertEqual(partial[5:8], ["07:24", "05:12", "10.01. - 10.04. (havi részlet)"])
        full = next(row for row in rows if row and row[0] == "2026 / 41. hét összesen")
        self.assertNotIn("havi részlet", full[7])
        total = next(row for row in rows if row and row[0] == "Havi összesen")
        self.assertEqual(total[4:7], ["00:40", "13:48", "10:24"])

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
        self.assertEqual(total[5:7], ["48:00", ""])

    def test_empty_all_absent_teacher_and_non_teacher_registers(self):
        fixture = register_fixture()
        fixture["rows"] = []
        self.assertIn([TEACHER_NOTE], csv_rows(fixture))
        self.assertEqual(csv_rows(fixture)[-1][5:7], ["00:00", "00:00"])
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
                assignment = next(row for row in rows if row and row[0] == "B1")
                for index in (1, 2, 3):
                    self.assertEqual(assignment[index], "'" + payload)
                self.assertEqual(daily[7], "[B1] " + payload)
        fixture["rows"][0]["note"] = 'Első sor; "idézet"\nMásodik sor őű'
        row = next(row for row in csv_rows(fixture) if row and row[0] == "2026.10.01.")
        self.assertEqual(row[7], "[B1] " + fixture["rows"][0]["note"])

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

    def test_daily_columns_do_not_repeat_header_context(self):
        fixture = register_fixture()
        for row in fixture["rows"]:
            row.update(workplace="Első óvoda", group_name="Süni")
        rows = csv_rows(fixture)
        header = next(row for row in rows if row and row[0] == "Dátum")
        self.assertNotIn("Munkakör", header)
        self.assertNotIn("Munkavégzési hely", header)
        self.assertNotIn("Csoport", header)
        self.assertIn(["Munkavégzési hely", "Első óvoda"], rows)
        self.assertIn(["Csoport", "Süni"], rows)
        self.assertFalse(any(row and row[0] == "B1" for row in rows))
        daily = next(row for row in rows if row and row[0] == "2026.10.01.")
        self.assertEqual(daily[7], "")

    def _render_pdf(self, fixture):
        from reportlab.pdfgen.canvas import Canvas
        calls = []

        class ObservedCanvas(Canvas):
            def drawString(self, x, y, text, *args, **kwargs):
                calls.append((x, y, text, self._fontsize))
                return super().drawString(x, y, text, *args, **kwargs)

            def drawCentredString(self, x, y, text, *args, **kwargs):
                calls.append((x, y, text, self._fontsize))
                return super().drawCentredString(x, y, text, *args, **kwargs)

        with patch("reportlab.pdfgen.canvas.Canvas", ObservedCanvas):
            payload = export_worktime_pdf(fixture)
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertTrue(payload.rstrip().endswith(b"%%EOF"))
        self.assertGreaterEqual(payload.count(b"/FontFile2"), 2)
        self.assertGreaterEqual(payload.count(b"/ToUnicode"), 2)
        self.assertEqual(len(re.findall(rb"/Type\s*/Page\b", payload)), 1)
        box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([0-9.]+)\s+([0-9.]+)\s*\]", payload)
        self.assertIsNotNone(box)
        self.assertAlmostEqual(float(box[1]), 595.276, delta=0.01)
        self.assertAlmostEqual(float(box[2]), 841.89, delta=0.01)
        self.assertTrue(all(24 <= x <= 571 and 20 <= y < 825 for x, y, _, _ in calls))
        self.assertTrue(all(size >= 6.2 for _, _, _, size in calls))
        return payload, calls

    def test_pdf_is_single_portrait_a4_page_for_31_dates_six_weeks_and_long_text(self):
        fixture = register_fixture()
        fixture.update(year=2026, month=8, employee_name="Őri-Tűrő Ágnes & <teszt> " * 6)
        fixture["rows"] = [
            {"date": date(2026, 8, day), "start": "08:00", "end": "14:44", "worked_minutes": 384,
             "break_minutes": 20, "teaching_minutes": 312, "contract_id": day % 5,
             "workplace": "Hosszú munkavégzési hely őű " * 20, "group_name": f"Csoport {day % 5}",
             "note": "Csoportösszevonás és hosszú megjegyzés. " * (100 if day == 5 else day)}
            for day in range(1, 32)
        ]
        fixture["rows"][4].update(worked_minutes=0, teaching_minutes=0, break_minutes=0,
                                  absence_label="Gyermekápolási táppénz")
        _, calls = self._render_pdf(fixture)
        rendered = "\n".join(item[2] for item in calls)
        self.assertIn("Távollét; Összevonás", rendered)
        self.assertIn("T1: Gyermekápolási táppénz", rendered)
        self.assertIn("rövidített szöveg", rendered)
        self.assertIn("CSV", rendered)
        self.assertIn("Havi összesen", rendered)
        self.assertEqual(sum(bool(re.fullmatch(r"\d{2}\. (H|K|Sze|Cs|P|Szo|V)", text)) for _, _, text, _ in calls), 31)
        self.assertEqual(sum(". hét" in text for _, _, text, _ in calls), 6)
        day_positions = [y for _, y, text, _ in calls if re.fullmatch(r"\d{2}\. (H|K|Sze|Cs|P|Szo|V)", text)]
        self.assertGreaterEqual(min(a - b for a, b in zip(day_positions, day_positions[1:])), 14)
        csv_text = export_worktime_csv(fixture).decode("utf-8-sig")
        self.assertIn(fixture["employee_name"], csv_text)
        self.assertIn(fixture["rows"][4]["note"], csv_text)
        self.assertIn("Gyermekápolási táppénz", csv_text)

    def test_typical_pdf_uses_plain_context_header_and_readable_signature_rows(self):
        fixture = register_fixture()
        fixture["rows"] = [
            {"date": date(2026, 10, day), "start": "08:00", "end": "14:44", "worked_minutes": 384,
             "break_minutes": 20, "teaching_minutes": 312, "workplace": "Első óvoda", "group_name": "Süni"}
            for day in range(1, 32) if date(2026, 10, day).weekday() < 5
        ]
        _, calls = self._render_pdf(fixture)
        texts = [item[2] for item in calls]
        self.assertEqual(texts.count("Első óvoda"), 1)
        self.assertEqual(texts.count("Süni"), 1)
        self.assertEqual(texts.count("Óvodapedagógus"), 1)
        self.assertFalse(any("B1" in text for text in texts))
        self.assertFalse(any("rövidített" in text for text in texts))
        self.assertIn("Aláírás", texts)
        self.assertIn(DRAFT_LABEL, texts)

    def test_multiple_intervals_have_one_signature_row_and_explicit_interval_warning(self):
        fixture = register_fixture()
        fixture["rows"] = [
            {"date": "2026-10-01", "start": "08:00", "end": "10:00", "worked_minutes": 120,
             "contract_id": 1, "job_title": "Dajka", "workplace": "A hely", "group_name": "Süni"},
            {"date": "2026-10-01", "start": "13:00", "end": "15:00", "worked_minutes": 120,
             "contract_id": 2, "job_title": "Pedagógiai asszisztens", "workplace": "B hely", "group_name": "Katica"},
        ]
        fixture.update(is_teacher=False, job_title="Dajka; Pedagógiai asszisztens")
        _, calls = self._render_pdf(fixture)
        texts = [item[2] for item in calls]
        self.assertEqual(texts.count("01. Cs"), 1)
        self.assertIn("08:00", texts)
        self.assertIn("15:00", texts)
        self.assertIn("04:00", texts)
        self.assertTrue(any("Több idősáv" in text for text in texts))
        self.assertTrue(any("B1 (01. nap)" in text for text in texts))
        self.assertTrue(any("B2 (01. nap)" in text for text in texts))
        rows = csv_rows(fixture)
        day_rows = [row for row in rows if row and row[0] == "2026.10.01."]
        self.assertEqual([row[2:4] for row in day_rows], [["08:00", "10:00"], ["13:00", "15:00"]])


if __name__ == "__main__":
    unittest.main()
