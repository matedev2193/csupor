"""Work-time schema compatibility and account-deletion data preservation."""

import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

from flask import Flask
from sqlalchemy import create_mock_engine, delete, event, inspect, text
from sqlalchemy.dialects import mysql

from app import db
from app.account_deletion import AccountDeletionConflict, delete_user_account
from app.models import Contract, ContractType, LegalEntity, PlaceOfWork, User
from app.schema import create_missing_tables
from app.worktime_models import (
    WorkAssignment, WorkGroup, WorkGroupMerge, WorkSchedule, WorkTimeEntry,
)


WORK_MODELS = (WorkGroup, WorkAssignment, WorkGroupMerge, WorkSchedule, WorkTimeEntry)
WORK_TABLES = {model.__tablename__ for model in WORK_MODELS}


class WorkTimeMySQLSchemaTests(unittest.TestCase):
    def compile_schema(self, existing, identifier_types):
        statements = []
        engine = create_mock_engine(
            "mysql+mysqlconnector://",
            lambda statement, *args, **kwargs: statements.append(
                str(statement.compile(dialect=mysql.dialect()))
            ),
        )
        inspector = Mock()
        inspector.get_table_names.return_value = existing
        inspector.get_columns.side_effect = lambda name, schema=None: [
            {"name": "id", "type": identifier_types[name]}
        ]
        with patch("app.schema.inspect", return_value=inspector):
            create_missing_tables(engine, db.metadata)
        tables = {
            model.__tablename__: next(
                (sql for sql in statements if f"CREATE TABLE {model.__tablename__} " in sql), None
            )
            for model in WORK_MODELS
        }
        return statements, tables, inspector

    def test_five_new_tables_match_signed_unsigned_and_bigint_existing_identifiers(self):
        existing = [name for name in db.metadata.tables if name not in WORK_TABLES]
        original_types = {
            column: column.type
            for model in WORK_MODELS for column in model.__table__.columns
            if column.foreign_keys
        }
        for reference_type, expected in (
            (mysql.INTEGER(), "INTEGER"),
            (mysql.INTEGER(unsigned=True), "INTEGER UNSIGNED"),
            (mysql.BIGINT(unsigned=True), "BIGINT UNSIGNED"),
        ):
            with self.subTest(identifier_type=expected):
                statements, tables, inspector = self.compile_schema(
                    existing,
                    {name: reference_type for name in ("users", "contracts", "places_of_work")},
                )
                self.assertEqual(sum("CREATE TABLE" in sql for sql in statements), 5)
                self.assertTrue(all(tables.values()))
                for table, columns in {
                    "work_groups": ("place_of_work_id",),
                    "work_assignments": ("contract_id",),
                    "work_group_merges": ("created_by_id",),
                    "work_schedules": ("place_of_work_id", "generated_by_id", "confirmed_by_id"),
                    "work_time_entries": ("contract_id", "user_id"),
                }.items():
                    for column in columns:
                        self.assertIn(f"{column} {expected}", tables[table])
                # Newly created groups/schedules keep their signed ORM primary keys.
                self.assertIn("group_id INTEGER NOT NULL", tables["work_assignments"])
                self.assertIn("schedule_id INTEGER NOT NULL", tables["work_time_entries"])
                self.assertIn("group_id INTEGER,", tables["work_time_entries"])
                self.assertIn("ON DELETE SET NULL", tables["work_schedules"])
                self.assertIn("ON DELETE CASCADE", tables["work_time_entries"])
                reflected = [call.args[0] for call in inspector.get_columns.call_args_list]
                self.assertCountEqual(reflected, ["users", "contracts", "places_of_work"])
                for column, original in original_types.items():
                    self.assertIs(column.type, original)

    def test_partial_installation_reflects_existing_work_group_and_schedule_keys(self):
        missing = {"work_assignments", "work_group_merges", "work_time_entries"}
        existing = [name for name in db.metadata.tables if name not in missing]
        _, tables, inspector = self.compile_schema(existing, {
            "users": mysql.INTEGER(unsigned=True),
            "contracts": mysql.INTEGER(),
            "work_groups": mysql.BIGINT(unsigned=True),
            "work_schedules": mysql.BIGINT(unsigned=True),
        })
        self.assertIsNone(tables["work_groups"])
        self.assertIsNone(tables["work_schedules"])
        self.assertIn("group_id BIGINT UNSIGNED NOT NULL", tables["work_assignments"])
        self.assertIn("source_group_id BIGINT UNSIGNED NOT NULL", tables["work_group_merges"])
        self.assertIn("target_group_id BIGINT UNSIGNED NOT NULL", tables["work_group_merges"])
        self.assertIn("schedule_id BIGINT UNSIGNED NOT NULL", tables["work_time_entries"])
        self.assertIn("group_id BIGINT UNSIGNED,", tables["work_time_entries"])
        self.assertIn("contract_id INTEGER NOT NULL", tables["work_time_entries"])
        self.assertIn("user_id INTEGER UNSIGNED NOT NULL", tables["work_time_entries"])
        reflected = [call.args[0] for call in inspector.get_columns.call_args_list]
        self.assertCountEqual(reflected, ["users", "contracts", "work_groups", "work_schedules"])

    def test_fresh_schema_creates_dependencies_before_work_entries(self):
        statements, tables, inspector = self.compile_schema([], {})
        table_sql = [sql for sql in statements if "CREATE TABLE" in sql]
        positions = {
            name: next(index for index, sql in enumerate(table_sql) if f"CREATE TABLE {name} " in sql)
            for name in (*WORK_TABLES, "users", "contracts", "places_of_work")
        }
        for parent, child in (
            ("places_of_work", "work_groups"), ("places_of_work", "work_schedules"),
            ("contracts", "work_assignments"), ("work_groups", "work_assignments"),
            ("work_groups", "work_group_merges"), ("users", "work_schedules"),
            ("work_schedules", "work_time_entries"), ("contracts", "work_time_entries"),
        ):
            self.assertLess(positions[parent], positions[child])
        self.assertIn("user_id INTEGER NOT NULL", tables["work_time_entries"])
        self.assertIn("generated_by_id INTEGER,", tables["work_schedules"])
        inspector.get_columns.assert_not_called()

    def test_completed_mysql_schema_has_no_ddl_or_identifier_reflection(self):
        statements, _, inspector = self.compile_schema(list(db.metadata.tables), {})
        self.assertEqual(statements, [])
        inspector.get_columns.assert_not_called()


class WorkTimePersistenceTests(unittest.TestCase):
    def setUp(self):
        # Keep schema/service coverage independent of in-progress route registration.
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.assertEqual(db.session.execute(text("PRAGMA foreign_keys")).scalar(), 1)
        create_missing_tables(db.engine, db.metadata)
        self.target = User(username="target", email="target@example.invalid", password_hash="unused")
        self.other = User(username="other", email="other@example.invalid", password_hash="unused")
        entity = LegalEntity(name="Nursery", address="Example", om_id="123456", tax_number="12345678901")
        self.place = PlaceOfWork(legal_entity=entity, address="Example")
        self.contracts = [Contract(
            user=user, employer=entity, place_of_work=self.place,
            contract_type=ContractType.teacher, start_date=date(2026, 1, 1),
            job_title="Teacher", working_hours_per_week=40,
        ) for user in (self.target, self.other)]
        self.groups = [WorkGroup(place_of_work=self.place, name=name, start_date=date(2026, 1, 1))
                       for name in ("First", "Second")]
        db.session.add_all([self.target, self.other, *self.contracts, *self.groups])
        db.session.flush()
        self.assignments = [WorkAssignment(contract=contract, group=group, start_date=date(2026, 1, 1), shift_phase=index)
                            for index, (contract, group) in enumerate(zip(self.contracts, self.groups))]
        self.merge = WorkGroupMerge(
            day=date(2026, 10, 6), source_group=self.groups[0], target_group=self.groups[1],
            note="Recorded group merge", created_by_id=self.target.id,
        )
        self.schedule = WorkSchedule(
            place_of_work=self.place, year=2026, month=10, revision="a" * 32, source_hash="b" * 64,
            issues=[{"code": "example", "message": "Preserve this note"}], status="confirmed",
            generated_at=datetime(2026, 10, 1, 8), generated_by_id=self.target.id,
            confirmed_at=datetime(2026, 10, 1, 9), confirmed_by_id=self.target.id,
        )
        self.entries = [WorkTimeEntry(
            schedule=self.schedule, contract=contract, user=user, group=group, day=date(2026, 10, 5),
            start_minute=start, end_minute=start + 404, break_start=start + 240,
            break_minutes=20, work_minutes=384, teaching_minutes=312,
            shift=shift, note="Saved employee note", is_manual=True,
        ) for user, contract, group, start, shift in zip(
            (self.target, self.other), self.contracts, self.groups, (480, 646), ("AM", "PM"),
        )]
        db.session.add_all([*self.assignments, self.merge, self.schedule, *self.entries])
        db.session.commit()
        self.target_id, self.other_id = self.target.id, self.other.id
        self.contract_ids = [contract.id for contract in self.contracts]
        self.entry_ids = [entry.id for entry in self.entries]
        self.assignment_ids = [assignment.id for assignment in self.assignments]
        self.schedule_id, self.merge_id = self.schedule.id, self.merge.id

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def work_rows(self):
        return {
            model.__tablename__: [dict(row) for row in db.session.execute(
                model.__table__.select().order_by(model.__table__.c.id)
            ).mappings()]
            for model in WORK_MODELS
        }

    def test_sqlite_repeated_startup_preserves_all_five_work_tables_and_records(self):
        before = self.work_rows()
        statements = []
        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(db.engine, "before_cursor_execute", collect)
        try:
            create_missing_tables(db.engine, db.metadata)
            create_missing_tables(db.engine, db.metadata)
        finally:
            event.remove(db.engine, "before_cursor_execute", collect)
        self.assertTrue(WORK_TABLES <= set(inspect(db.engine).get_table_names()))
        self.assertFalse(any(sql.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP ")) for sql in statements))
        self.assertEqual(self.work_rows(), before)

    def test_account_deletion_removes_owned_work_records_but_preserves_shared_register(self):
        other_before = dict(db.session.execute(WorkTimeEntry.__table__.select().where(
            WorkTimeEntry.id == self.entry_ids[1]
        )).mappings().one())
        delete_user_account(self.target)
        db.session.commit()
        self.assertIsNone(db.session.get(User, self.target_id))
        self.assertIsNone(db.session.get(Contract, self.contract_ids[0]))
        self.assertIsNone(db.session.get(WorkAssignment, self.assignment_ids[0]))
        self.assertIsNone(db.session.get(WorkTimeEntry, self.entry_ids[0]))
        self.assertIsNotNone(db.session.get(User, self.other_id))
        self.assertIsNotNone(db.session.get(WorkAssignment, self.assignment_ids[1]))
        other_after = dict(db.session.execute(WorkTimeEntry.__table__.select().where(
            WorkTimeEntry.id == self.entry_ids[1]
        )).mappings().one())
        self.assertEqual(other_after, other_before)
        schedule = db.session.get(WorkSchedule, self.schedule_id)
        self.assertIsNone(schedule.generated_by_id)
        self.assertIsNone(schedule.confirmed_by_id)
        self.assertEqual((schedule.status, schedule.revision), ("confirmed", "a" * 32))
        self.assertEqual(schedule.issues, [{"code": "example", "message": "Preserve this note"}])
        merge = db.session.get(WorkGroupMerge, self.merge_id)
        self.assertIsNone(merge.created_by_id)
        self.assertEqual(merge.note, "Recorded group merge")
        self.assertEqual(WorkGroup.query.count(), 2)
        self.assertEqual(PlaceOfWork.query.count(), 1)

    def test_direct_contract_delete_cascades_assignments_and_entries_only(self):
        db.session.execute(delete(Contract).where(Contract.id == self.contract_ids[0]))
        db.session.commit()
        self.assertIsNone(db.session.get(WorkAssignment, self.assignment_ids[0]))
        self.assertIsNone(db.session.get(WorkTimeEntry, self.entry_ids[0]))
        self.assertIsNotNone(db.session.get(WorkAssignment, self.assignment_ids[1]))
        self.assertIsNotNone(db.session.get(WorkTimeEntry, self.entry_ids[1]))
        self.assertIsNotNone(db.session.get(User, self.target_id))
        self.assertEqual(WorkSchedule.query.count(), 1)
        self.assertEqual(WorkGroupMerge.query.count(), 1)

    def test_rollback_restores_deleted_work_records_and_cleared_attribution(self):
        before = self.work_rows()
        delete_user_account(self.target)
        db.session.flush()
        self.assertIsNone(db.session.get(User, self.target_id))
        self.assertEqual(WorkTimeEntry.query.filter_by(user_id=self.target_id).count(), 0)
        db.session.rollback()
        self.assertIsNotNone(db.session.get(User, self.target_id))
        self.assertIsNotNone(db.session.get(Contract, self.contract_ids[0]))
        self.assertEqual(self.work_rows(), before)

    def test_inconsistent_entry_ownership_blocks_deletion_before_any_change(self):
        self.entries[0].user_id = self.other_id
        db.session.commit()
        before = self.work_rows()
        with self.assertRaises(AccountDeletionConflict):
            delete_user_account(self.target)
        self.assertEqual(self.work_rows(), before)
        self.assertIsNotNone(db.session.get(User, self.target_id))
        self.assertIsNotNone(db.session.get(Contract, self.contract_ids[0]))


if __name__ == "__main__":
    unittest.main()
