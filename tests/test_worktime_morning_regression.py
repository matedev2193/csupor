"""Persisted opening history must survive month and contract boundaries."""

import unittest
from datetime import date, datetime

from flask import Flask
from sqlalchemy import text

from app import db
from app.models import Contract, ContractType, LegalEntity, PlaceOfWork, User
from app.worktime_engine import build_schedule
from app.worktime_models import WorkAssignment, WorkGroup, WorkSchedule, WorkTimeEntry
from app.worktime_service import build_payload, settings_revision


class MorningOpeningHistoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        db.create_all()
        entity = LegalEntity(name="Nursery", address="Example", om_id="123456", tax_number="12345678901")
        self.site = PlaceOfWork(legal_entity=entity, address="Example")
        self.groups = [WorkGroup(place_of_work=self.site, name=name, start_date=date(2026, 1, 1))
                       for name in ("Group A", "Group B")]
        db.session.add_all([entity, self.site, *self.groups])
        db.session.flush()
        self.users = {}
        for name in ("morning_a", "afternoon_a", "morning_b", "afternoon_b", "nurse_a", "nurse_b"):
            user = User(username=name, email=f"{name}@example.invalid", password_hash="unused")
            db.session.add(user)
            self.users[name] = user
        db.session.flush()

        # The expired contract does not overlap even the first boundary week,
        # so history cannot recover its owner by looking at current workers.
        self.old_contract = self.contract("morning_a", ContractType.teacher, date(2026, 1, 1), date(2026, 9, 25))
        self.contracts = {}
        for name, group_index, phase in (
            ("morning_a", 0, 0), ("afternoon_a", 0, 1),
            ("morning_b", 1, 0), ("afternoon_b", 1, 1),
            ("nurse_a", 0, 0), ("nurse_b", 1, 1),
        ):
            role = ContractType.nursery_assistant if name.startswith("nurse") else ContractType.teacher
            start = date(2026, 9, 28) if name == "morning_a" else date(2026, 1, 1)
            contract = self.contract(name, role, start)
            self.contracts[name] = contract
            db.session.add(WorkAssignment(contract=contract, group=self.groups[group_index], start_date=start, shift_phase=phase))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def contract(self, name, role, start, end=None):
        contract = Contract(user=self.users[name], employer=self.site.legal_entity, place_of_work=self.site,
                            contract_type=role, job_title=name, working_hours_per_week=40,
                            start_date=start, end_date=end)
        db.session.add(contract)
        db.session.flush()
        return contract

    def october_first_rows(self):
        payload, _ = build_payload(self.site.id, 2026, 10)
        # Keep the real service boundary week and history, without allocating
        # unrelated later October weeks in this focused regression.
        payload["days"] = [day for day in payload["days"] if day <= "2026-10-01"]
        result = build_schedule(payload)
        errors = [issue for issue in result["issues"]
                  if issue["severity"] == "error" and issue.get("day") == "2026-10-01"]
        self.assertEqual(errors, [])
        return payload, [row for row in result["entries"] if row["day"] == "2026-10-01"]

    def test_actual_manual_boundary_openings_and_previous_contract_count_for_same_person(self):
        previous = WorkSchedule(place_of_work=self.site, year=2026, month=9, revision="a" * 32,
                                source_hash="b" * 64, status="confirmed", generated_at=datetime(2026, 9, 1))
        db.session.add(previous)
        for contract, group, days in (
            (self.old_contract, self.groups[0], range(21, 26)),
            (self.contracts["morning_b"], self.groups[1], range(28, 31)),
        ):
            for day in days:
                db.session.add(WorkTimeEntry(
                    schedule=previous, contract=contract, user=contract.user, group=group,
                    day=date(2026, 9, day), start_minute=420, end_minute=824,
                    break_start=720, break_minutes=20, work_minutes=384, teaching_minutes=384,
                    shift="manual", is_manual=True, note="Actual opening recorded manually",
                ))
        db.session.commit()

        payload, rows = self.october_first_rows()
        self.assertNotIn(self.old_contract.id, {worker["contract_id"] for worker in payload["workers"]})
        opener = next(row for row in rows if row["shift"] == "early_teacher")
        # A has five openings under the old contract, B has three real boundary
        # openings. B remains least-used despite having opened the previous day.
        self.assertEqual(opener["user_id"], self.users["morning_b"].id)
        self.assertEqual(next(row for row in rows if row["user_id"] == self.users["morning_a"].id)["start_minute"], 480)
        for name in ("afternoon_a", "afternoon_b"):
            row = next(row for row in rows if row["user_id"] == self.users[name].id)
            self.assertEqual((row["shift"], row["end_minute"]), ("afternoon", 1050))

    def test_unsaved_boundary_days_do_not_invent_historical_opening_counts(self):
        payload, rows = self.october_first_rows()
        self.assertEqual(payload["history"], [])
        # With no saved history, both morning employees start equally used.
        # A wins the stable tie-break; simulated September days must not turn
        # the result into B's turn before the first October entry even exists.
        opener = next(row for row in rows if row["shift"] == "early_teacher")
        self.assertEqual(opener["user_id"], self.users["morning_a"].id)

    def test_flexible_assignment_changes_payload_freshness_and_settings_revision(self):
        _, original_hash = build_payload(self.site.id, 2026, 10)
        original_revision = settings_revision(self.site.id)
        contract = self.contracts["nurse_a"]
        assignment = WorkAssignment.query.filter_by(contract_id=contract.id).one()
        assignment.flexible_shift = True
        db.session.commit()

        payload, flexible_hash = build_payload(self.site.id, 2026, 10)
        nurse = next(worker for worker in payload["workers"] if worker["contract_id"] == contract.id)
        self.assertIsNone(nurse["assignments"][0]["shift_phase"])
        self.assertNotEqual(original_hash, flexible_hash)
        self.assertNotEqual(original_revision, settings_revision(self.site.id))
        # The legacy phase is retained so older assignments remain unchanged.
        self.assertEqual(assignment.shift_phase, 0)


if __name__ == "__main__":
    unittest.main()
