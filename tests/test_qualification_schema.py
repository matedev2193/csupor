"""Exact qualification dates migrate without changing legacy year-only records."""

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, Mock, patch

from mysql.connector.errors import ProgrammingError
from sqlalchemy import Date, create_engine, event, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError

from app import create_app, db
from app.models import EducationalQualification, ProfessionalExam, User
from app.schema import QUALIFICATION_DATE_TABLES, ensure_qualification_date_columns


class QualificationDateSchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_url = "sqlite:///" + os.path.join(self.directory.name, "qualifications.db")

    def tearDown(self):
        self.directory.cleanup()

    def legacy_database(self):
        engine = create_engine(self.database_url)
        User.__table__.create(engine)
        with engine.begin() as connection:
            connection.execute(User.__table__.insert(), {
                "id": 1, "email": "legacy@example.invalid", "username": "legacy",
                "password_hash": "unused", "privilege": "employee",
            })
            connection.exec_driver_sql("""
                CREATE TABLE educational_qualifications (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                    level_or_type VARCHAR(120) NOT NULL, qualification_name VARCHAR(120) NOT NULL,
                    institution_name VARCHAR(120) NOT NULL, degree_number VARCHAR(80) NOT NULL,
                    year_obtained INTEGER NOT NULL, highest BOOLEAN NOT NULL DEFAULT 0
                )
            """)
            connection.exec_driver_sql("""
                CREATE TABLE professional_exams (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
                    qualification_name VARCHAR(120) NOT NULL, year_obtained INTEGER NOT NULL,
                    degree_number VARCHAR(80) NOT NULL
                )
            """)
            connection.execute(text("""
                INSERT INTO educational_qualifications
                (id, user_id, level_or_type, qualification_name, institution_name, degree_number, year_obtained, highest)
                VALUES (1, 1, 'BA', 'Nursery teacher', 'Example University', 'EDU-123', 2021, 1)
            """))
            connection.execute(text("""
                INSERT INTO professional_exams
                (id, user_id, qualification_name, year_obtained, degree_number)
                VALUES (1, 1, 'Professional exam', 2024, 'EXAM-456')
            """))
        return engine

    def make_app(self):
        with patch.dict(os.environ, {"DATABASE_URL": self.database_url, "SECRET_KEY": "date-migration-tests"}):
            return create_app()

    def test_startup_preserves_legacy_years_without_fabricating_dates(self):
        legacy_engine = self.legacy_database()
        for table in QUALIFICATION_DATE_TABLES:
            self.assertNotIn("date_obtained", {column["name"] for column in inspect(legacy_engine).get_columns(table)})
        legacy_engine.dispose()
        app = self.make_app()
        with app.app_context():
            qualification = db.session.get(EducationalQualification, 1)
            exam = db.session.get(ProfessionalExam, 1)
            self.assertEqual((qualification.year_obtained, qualification.date_obtained), (2021, None))
            self.assertEqual((exam.year_obtained, exam.date_obtained), (2024, None))
            self.assertEqual((qualification.degree_number, qualification.highest), ("EDU-123", True))
            self.assertEqual(exam.degree_number, "EXAM-456")
            for table in QUALIFICATION_DATE_TABLES:
                column = next(column for column in inspect(db.engine).get_columns(table) if column["name"] == "date_obtained")
                self.assertTrue(column["nullable"])
                self.assertIsInstance(column["type"], Date)
            qualification.date_obtained = date(2021, 6, 17)
            db.session.commit()
            db.session.remove()
            db.engine.dispose()

        # Restarting leaves known exact dates and still-unknown legacy dates intact.
        restarted = self.make_app()
        with restarted.app_context():
            self.assertEqual(db.session.get(EducationalQualification, 1).date_obtained, date(2021, 6, 17))
            exam = db.session.get(ProfessionalExam, 1)
            self.assertEqual((exam.year_obtained, exam.date_obtained), (2024, None))
            self.assertEqual(EducationalQualification.query.count(), 1)
            self.assertEqual(ProfessionalExam.query.count(), 1)
            db.session.remove()
            db.engine.dispose()

    def test_migration_is_idempotent_and_only_adds_missing_columns(self):
        engine = self.legacy_database()
        with engine.begin() as connection:
            connection.exec_driver_sql('ALTER TABLE "educational_qualifications" ADD COLUMN "date_obtained" DATE NULL')
            connection.exec_driver_sql("UPDATE educational_qualifications SET date_obtained = '2021-06-17'")
        statements = []

        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", collect)
        try:
            ensure_qualification_date_columns(engine)
            ensure_qualification_date_columns(engine)
            ddl = [statement for statement in statements if statement.startswith("ALTER TABLE")]
            self.assertEqual(ddl, ['ALTER TABLE "professional_exams" ADD COLUMN "date_obtained" DATE NULL'])
            with engine.connect() as connection:
                qualification = connection.execute(text("SELECT year_obtained, date_obtained FROM educational_qualifications")).one()
                exam = connection.execute(text("SELECT year_obtained, date_obtained FROM professional_exams")).one()
                self.assertEqual(tuple(qualification), (2021, "2021-06-17"))
                self.assertEqual(tuple(exam), (2024, None))
        finally:
            event.remove(engine, "before_cursor_execute", collect)
            engine.dispose()

    def test_fresh_database_has_nullable_dates_and_required_legacy_years(self):
        app = self.make_app()
        with app.app_context():
            for model in (EducationalQualification, ProfessionalExam):
                columns = {column["name"]: column for column in inspect(db.engine).get_columns(model.__tablename__)}
                self.assertTrue(columns["date_obtained"]["nullable"])
                self.assertFalse(columns["year_obtained"]["nullable"])
                self.assertIsInstance(columns["date_obtained"]["type"], Date)
            db.session.remove()
            db.engine.dispose()

    def mock_engine(self, dialect_name="mysql"):
        engine = MagicMock()
        engine.dialect = mysql.dialect()
        engine.dialect.name = dialect_name
        connection = engine.begin.return_value.__enter__.return_value
        return engine, connection

    def test_mysql_and_mariadb_generate_only_quoted_nullable_date_additions(self):
        for dialect_name in ("mysql", "mariadb"):
            with self.subTest(dialect=dialect_name):
                engine, connection = self.mock_engine(dialect_name)
                inspector = Mock()
                inspector.get_columns.return_value = [{"name": "id"}, {"name": "year_obtained"}]
                with patch("app.schema.inspect", return_value=inspector):
                    ensure_qualification_date_columns(engine)
                self.assertEqual([call.args[0] for call in connection.exec_driver_sql.call_args_list], [
                    "ALTER TABLE `educational_qualifications` ADD COLUMN `date_obtained` DATE NULL",
                    "ALTER TABLE `professional_exams` ADD COLUMN `date_obtained` DATE NULL",
                ])

    def test_concurrent_duplicate_column_is_ignored_only_when_reinspection_confirms_it(self):
        errors = [
            ("sqlite", sqlite3.OperationalError("duplicate column name: date_obtained")),
            ("mysql", ProgrammingError("Duplicate column name 'date_obtained'", errno=1060)),
            ("mariadb", Exception(1060, "Duplicate column name 'date_obtained'")),
        ]
        for dialect_name, original in errors:
            with self.subTest(dialect=dialect_name):
                engine, connection = self.mock_engine(dialect_name)
                connection.exec_driver_sql.side_effect = OperationalError("ALTER TABLE", {}, original)
                inspector = Mock()
                inspector.get_columns.side_effect = [
                    [{"name": "year_obtained"}],
                    [{"name": "year_obtained"}, {"name": "date_obtained"}],
                    [{"name": "year_obtained"}, {"name": "date_obtained"}],
                ]
                with patch("app.schema.inspect", return_value=inspector):
                    ensure_qualification_date_columns(engine)
                self.assertEqual(connection.exec_driver_sql.call_count, 1)
                self.assertEqual(inspector.get_columns.call_count, 3)

    def test_duplicate_error_without_the_new_column_is_not_hidden(self):
        engine, connection = self.mock_engine()
        failure = OperationalError("ALTER TABLE", {}, Exception(1060, "Duplicate column name 'date_obtained'"))
        connection.exec_driver_sql.side_effect = failure
        inspector = Mock()
        inspector.get_columns.return_value = [{"name": "year_obtained"}]
        with patch("app.schema.inspect", return_value=inspector), self.assertRaises(OperationalError) as caught:
            ensure_qualification_date_columns(engine)
        self.assertIs(caught.exception, failure)
        self.assertEqual(inspector.get_columns.call_count, 2)

    def test_permission_lock_and_unrelated_column_errors_are_not_hidden(self):
        errors = [
            ("mysql", Exception(1142, "ALTER command denied")),
            ("mariadb", Exception(1205, "Lock wait timeout exceeded")),
            ("sqlite", sqlite3.OperationalError("database is locked")),
            ("sqlite", sqlite3.OperationalError("duplicate column name: other_column")),
        ]
        for dialect_name, original in errors:
            with self.subTest(dialect=dialect_name, message=str(original)):
                engine, connection = self.mock_engine(dialect_name)
                failure = OperationalError("ALTER TABLE", {}, original)
                connection.exec_driver_sql.side_effect = failure
                inspector = Mock()
                inspector.get_columns.side_effect = [
                    [{"name": "year_obtained"}],
                    [{"name": "year_obtained"}, {"name": "date_obtained"}],
                ]
                with patch("app.schema.inspect", return_value=inspector), self.assertRaises(OperationalError) as caught:
                    ensure_qualification_date_columns(engine)
                self.assertIs(caught.exception, failure)
                self.assertEqual(inspector.get_columns.call_count, 1)


if __name__ == "__main__":
    unittest.main()
