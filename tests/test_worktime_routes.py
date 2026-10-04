"""Scheduling authorisation, dated setup, freshness and atomic monthly changes."""

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from flask import g

from app import create_app, db
from app.models import (
    Contract, ContractType, LeaveRequest, LeaveRequestCategory, LeaveRequestStatus,
    LegalEntity, PlaceOfWork, TeacherClassification, User, UserPrivilege, UserProfile, WorkingDayOverride,
)
from app.worktime_models import WorkAssignment, WorkGroup, WorkGroupMerge, WorkSchedule, WorkTimeEntry
from app.worktime_service import build_payload, export_register, settings_revision


class WorktimeRoutesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///" + self.directory.name + "/worktime.db", "SECRET_KEY": "worktime-tests"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for name in ("hr", "ceo", "developer", "teacher1", "teacher2", "teacher3", "teacher4", "nanny1", "nanny2", "assistant"):
            user = User(username=name, email=f"{name}@example.invalid", password_hash="unused", privilege=UserPrivilege(name) if name in {"hr", "ceo", "developer"} else UserPrivilege.employee)
            user.profile = UserProfile(full_name=f"Minta {name}")
            db.session.add(user)
            self.users[name] = user
        entity = LegalEntity(name="Test nursery", address="Address", om_id="123456", tax_number="12345678901")
        self.site = PlaceOfWork(legal_entity=entity, address="Main workplace")
        self.other_site = PlaceOfWork(legal_entity=entity, address="Other workplace")
        db.session.add_all([entity, self.site, self.other_site])
        db.session.flush()
        self.groups = [WorkGroup(place_of_work_id=self.site.id, name=name, start_date=date(2025, 1, 1)) for name in ("Group A", "Group B")]
        db.session.add_all(self.groups)
        db.session.flush()
        self.contracts = {}
        for name in ("teacher1", "teacher2", "teacher3", "teacher4", "nanny1", "nanny2", "assistant"):
            role = ContractType.teacher if name.startswith("teacher") else (ContractType.nursery_assistant if name.startswith("nanny") else ContractType.teaching_assistant)
            contract = Contract(user=self.users[name], contract_type=role, start_date=date(2025, 1, 1), job_title=name, working_hours_per_week=40, employer=entity, place_of_work=self.site, teacher_classification=TeacherClassification.trainee if name == "teacher4" else (TeacherClassification.teacher_i if role == ContractType.teacher else None))
            db.session.add(contract)
            db.session.flush()
            self.contracts[name] = contract
            if name != "assistant":
                group = self.groups[0 if name in {"teacher1", "teacher2", "nanny1"} else 1]
                db.session.add(WorkAssignment(contract=contract, group=group, start_date=date(2025, 1, 1), shift_phase=1 if name in {"teacher2", "teacher4"} else 0))
        db.session.commit()
        self.login("hr")

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.directory.cleanup()

    def login(self, name):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.users[name].id)
            session["_fresh"] = True
            session["locale"] = "en"
            session["worktime_csrf_token"] = "test-worktime-csrf"
        g.pop("_login_user", None)

    def schedule(self):
        db.session.expire_all()
        return WorkSchedule.query.filter_by(place_of_work_id=self.site.id, year=2026, month=2).first()

    def post(self, path, **extra):
        schedule = self.schedule()
        data = {"csrf_token": "test-worktime-csrf", "year": "2026", "month": "2", "place_id": str(self.site.id), "revision": schedule.revision if schedule else ""}
        data.update(extra)
        return self.client.post(path, data=data, headers={"Accept": "application/json"})

    def generate(self, **extra):
        result = self.post("/worktime/generate", **extra)
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        return self.schedule()

    def setup_post(self, **extra):
        return self.post("/worktime/groups", settings_revision=settings_revision(self.site.id), **extra)

    def leave(self, name, start, end=None, status=LeaveRequestStatus.approved):
        leave = LeaveRequest(user=self.users[name], contract=self.contracts[name], category=LeaveRequestCategory.paid_leave, start_date=start, end_date=end or start, status=status)
        db.session.add(leave)
        db.session.commit()
        return leave

    def test_get_is_read_only_and_staff_only_see_own_register(self):
        self.assertEqual(self.client.get("/worktime?year=2026&month=2").status_code, 200)
        self.assertEqual(WorkSchedule.query.count(), 0)
        schedule = self.generate()
        self.assertTrue(schedule.entries)
        self.login("teacher1")
        own = self.client.get(f"/worktime?year=2026&month=2&place_id={self.site.id}")
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.headers["Cache-Control"], "private, no-store")
        self.assertNotIn(b"Minta teacher2", own.data)
        self.assertEqual(self.client.get(f"/worktime?year=2026&month=2&user_id={self.users['teacher2'].id}").status_code, 403)
        self.assertEqual(self.client.get(f"/worktime?year=2026&month=2&place_id={self.other_site.id}").status_code, 403)
        self.assertEqual(self.client.get("/worktime/groups").status_code, 403)
        self.assertEqual(self.post("/worktime/generate").status_code, 403)
        self.login("developer")
        self.assertEqual(self.post("/worktime/generate").status_code, 403)
        self.assertEqual(self.client.get(f"/worktime/export/{self.users['teacher1'].id}?year=2026&month=2").status_code, 403)
        self.login("ceo")
        self.assertEqual(self.client.get("/worktime/groups").status_code, 200)

    def test_csrf_and_month_validation_never_write(self):
        for values in ({"csrf_token": "wrong"}, {"year": "1969"}, {"year": "2101"}, {"month": "13"}, {"month": "2.5"}):
            with self.subTest(values=values):
                self.assertEqual(self.post("/worktime/generate", **values).status_code, 400)
        self.assertEqual(WorkSchedule.query.count(), 0)
        self.assertEqual(self.client.get("/worktime?year=bad&month=2").status_code, 400)

    def test_full_boundary_weeks_and_working_day_override_drive_generation(self):
        db.session.add(WorkingDayOverride(day=date(2026, 2, 14), is_working_day=True))
        db.session.add(WorkingDayOverride(day=date(2026, 2, 16), is_working_day=False))
        db.session.commit()
        payload, _ = build_payload(self.site.id, 2026, 2)
        self.assertIn("2026-01-26", payload["days"])
        self.assertIn("2026-02-14", payload["days"])
        self.assertNotIn("2026-02-16", payload["days"])
        schedule = self.generate()
        self.assertTrue(all(entry.day.month == 2 for entry in schedule.entries))
        rows = [entry for entry in schedule.entries if entry.user_id == self.users["teacher1"].id]
        self.assertEqual(sum(entry.work_minutes for entry in rows), len(rows) * 384)
        trainee_rows = [entry for entry in schedule.entries if entry.user_id == self.users["teacher4"].id]
        self.assertEqual(sum(entry.teaching_minutes for entry in trainee_rows), len(trainee_rows) * 312)
        self.assertTrue(all(entry.work_minutes <= 480 and entry.break_minutes == 20 for entry in schedule.entries))

    def test_absences_pending_cancellation_are_zero_but_pending_approval_is_not(self):
        self.leave("teacher1", date(2026, 2, 3))
        self.leave("teacher2", date(2026, 2, 4), status=LeaveRequestStatus.pending_cancellation)
        self.leave("teacher3", date(2026, 2, 5), status=LeaveRequestStatus.pending_approval)
        schedule = self.generate()
        for name, day in (("teacher1", 3), ("teacher2", 4)):
            row = next(entry for entry in schedule.entries if entry.user_id == self.users[name].id and entry.day.day == day)
            self.assertEqual((row.work_minutes, row.teaching_minutes, row.break_minutes), (0, 0, 0))
            self.assertIsNone(row.start_minute)
        row = next(entry for entry in schedule.entries if entry.user_id == self.users["teacher3"].id and entry.day.day == 5)
        self.assertEqual(row.work_minutes, 384)

    def test_regeneration_revision_and_manual_changes_require_explicit_acknowledgement(self):
        schedule = self.generate()
        previous_revision = schedule.revision
        row = next(entry for entry in schedule.entries if entry.user_id == self.users["teacher1"].id)
        edit = self.post(f"/worktime/entries/{row.id}", start=f"{row.start_minute // 60:02}:{row.start_minute % 60:02}", end=f"{row.end_minute // 60:02}:{row.end_minute % 60:02}", break_start=f"{row.break_start // 60:02}:{row.break_start % 60:02}", teaching_minutes="384", note="Manual note")
        self.assertEqual(edit.status_code, 200, edit.json)
        self.assertNotEqual(self.schedule().revision, previous_revision)
        self.assertEqual(self.post("/worktime/generate", revision=previous_revision, discard_manual="1").status_code, 409)
        self.assertEqual(self.post("/worktime/generate").status_code, 409)
        self.assertTrue(any(entry.is_manual for entry in self.schedule().entries))
        self.generate(discard_manual="1")
        self.assertFalse(any(entry.is_manual for entry in self.schedule().entries))

    def test_manual_daily_cap_break_and_absence_cannot_be_bypassed(self):
        self.leave("teacher1", date(2026, 2, 3))
        schedule = self.generate()
        absent = next(entry for entry in schedule.entries if entry.user_id == self.users["teacher1"].id and entry.day.day == 3)
        self.assertEqual(self.post(f"/worktime/entries/{absent.id}", start="08:00", end="14:00", teaching_minutes="360").status_code, 400)
        row = next(entry for entry in schedule.entries if entry.user_id == self.users["teacher1"].id and entry.day.day == 4)
        for values in ({"start": "06:00", "end": "17:30", "break_start": "12:00"}, {"start": "08:00", "end": "14:44"}, {"start": "08:00", "end": "14:01", "break_start": "12:00"}):
            self.assertEqual(self.post(f"/worktime/entries/{row.id}", teaching_minutes="0", **values).status_code, 400)
        result = self.post(f"/worktime/entries/{row.id}", start="08:00", end="14:00", teaching_minutes="360")
        self.assertEqual(result.status_code, 200)
        db.session.refresh(row)
        self.assertEqual((row.work_minutes, row.break_minutes, row.break_start), (360, 0, None))
        self.assertEqual(self.post("/worktime/confirm", acknowledge="1").status_code, 409)

    def test_freshness_blocks_export_confirm_and_edit_and_zeroes_new_absence_display(self):
        schedule = self.generate()
        entry_id = next(entry.id for entry in schedule.entries if entry.user_id == self.users["teacher1"].id and entry.day.day == 3)
        self.leave("teacher1", date(2026, 2, 3))
        self.assertEqual(self.post("/worktime/confirm", acknowledge="1").status_code, 409)
        self.assertEqual(self.post(f"/worktime/entries/{entry_id}", start="08:00", end="14:00").status_code, 409)
        export = self.client.get(f"/worktime/export/{self.users['teacher1'].id}?year=2026&month=2", headers={"Accept": "application/json"})
        self.assertEqual(export.status_code, 409)
        with patch("app.worktime.render_template", return_value="rendered") as render:
            self.client.get(f"/worktime?year=2026&month=2&place_id={self.site.id}&user_id={self.users['teacher1'].id}")
            context = render.call_args.kwargs
            self.assertTrue(context["is_stale"])
            row = next(row for row in context["rows"] if row["date"].day == 3)
            self.assertEqual(row["work_minutes"], 0)
            self.assertIsNone(row["start"])
        self.generate()
        export = self.client.get(f"/worktime/export/{self.users['teacher1'].id}?year=2026&month=2&format=csv")
        self.assertEqual(export.status_code, 200)

    def test_source_hash_includes_other_site_contracts_and_calendar_but_not_history(self):
        _, original = build_payload(self.site.id, 2026, 2)
        other = Contract(user=self.users["teacher1"], contract_type=ContractType.teacher, start_date=date(2026, 2, 10), job_title="Other", working_hours_per_week=40, legal_entity_id=self.site.legal_entity_id, place_of_work=self.other_site)
        db.session.add(other)
        db.session.commit()
        payload, changed = build_payload(self.site.id, 2026, 2)
        self.assertNotEqual(original, changed)
        self.assertEqual(len(payload["external_contracts"]), 1)
        schedule = self.generate()
        self.assertFalse(any(row.contract_id == other.id for row in schedule.entries))
        self.assertIn("overlapping_contracts", {issue["code"] for issue in schedule.issues})
        self.assertEqual(self.post("/worktime/confirm", acknowledge="1").status_code, 409)

    def test_confirmation_is_explicit_and_export_is_per_employee(self):
        schedule = self.generate()
        hard = [issue for issue in schedule.issues if issue["severity"] == "error"]
        self.assertEqual(hard, [])
        self.assertEqual(self.post("/worktime/confirm").status_code, 400)
        self.assertEqual(self.post("/worktime/confirm", acknowledge="1").status_code, 200)
        self.assertEqual(self.schedule().status, "confirmed")
        self.login("teacher1")
        pdf = self.client.get(f"/worktime/export/{self.users['teacher1'].id}?year=2026&month=2")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        csv = self.client.get(f"/worktime/export/{self.users['teacher1'].id}?year=2026&month=2&format=csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Minta teacher1", csv.data.decode("utf-8-sig"))
        self.assertNotIn("Minta teacher2", csv.data.decode("utf-8-sig"))
        self.assertEqual(self.client.get(f"/worktime/export/{self.users['teacher2'].id}?year=2026&month=2").status_code, 403)

    def test_group_assignment_dates_overlap_and_settings_revision(self):
        old_revision = settings_revision(self.site.id)
        added = self.setup_post(action="group", name="Group C", start_date="2026-02-01")
        self.assertEqual(added.status_code, 200)
        stale = self.post("/worktime/groups", settings_revision=old_revision, action="group", name="Group D", start_date="2026-02-01")
        self.assertEqual(stale.status_code, 409)
        group = WorkGroup.query.filter_by(name="Group C").one()
        invalid = self.setup_post(action="assignment", contract_id=str(self.contracts["teacher1"].id), group_id=str(group.id), start_date="2026-02-01", shift_phase="0")
        self.assertEqual(invalid.status_code, 400)
        assignment = WorkAssignment.query.filter_by(contract_id=self.contracts["teacher1"].id).one()
        edit = self.setup_post(action="assignment", assignment_id=str(assignment.id), contract_id=str(assignment.contract_id), group_id=str(assignment.group_id), start_date="2025-01-01", end_date="2026-01-31", shift_phase="0")
        self.assertEqual(edit.status_code, 200)
        valid = self.setup_post(action="assignment", contract_id=str(self.contracts["teacher1"].id), group_id=str(group.id), start_date="2026-02-01", shift_phase="1")
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(self.setup_post(action="group", group_id=str(group.id), name="Group C", start_date="2026-03-01").status_code, 400)
        foreign = WorkGroup(place_of_work_id=self.other_site.id, name="Foreign", start_date=date(2025, 1, 1))
        db.session.add(foreign)
        db.session.commit()
        self.assertEqual(self.setup_post(action="assignment", contract_id=str(self.contracts["assistant"].id), group_id=str(foreign.id), start_date="2026-02-01", shift_phase="0").status_code, 400)

    def test_manual_merges_block_invalid_targets_and_are_visible_in_export(self):
        self.leave("teacher1", date(2026, 2, 3))
        self.leave("teacher2", date(2026, 2, 3))
        schedule = self.generate()
        self.assertIn("manual_merge_required", {issue["code"] for issue in schedule.issues})
        for values in ({"source_group_id": str(self.groups[0].id), "target_group_id": str(self.groups[0].id)}, {"source_group_id": str(self.groups[0].id), "target_group_id": str(self.groups[1].id), "day": "2026-03-03"}):
            data = {"day": "2026-02-03", **values}
            self.assertEqual(self.post("/worktime/merges", **data).status_code, 400)
        merged = self.post("/worktime/merges", day="2026-02-03", source_group_id=str(self.groups[0].id), target_group_id=str(self.groups[1].id), note="Manual group combination")
        self.assertEqual(merged.status_code, 200)
        self.assertEqual(self.post("/worktime/merges", day="2026-02-03", source_group_id=str(self.groups[1].id), target_group_id=str(self.groups[0].id)).status_code, 400)
        self.generate()
        register = export_register(self.users["teacher1"], 2026, 2)
        row = next(row for row in register["rows"] if row["date"].day == 3)
        self.assertEqual(row["work_minutes"], 0)
        self.assertIn("Group A", row["note"])
        self.assertIn("Group B", row["note"])
        self.assertNotIn("paid leave", row["note"])
        self.assertNotIn(row["absence_label"], row["note"])
        from app.worktime_export import export_worktime_csv
        csv_text = export_worktime_csv(register).decode("utf-8-sig")
        self.assertNotIn("paid leave", csv_text)
        self.assertEqual(csv_text.count(row["absence_label"]), 1)
        merge_id = WorkGroupMerge.query.one().id
        self.assertEqual(self.post("/worktime/merges", action="delete", merge_id=str(merge_id)).status_code, 200)
        self.assertEqual(WorkGroupMerge.query.count(), 0)

    def test_export_requires_all_workplaces_and_never_combines_another_employee(self):
        self.contracts["teacher1"].end_date = date(2026, 2, 10)
        WorkAssignment.query.filter_by(contract_id=self.contracts["teacher1"].id).one().end_date = date(2026, 2, 10)
        later = Contract(user=self.users["teacher1"], contract_type=ContractType.employee_under_the_labour_code, start_date=date(2026, 2, 11), job_title="Later job", working_hours_per_week=40, legal_entity_id=self.site.legal_entity_id, place_of_work=self.other_site)
        db.session.add(later)
        db.session.commit()
        self.generate()
        url = f"/worktime/export/{self.users['teacher1'].id}?year=2026&month=2&format=csv"
        self.assertEqual(self.client.get(url, headers={"Accept": "application/json"}).status_code, 409)
        result = self.post("/worktime/generate", place_id=str(self.other_site.id), revision="")
        self.assertEqual(result.status_code, 200)
        register = export_register(self.users["teacher1"], 2026, 2)
        self.assertEqual({row["workplace"] for row in register["rows"]}, {self.site.address, self.other_site.address})
        self.assertEqual({row["user_id"] for row in register["rows"]}, {self.users["teacher1"].id})

    def test_partial_month_without_staff_cannot_be_confirmed_and_employee_issues_are_private(self):
        for contract in self.contracts.values():
            contract.end_date = date(2026, 2, 6)
        for assignment in WorkAssignment.query.all():
            assignment.end_date = date(2026, 2, 6)
        db.session.commit()
        schedule = self.generate()
        self.assertTrue(schedule.entries)
        self.assertTrue(all(entry.day <= date(2026, 2, 6) for entry in schedule.entries))
        self.assertTrue(any(issue.get("day") == "2026-02-09" and issue["severity"] == "error" for issue in schedule.issues))
        self.assertEqual(self.post("/worktime/confirm", acknowledge="1").status_code, 409)
        self.login("teacher1")
        with patch("app.worktime.render_template", return_value="rendered") as render:
            self.client.get(f"/worktime?year=2026&month=2&place_id={self.site.id}")
            issues = render.call_args.kwargs["issues"]
            self.assertTrue(all(issue.get("contract_id") == self.contracts["teacher1"].id for issue in issues))

    def test_actual_manually_edited_openers_still_count_in_fairness_history(self):
        result = self.post("/worktime/generate", month="1", revision="")
        self.assertEqual(result.status_code, 200)
        january = WorkSchedule.query.filter_by(place_of_work_id=self.site.id, year=2026, month=1).one()
        entry = next(row for row in january.entries if row.start_minute == 420 and row.day < date(2026, 1, 26))
        entry.shift, entry.is_manual = "manual", True
        db.session.commit()
        payload, before = build_payload(self.site.id, 2026, 2)
        historic = next(row for row in payload["history"] if row["day"] == entry.day.isoformat() and row["contract_id"] == entry.contract_id)
        self.assertEqual(historic["shift"], "early_teacher")
        entry.note = "Historical comment"
        db.session.commit()
        self.assertEqual(before, build_payload(self.site.id, 2026, 2)[1])
