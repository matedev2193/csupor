"""Allocation invariants and adversarial manual edits use synthetic employees."""
from collections import Counter
from copy import deepcopy
import unittest

from app.worktime_engine import build_schedule, validate_schedule


def worker(cid, role, group=None, *, phase=0, hours=40, trainee=False, site=1):
    return {"contract_id": cid, "user_id": cid, "name": f"Employee {cid}", "site_id": site,
            "role": role, "trainee": trainee, "weekly_hours": hours,
            "start_date": "2020-01-01", "end_date": None,
            "assignments": [] if group is None else [{"group_id": group, "start_date": "2020-01-01", "end_date": None, "shift_phase": phase}]}


def fixture(days=None):
    return {"days": days or [f"2026-10-{day:02d}" for day in range(5, 10)],
            "groups": [{"id": 1, "name": "Group A", "site_id": 1}, {"id": 2, "name": "Group B", "site_id": 1}],
            "workers": [worker(1, "teacher", 1), worker(2, "teacher", 1, phase=1, trainee=True),
                        worker(3, "teacher", 2), worker(4, "teacher", 2, phase=1),
                        worker(5, "nursery_assistant", 1), worker(6, "nursery_assistant", 2),
                        worker(7, "teaching_assistant"), worker(8, "secretary"),
                        worker(9, "employee_under_the_labour_code")],
            "absences": [], "merges": [], "history": []}


def codes(result, severity="error"):
    issues = result["issues"] if isinstance(result, dict) else result
    return {issue["code"] for issue in issues if severity is None or issue["severity"] == severity}


class WorktimeEngineTests(unittest.TestCase):
    def assert_feasible(self, payload, result):
        errors = [issue for issue in result["issues"] if issue["severity"] == "error"]
        self.assertEqual(errors, [])
        self.assertEqual(codes(validate_schedule(payload, result["entries"])), set())
        for row in result["entries"]:
            self.assertLessEqual(row["work_minutes"], 480)
            if row["work_minutes"]:
                self.assertEqual(row["end_minute"] - row["start_minute"], row["work_minutes"] + row["break_minutes"])

    def test_regular_week_net_bound_and_teaching_hours_and_daily_breaks(self):
        payload = fixture()
        snapshot = deepcopy(payload)
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        self.assertEqual(payload, snapshot)
        self.assertEqual(len(result["entries"]), 45)
        for person in payload["workers"]:
            rows = [row for row in result["entries"] if row["contract_id"] == person["contract_id"]]
            self.assertEqual(sum(row["work_minutes"] for row in rows), 1920 if person["role"] == "teacher" else 2400)
            self.assertEqual(sum(row["teaching_minutes"] for row in rows), (1560 if person["trainee"] else 1920) if person["role"] == "teacher" else 0)
            self.assertTrue(all(row["break_minutes"] == 20 for row in rows))

    def test_four_and_six_day_weeks_use_actual_working_days(self):
        for count in (4, 6):
            with self.subTest(count=count):
                payload = fixture([f"2026-10-{day:02d}" for day in range(5, 5 + count)])
                result = build_schedule(payload)
                self.assert_feasible(payload, result)
                self.assertEqual(sum(row["work_minutes"] for row in result["entries"] if row["contract_id"] == 1), count * 384)
                self.assertEqual(sum(row["work_minutes"] for row in result["entries"] if row["contract_id"] == 8), count * 480)

    def test_fractional_part_time_minutes_preserve_week_total(self):
        payload = fixture()
        payload["workers"][0]["weekly_hours"] = 39
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        rows = [row for row in result["entries"] if row["contract_id"] == 1]
        self.assertEqual(sum(row["work_minutes"] for row in rows), 1872)
        self.assertLessEqual(max(row["work_minutes"] for row in rows) - min(row["work_minutes"] for row in rows), 1)

    def test_rotating_openers_no_consecutive_repeats_and_weekly_shift_preference(self):
        payload = fixture([f"2026-10-{day:02d}" for day in range(5, 10)] + [f"2026-10-{day:02d}" for day in range(12, 17)])
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        for shift in ("early_teacher", "early_nursery"):
            openers = [row["contract_id"] for row in result["entries"] if row["shift"] == shift]
            self.assertEqual(len(openers), 10)
            self.assertTrue(all(a != b for a, b in zip(openers, openers[1:])))
            counts = Counter(openers)
            self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        self.assertNotIn("shift_preference_changed", codes(result, "warning"))
        first = {row["contract_id"] for row in result["entries"] if row["day"] == "2026-10-05" and row["shift"] == "afternoon" and row["contract_id"] <= 4}
        second = {row["contract_id"] for row in result["entries"] if row["day"] == "2026-10-12" and row["shift"] == "afternoon" and row["contract_id"] <= 4}
        self.assertEqual(first | second, {1, 2, 3, 4})
        self.assertFalse(first & second)

    def test_weekly_toggle_survives_iso_week_53_and_year_boundary(self):
        payload = fixture(["2026-12-28", "2027-01-04"])
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        afternoons = [{r["contract_id"] for r in result["entries"] if r["day"] == day and r["shift"] == "afternoon" and r["contract_id"] <= 4} for day in payload["days"]]
        self.assertFalse(afternoons[0] & afternoons[1])

    def test_history_continues_early_duty_rotation(self):
        payload = fixture(["2026-10-05"])
        first = build_schedule(payload)
        previous_teacher = next(row["contract_id"] for row in first["entries"] if row["shift"] == "early_teacher")
        payload["history"] = [{"day": "2026-10-02", "contract_id": previous_teacher, "shift": "early_teacher"}]
        second = build_schedule(payload)
        self.assertNotEqual(next(row["contract_id"] for row in second["entries"] if row["shift"] == "early_teacher"), previous_teacher)

    def test_absent_teacher_has_zero_hours_and_partner_morning_with_substitute(self):
        payload = fixture()
        payload["absences"] = [{"user_id": 2, "start_date": "2026-10-06", "end_date": "2026-10-06", "label": "Annual leave"}]
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        rows = [row for row in result["entries"] if row["day"] == "2026-10-06"]
        absent = next(row for row in rows if row["contract_id"] == 2)
        self.assertEqual((absent["work_minutes"], absent["teaching_minutes"], absent["start_minute"]), (0, 0, None))
        self.assertEqual(absent["note"], "Annual leave")
        present = next(row for row in rows if row["contract_id"] == 1)
        self.assertLessEqual(present["start_minute"], 480)
        self.assertTrue(any(row["group_id"] == 1 and row["contract_id"] in (5, 7) and row["end_minute"] == 1050 for row in rows))
        self.assertEqual(sum(row["work_minutes"] for row in result["entries"] if row["contract_id"] == 2), 4 * 384)

    def test_shared_assistant_matching_reserves_nurse_for_opening(self):
        payload = fixture(["2026-10-05"])
        payload["absences"] = [{"user_id": cid, "start_date": "2026-10-05", "end_date": "2026-10-05"} for cid in (2, 4)]
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        replacements = [row for row in result["entries"] if row["contract_id"] in (5, 6, 7) and row["end_minute"] == 1050]
        self.assertEqual({row["group_id"] for row in replacements}, {1, 2})
        self.assertEqual(len({row["contract_id"] for row in replacements}), 2)
        self.assertEqual(sum(row["shift"] == "early_nursery" for row in result["entries"]), 1)

    def test_one_assistant_cannot_cover_two_groups_or_double_nurse_open_and_close(self):
        payload = fixture(["2026-10-05"])
        payload["workers"] = [person for person in payload["workers"] if person["contract_id"] != 7]
        payload["absences"] = [{"user_id": cid, "start_date": "2026-10-05", "end_date": "2026-10-05"} for cid in (2, 4)]
        result = build_schedule(payload)
        self.assertIn("allocation_conflict", codes(result))
        self.assertTrue(all(row["work_minutes"] <= 480 for row in result["entries"]))
        self.assertEqual(len({(row["day"], row["contract_id"]) for row in result["entries"]}), len(result["entries"]))

    def test_all_teachers_absent_requires_manual_merge_and_notes_survive_absence(self):
        payload = fixture(["2026-10-05"])
        payload["absences"] = [{"user_id": cid, "start_date": "2026-10-05", "end_date": "2026-10-05"} for cid in (1, 2)]
        self.assertIn("manual_merge_required", codes(build_schedule(payload)))
        payload["merges"] = [{"day": "2026-10-05", "source_group_id": 1, "target_group_id": 2}]
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        for cid in (1, 2, 3, 4, 5, 6):
            row = next(row for row in result["entries"] if row["contract_id"] == cid)
            self.assertIn("Group A → Group B", row["note"])
            self.assertTrue(any(part["message"].startswith("Merged group:") for part in row["note_parts"]))

    def test_merge_cannot_silently_target_another_closed_group(self):
        payload = fixture(["2026-10-05"])
        payload["absences"] = [{"user_id": cid, "start_date": "2026-10-05", "end_date": "2026-10-05"} for cid in (1, 2, 3, 4)]
        payload["merges"] = [{"day": "2026-10-05", "source_group_id": 1, "target_group_id": 2}]
        self.assertIn("manual_merge_required", codes(build_schedule(payload)))
        payload["merges"].append({"day": "2026-10-05", "source_group_id": 2, "target_group_id": 1})
        self.assertIn("chained_merge", codes(build_schedule(payload)))

    def test_short_substitute_cannot_hide_uncovered_afternoon(self):
        payload = fixture(["2026-10-05"])
        payload["groups"] = payload["groups"][:1]
        payload["workers"] = [person for person in payload["workers"] if person["contract_id"] in (1, 2, 5, 7)]
        next(person for person in payload["workers"] if person["contract_id"] == 7)["weekly_hours"] = 10
        payload["absences"] = [{"user_id": 2, "start_date": "2026-10-05", "end_date": "2026-10-05"}]
        self.assertIn("group_coverage_gap", codes(build_schedule(payload)))

    def test_exactly_six_hours_has_no_break_but_longer_day_has_twenty(self):
        payload = fixture(["2026-10-05"])
        secretary = next(person for person in payload["workers"] if person["contract_id"] == 8)
        for hours, expected in ((30, 0), (31, 20)):
            secretary["weekly_hours"] = hours
            result = build_schedule(payload)
            self.assert_feasible(payload, result)
            row = next(row for row in result["entries"] if row["contract_id"] == 8)
            self.assertEqual(row["break_minutes"], expected)

    def test_manual_validation_rejects_absence_work_caps_breaks_and_shift_anchors(self):
        payload = fixture(["2026-10-05"])
        original = build_schedule(payload)["entries"]
        for change, expected in (({"work_minutes": 481, "end_minute": 981}, "daily_limit"),
                                 ({"break_minutes": 0, "break_start": None, "end_minute": 960}, "invalid_break"),
                                 ({"start_minute": 540, "end_minute": 1040, "break_start": 780}, "invalid_shift_anchor")):
            rows = deepcopy(original)
            row = next(row for row in rows if row["contract_id"] == 8)
            row.update(start_minute=480, end_minute=980, break_start=720)
            row.update(change)
            self.assertIn(expected, codes(validate_schedule(payload, rows)))
        payload["absences"] = [{"user_id": 8, "start_date": "2026-10-05", "end_date": "2026-10-05"}]
        self.assertIn("absence_work", codes(validate_schedule(payload, original)))

    def test_manual_break_during_morning_teacher_coverage_is_rejected(self):
        payload = fixture(["2026-10-05"])
        rows = build_schedule(payload)["entries"]
        morning = next(row for row in rows if row["contract_id"] in (1, 2) and row["start_minute"] <= 480)
        morning["break_start"] = 540
        self.assertIn("morning_teacher_coverage", codes(validate_schedule(payload, rows)))

    def test_same_day_overlapping_contracts_include_other_workplaces(self):
        payload = fixture(["2026-10-05"])
        payload["external_contracts"] = [{"contract_id": 99, "user_id": 1, "site_id": 2, "start_date": "2026-10-01", "end_date": None}]
        result = build_schedule(payload)
        self.assertIn("overlapping_contracts", codes(result))
        self.assertFalse(any(row["contract_id"] == 99 for row in result["entries"]))
        self.assertEqual(next(row for row in result["entries"] if row["contract_id"] == 1)["work_minutes"], 0)

    def test_inactive_groups_and_contracts_not_scheduled_or_warned(self):
        payload = fixture(["2026-10-05"])
        payload["groups"].append({"id": 3, "name": "Future group", "site_id": 1, "start_date": "2027-01-01"})
        payload["workers"].append(worker(10, "teacher", 3))
        payload["workers"][-1]["start_date"] = "2027-01-01"
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        self.assertFalse(any(row["contract_id"] == 10 for row in result["entries"]))
        self.assertFalse(any(issue.get("group_id") == 3 for issue in result["issues"]))

    def test_missing_bad_assignment_and_bad_hours_report_no_fake_work(self):
        for change, expected in (({"assignments": []}, "missing_group"), ({"weekly_hours": None}, "invalid_weekly_hours"),
                                 ({"weekly_hours": 41}, "invalid_weekly_hours"), ({"weekly_hours": float("nan")}, "invalid_weekly_hours")):
            payload = fixture(["2026-10-05"])
            payload["workers"][0].update(change)
            result = build_schedule(payload)
            self.assertIn(expected, codes(result))
            self.assertEqual(next(row for row in result["entries"] if row["contract_id"] == 1)["work_minutes"], 0)

    def test_month_subset_validation_does_not_demand_other_month_days(self):
        payload = fixture(["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"])
        result = build_schedule(payload)
        subset = [row for row in result["entries"] if row["day"].startswith("2026-10")]
        self.assertEqual(codes(validate_schedule(payload, subset, restrict_to_entry_days=True)), set())
        self.assertIn("missing_entry", codes(validate_schedule(payload, subset)))

    def test_search_tries_another_matching_after_short_cover_fails(self):
        payload = fixture(["2026-10-05"])
        payload["groups"] = payload["groups"][:1]
        payload["workers"] = [person for person in payload["workers"] if person["contract_id"] in (1, 2, 5)]
        payload["workers"] += [worker(7, "teaching_assistant", hours=10), worker(10, "teaching_assistant")]
        payload["absences"] = [{"user_id": 2, "start_date": "2026-10-05", "end_date": "2026-10-05"}]
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        self.assertEqual(next(row for row in result["entries"] if row["contract_id"] == 10)["group_id"], 1)

    def test_empty_schedule_and_entire_missing_day_still_validate_with_explicit_scope(self):
        payload = fixture(["2026-10-05", "2026-10-06"])
        self.assertIn("missing_entry", codes(validate_schedule(payload, [], validation_days=payload["days"])))
        self.assertIn("missing_entry", codes(validate_schedule(payload, [], restrict_to_entry_days=True)))
        rows = [row for row in build_schedule(payload)["entries"] if row["day"] == "2026-10-05"]
        issues = validate_schedule(payload, rows, validation_days=payload["days"])
        self.assertTrue(any(issue["code"] == "missing_entry" and issue["day"] == "2026-10-06" for issue in issues))

    def test_manual_validation_preserves_shift_and_repeated_opener_warnings(self):
        payload = fixture(["2026-10-05", "2026-10-06"])
        payload["groups"] = payload["groups"][:1]
        payload["workers"] = [person for person in payload["workers"] if person["contract_id"] in (1, 2, 5, 7)]
        result = build_schedule(payload)
        self.assert_feasible(payload, result)
        warning_codes = codes(validate_schedule(payload, result["entries"], validation_days=payload["days"]), "warning")
        self.assertIn("shift_preference_changed", warning_codes)
        self.assertIn("early_rotation_repeated", warning_codes)

    def test_deterministic_output_independent_of_input_worker_order(self):
        payload = fixture()
        first = build_schedule(payload)
        payload["workers"].reverse()
        second = build_schedule(payload)
        self.assertEqual(first["entries"], second["entries"])
        self.assertEqual(codes(first, None), codes(second, None))


if __name__ == "__main__":
    unittest.main()
