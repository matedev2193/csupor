"""Flexible-shift schema updates preserve all existing assignment information."""

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, Mock, patch

from mysql.connector.errors import ProgrammingError
from sqlalchemy import Boolean, create_engine, event, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import CreateTable

from app import create_app, db
from app import models  # noqa: F401 -- register the referenced parent tables
from app.schema import ensure_work_assignment_flexible_shift_column
from app.worktime_models import WorkAssignment


class FlexibleShiftSchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_url = "sqlite:///" + os.path.join(self.directory.name, "worktime.db")

    def tearDown(self):
        self.directory.cleanup()

    def legacy_database(self):
        engine = create_engine(self.database_url)
        with engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TABLE work_assignments (
                    id INTEGER PRIMARY KEY, contract_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL, start_date DATE NOT NULL,
                    end_date DATE NULL, shift_phase INTEGER NOT NULL DEFAULT 0,
                    CONSTRAINT work_assignment_phase CHECK (shift_phase IN (0, 1)),
                    CONSTRAINT work_assignment_dates
                        CHECK (end_date IS NULL OR end_date >= start_date)
                )
            """)
            connection.exec_driver_sql("""
                INSERT INTO work_assignments
                    (id, contract_id, group_id, start_date, end_date, shift_phase)
                VALUES (1, 11, 21, '2026-01-01', NULL, 0),
                       (2, 12, 22, '2026-02-01', '2026-12-31', 1)
            """)
        return engine

    def make_app(self):
        with patch.dict(os.environ, {
            "DATABASE_URL": self.database_url, "SECRET_KEY": "flexible-shift-schema-tests",
        }):
            return create_app()

    def test_startup_preserves_legacy_records_and_phase_then_retains_explicit_flexible_choice(self):
        engine = self.legacy_database()
        with engine.connect() as connection:
            before = [tuple(row) for row in connection.execute(text(
                "SELECT id, contract_id, group_id, start_date, end_date, shift_phase "
                "FROM work_assignments ORDER BY id"
            ))]
        engine.dispose()
        app = self.make_app()
        with app.app_context():
            after = [tuple(row) for row in db.session.execute(text(
                "SELECT id, contract_id, group_id, start_date, end_date, shift_phase "
                "FROM work_assignments ORDER BY id"
            ))]
            self.assertEqual(after, before)
            self.assertFalse(db.session.get(WorkAssignment, 1).flexible_shift)
            assignment = db.session.get(WorkAssignment, 2)
            self.assertEqual((assignment.flexible_shift, assignment.shift_phase), (False, 1))
            assignment.flexible_shift = True
            db.session.commit()
            db.session.remove()
            db.engine.dispose()

        restarted = self.make_app()
        with restarted.app_context():
            assignment = db.session.get(WorkAssignment, 2)
            self.assertEqual((assignment.flexible_shift, assignment.shift_phase), (True, 1))
            self.assertFalse(db.session.get(WorkAssignment, 1).flexible_shift)
            self.assertEqual(WorkAssignment.query.count(), 2)
            db.session.remove()
            db.engine.dispose()

    def test_migration_only_adds_one_boolean_column_and_leaves_phase_constraint_intact(self):
        engine = self.legacy_database()
        statements = []

        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", collect)
        try:
            ensure_work_assignment_flexible_shift_column(engine)
            ensure_work_assignment_flexible_shift_column(engine)
            changes = [statement for statement in statements if statement.lstrip().upper().startswith(
                ("ALTER ", "UPDATE ", "INSERT ", "DELETE ", "DROP ", "CREATE ")
            )]
            self.assertEqual(changes, [
                'ALTER TABLE "work_assignments" ADD COLUMN "flexible_shift" BOOLEAN NOT NULL DEFAULT 0',
            ])
            column = next(column for column in inspect(engine).get_columns("work_assignments")
                          if column["name"] == "flexible_shift")
            self.assertIsInstance(column["type"], Boolean)
            self.assertFalse(column["nullable"])
            self.assertEqual(column["default"], "0")
            with engine.begin() as connection:
                with self.assertRaises(IntegrityError):
                    connection.exec_driver_sql("UPDATE work_assignments SET shift_phase = 2 WHERE id = 1")
                self.assertEqual(connection.execute(text(
                    "SELECT shift_phase, flexible_shift FROM work_assignments ORDER BY id"
                )).all(), [(0, 0), (1, 0)])
        finally:
            event.remove(engine, "before_cursor_execute", collect)
            engine.dispose()

    def test_fresh_schema_has_python_and_server_false_defaults_and_accepts_true(self):
        engine = create_engine(self.database_url)
        WorkAssignment.__table__.create(engine)
        try:
            with engine.begin() as connection:
                # Raw SQL exercises the server default; Core insert exercises
                # the ORM column's Python default for existing callers.
                connection.exec_driver_sql("""
                    INSERT INTO work_assignments (id, contract_id, group_id, start_date, shift_phase)
                    VALUES (1, 11, 21, '2026-01-01', 1)
                """)
                connection.execute(WorkAssignment.__table__.insert(), {
                    "id": 2, "contract_id": 12, "group_id": 21, "start_date": date(2026, 1, 1),
                })
                connection.execute(WorkAssignment.__table__.insert(), {
                    "id": 3, "contract_id": 13, "group_id": 21, "start_date": date(2026, 1, 1),
                    "flexible_shift": True, "shift_phase": 1,
                })
                self.assertEqual(connection.execute(text(
                    "SELECT flexible_shift, shift_phase FROM work_assignments ORDER BY id"
                )).all(), [(0, 1), (0, 0), (1, 1)])
                with self.assertRaises(IntegrityError):
                    connection.exec_driver_sql("UPDATE work_assignments SET flexible_shift = NULL WHERE id = 1")
        finally:
            engine.dispose()
        mysql_ddl = str(CreateTable(WorkAssignment.__table__).compile(dialect=mysql.dialect()))
        self.assertIn("flexible_shift BOOL NOT NULL DEFAULT false", mysql_ddl)
        self.assertIn("CHECK (shift_phase IN (0, 1))", mysql_ddl)

    def mock_engine(self, dialect_name="mysql"):
        engine = MagicMock()
        engine.dialect = mysql.dialect()
        engine.dialect.name = dialect_name
        return engine, engine.begin.return_value.__enter__.return_value

    def test_mysql_and_mariadb_add_only_the_missing_non_nullable_flag(self):
        for dialect_name in ("mysql", "mariadb"):
            with self.subTest(dialect=dialect_name):
                engine, connection = self.mock_engine(dialect_name)
                inspector = Mock()
                inspector.get_columns.return_value = [{"name": "id"}, {"name": "shift_phase"}]
                with patch("app.schema.inspect", return_value=inspector):
                    ensure_work_assignment_flexible_shift_column(engine)
                connection.exec_driver_sql.assert_called_once_with(
                    "ALTER TABLE `work_assignments` ADD COLUMN `flexible_shift` BOOL NOT NULL DEFAULT false"
                )

    def test_concurrent_duplicate_is_ignored_only_after_column_is_confirmed(self):
        for dialect_name, original in (
            ("sqlite", sqlite3.OperationalError("duplicate column name: flexible_shift")),
            ("mysql", ProgrammingError("Duplicate column name 'flexible_shift'", errno=1060)),
            ("mariadb", Exception(1060, "Duplicate column name 'flexible_shift'")),
        ):
            with self.subTest(dialect=dialect_name):
                engine, connection = self.mock_engine(dialect_name)
                connection.exec_driver_sql.side_effect = OperationalError("ALTER TABLE", {}, original)
                inspector = Mock()
                inspector.get_columns.side_effect = [
                    [{"name": "shift_phase"}],
                    [{"name": "shift_phase"}, {"name": "flexible_shift"}],
                ]
                with patch("app.schema.inspect", return_value=inspector):
                    ensure_work_assignment_flexible_shift_column(engine)
                self.assertEqual(connection.exec_driver_sql.call_count, 1)
                self.assertEqual(inspector.get_columns.call_count, 2)

    def test_permission_lock_unrelated_and_unconfirmed_duplicate_errors_are_not_hidden(self):
        for dialect_name, original, expected_inspections in (
            ("mysql", Exception(1142, "ALTER command denied"), 1),
            ("mariadb", Exception(1205, "Lock wait timeout exceeded"), 1),
            ("sqlite", sqlite3.OperationalError("database is locked"), 1),
            ("sqlite", sqlite3.OperationalError("duplicate column name: other_column"), 1),
            ("mysql", Exception(1060, "Duplicate column name 'flexible_shift'"), 2),
        ):
            with self.subTest(dialect=dialect_name, message=str(original)):
                engine, connection = self.mock_engine(dialect_name)
                failure = OperationalError("ALTER TABLE", {}, original)
                connection.exec_driver_sql.side_effect = failure
                inspector = Mock()
                inspector.get_columns.return_value = [{"name": "shift_phase"}]
                with patch("app.schema.inspect", return_value=inspector), self.assertRaises(OperationalError) as caught:
                    ensure_work_assignment_flexible_shift_column(engine)
                self.assertIs(caught.exception, failure)
                self.assertEqual(inspector.get_columns.call_count, expected_inspections)


if __name__ == "__main__":
    unittest.main()
