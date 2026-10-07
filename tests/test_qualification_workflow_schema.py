"""Legacy data, durable documents and unified-register schema regressions."""

import os
from pathlib import Path
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, Mock, patch

from mysql.connector.conversion import MySQLConverter
from sqlalchemy import create_mock_engine, event, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql.mysqlconnector import dialect as mysqlconnector_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app import create_app, db
from app.models import EducationalQualification, ProfessionalExam, User
from app.qualification_migration import migrate_legacy_qualifications
from app.qualification_models import QualificationDocument, QualificationRecord
from app.schema import create_missing_tables


class QualificationWorkflowMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(Path(self.directory.name) / "qualifications.db"),
            "SECRET_KEY": "qualification-migration-tests", "EMAIL_WORKER_ENABLED": "false",
        })
        self.environment.start()
        self.app = create_app()
        self.context = self.app.app_context()
        self.context.push()
        db.session.add(User(id=1, username="legacy", email="legacy@example.invalid", password_hash="unused"))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.environment.stop()
        self.directory.cleanup()

    def seed_legacy(self):
        db.session.add_all([
            EducationalQualification(
                id=14, user_id=1, level_or_type="BA", qualification_name="Óvodapedagógus",
                institution_name="Régi egyetem", degree_number="EDU/2016-1", year_obtained=2016,
                date_obtained=date(2016, 6, 24), highest=True,
            ),
            EducationalQualification(
                id=15, user_id=1, level_or_type="Tanúsítvány", qualification_name="Régi képesítés",
                institution_name="Képzőintézet", degree_number="OLD-1", year_obtained=2012,
                date_obtained=None, highest=False,
            ),
            ProfessionalExam(
                id=14, user_id=1, qualification_name="Szakvizsga", degree_number="EX/2020",
                year_obtained=2020, date_obtained=None,
            ),
        ])
        db.session.commit()

    def legacy_rows(self):
        return {
            model.__tablename__: [dict(row) for row in db.session.execute(model.__table__.select()).mappings()]
            for model in (EducationalQualification, ProfessionalExam)
        }

    def test_lossless_import_preserves_both_sources_and_year_only_dates(self):
        self.seed_legacy()
        before = self.legacy_rows()
        db.session.remove()
        migrate_legacy_qualifications(db.engine)
        records = QualificationRecord.query.order_by(QualificationRecord.id).all()
        self.assertEqual(len(records), 3)
        self.assertEqual(self.legacy_rows(), before)
        by_source = {(row.legacy_source, row.legacy_id): row for row in records}
        for source, original_rows in before.items():
            for original in original_rows:
                row = by_source[(source, original["id"])]
                for key, value in original.items():
                    if key != "id":
                        self.assertEqual(getattr(row, key), value, key)
                self.assertEqual(row.status, "processed")
                self.assertEqual(row.completion_state, "completed")
                self.assertEqual(row.revision, 1)
                self.assertIsNone(row.study_categories)
                self.assertEqual(row.documents, [])
        exam = by_source[("professional_exams", 14)]
        self.assertEqual(exam.kind, "exam")
        self.assertEqual(exam.award_categories, ["professional_exam"])
        self.assertIsNone(exam.institution_name)
        self.assertIsNone(exam.date_obtained)
        self.assertIsNone(by_source[("educational_qualifications", 14)].award_categories)

    def test_restarts_never_duplicate_or_overwrite_hr_edits(self):
        self.seed_legacy()
        db.session.remove()
        migrate_legacy_qualifications(db.engine)
        record = QualificationRecord.query.filter_by(legacy_source="professional_exams").one()
        record.qualification_name = "HR corrected exam"
        record.date_obtained = date(2020, 8, 31)
        record.notes = "Verified original document"
        record.award_categories = ["professional_exam", "leadership_training"]
        db.session.commit()
        expected = dict(db.session.execute(QualificationRecord.__table__.select().where(
            QualificationRecord.id == record.id,
        )).mappings().one())
        db.session.remove()
        for _ in range(2):
            restarted = create_app()
            with restarted.app_context():
                actual = dict(db.session.execute(QualificationRecord.__table__.select().where(
                    QualificationRecord.id == expected["id"],
                )).mappings().one())
                self.assertEqual(actual, expected)
                self.assertEqual(QualificationRecord.query.count(), 3)
                db.session.remove()
                db.engine.dispose()

    def test_failure_in_second_source_rolls_back_first_source_import(self):
        self.seed_legacy()
        db.session.remove()

        def fail_exam(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO qualification_records") and "FROM professional_exams" in statement:
                raise RuntimeError("simulated second-source failure")

        event.listen(db.engine, "before_cursor_execute", fail_exam)
        try:
            with self.assertRaisesRegex(RuntimeError, "second-source"):
                migrate_legacy_qualifications(db.engine)
        finally:
            event.remove(db.engine, "before_cursor_execute", fail_exam)
        self.assertEqual(QualificationRecord.query.count(), 0)
        self.assertEqual(EducationalQualification.query.count(), 2)
        self.assertEqual(ProfessionalExam.query.count(), 1)
        db.session.remove()
        migrate_legacy_qualifications(db.engine)
        self.assertEqual(QualificationRecord.query.count(), 3)

    def test_multiple_new_exams_are_supported_but_legacy_pair_cannot_duplicate(self):
        db.session.add_all([
            QualificationRecord(user_id=1, status="processed", kind="exam", qualification_name="First exam"),
            QualificationRecord(user_id=1, status="processed", kind="exam", qualification_name="Second exam"),
        ])
        db.session.commit()
        self.assertEqual(QualificationRecord.query.filter_by(kind="exam").count(), 2)
        db.session.add(QualificationRecord(user_id=1, legacy_source="professional_exams", legacy_id=14))
        db.session.commit()
        db.session.add(QualificationRecord(user_id=1, legacy_source="professional_exams", legacy_id=14))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        self.assertEqual(QualificationRecord.query.count(), 3)

    def test_owner_deletion_removes_records_documents_and_legacy_rows(self):
        self.seed_legacy()
        db.session.remove()
        migrate_legacy_qualifications(db.engine)
        record = QualificationRecord.query.first()
        record.documents.append(QualificationDocument(
            filename="certificate.pdf", mime_type="application/pdf", size_bytes=4, data=b"test", uploaded_by_id=1,
        ))
        db.session.commit()
        db.session.delete(db.session.get(User, 1))
        db.session.commit()
        for model in (QualificationRecord, QualificationDocument, EducationalQualification, ProfessionalExam):
            self.assertEqual(db.session.query(model).count(), 0)

    def test_actor_deletion_keeps_another_users_record_and_document(self):
        db.session.remove()
        with db.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        reviewer = User(username="reviewer", email="reviewer@example.invalid", password_hash="unused")
        db.session.add(reviewer)
        db.session.flush()
        record = QualificationRecord(user_id=1, processed_by_id=reviewer.id)
        record.documents.append(QualificationDocument(
            filename="certificate.pdf", mime_type="application/pdf", size_bytes=4,
            data=b"test", uploaded_by_id=reviewer.id,
        ))
        db.session.add(record)
        db.session.commit()
        db.session.delete(reviewer)
        db.session.commit()
        db.session.expire_all()
        self.assertIsNone(record.processed_by_id)
        self.assertIsNone(record.documents[0].uploaded_by_id)
        self.assertEqual(record.documents[0].data, b"test")

    def test_uploaded_document_defaults_and_blob_is_deferred(self):
        record = QualificationRecord(user_id=1)
        record.documents.append(QualificationDocument(
            filename="certificate.pdf", mime_type="application/pdf", size_bytes=4, data=b"test",
        ))
        db.session.add(record)
        db.session.commit()
        db.session.remove()
        record = QualificationRecord.query.one()
        self.assertEqual(record.status, "uploaded")
        self.assertEqual(record.revision, 1)
        self.assertIsNone(record.year_obtained)
        self.assertIsNone(record.kind)
        self.assertIsNone(record.processed_at)
        self.assertIn("data", inspect(record.documents[0]).unloaded)
        self.assertEqual(record.documents[0].data, b"test")

    def test_stale_orm_edits_cannot_silently_overwrite_saved_work(self):
        record = QualificationRecord(user_id=1)
        db.session.add(record)
        db.session.commit()
        record_id = record.id
        db.session.remove()
        with Session(db.engine) as first, Session(db.engine) as second:
            one = first.get(QualificationRecord, record_id)
            two = second.get(QualificationRecord, record_id)
            one.notes = "First review"
            first.commit()
            two.notes = "Stale second review"
            with self.assertRaises(StaleDataError):
                second.commit()
        self.assertEqual(db.session.get(QualificationRecord, record_id).notes, "First review")


class QualificationWorkflowMySQLSchemaTests(unittest.TestCase):
    def test_import_parameters_are_accepted_by_mysql_connector(self):
        # SQL compilation and SQLite both accept quoted_name, but the deployed
        # MySQL connector rejects this str subclass when it is a bound value.
        engine = MagicMock()
        migrate_legacy_qualifications(engine)
        calls = engine.begin.return_value.__enter__.return_value.execute.call_args_list
        self.assertEqual(len(calls), 2)
        converter = MySQLConverter()
        for call, source_name in zip(calls, ("educational_qualifications", "professional_exams")):
            with self.subTest(source=source_name):
                compiled = call.args[0].compile(dialect=mysqlconnector_dialect())
                # Apply the same dialect bind processors as execution would,
                # including JSON serialisation, before the real DBAPI converter.
                parameters = {
                    key: compiled._bind_processors[key](value)
                    if key in compiled._bind_processors else value
                    for key, value in compiled.params.items()
                }
                converted = [converter.to_mysql(value) for value in parameters.values()]
                # Both the inserted legacy_source and the duplicate guard must
                # keep their original value while becoming driver-compatible.
                self.assertEqual(converted.count(source_name.encode("utf-8")), 2)

    def test_import_avoids_mysql_target_table_subquery_restriction(self):
        # MySQL permits self INSERT ... SELECT joins, but rejects selecting the
        # INSERT target inside a NOT EXISTS subquery with error 1093.
        engine = MagicMock()
        migrate_legacy_qualifications(engine)
        calls = engine.begin.return_value.__enter__.return_value.execute.call_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            sql = str(call.args[0].compile(dialect=mysql.dialect()))
            self.assertIn("LEFT OUTER JOIN qualification_records AS imported ON", sql)
            self.assertIn("WHERE imported.id IS NULL", sql)
            self.assertNotIn("EXISTS", sql)
        manual = (Path(__file__).resolve().parents[1] / "sql" / "migrations" /
                  "2026-10-07-unify-qualification-documents.sql").read_text()
        imports = manual.split("START TRANSACTION;", 1)[1]
        self.assertEqual(imports.count("LEFT JOIN qualification_records r"), 2)
        self.assertEqual(imports.count("WHERE r.id IS NULL"), 2)
        self.assertNotIn("EXISTS", imports)

    def test_signed_unsigned_and_bigint_user_foreign_keys_match_and_blobs_are_durable(self):
        originals = [QualificationRecord.__table__.c.user_id.type, QualificationDocument.__table__.c.uploaded_by_id.type]
        for reference_type, expected in (
            (mysql.INTEGER(), "INTEGER"), (mysql.INTEGER(unsigned=True), "INTEGER UNSIGNED"),
            (mysql.BIGINT(unsigned=True), "BIGINT UNSIGNED"),
        ):
            with self.subTest(expected=expected):
                statements = []
                engine = create_mock_engine("mysql+mysqlconnector://", lambda statement, *args, **kwargs: statements.append(
                    str(statement.compile(dialect=mysql.dialect())),
                ))
                inspector = Mock()
                inspector.get_table_names.return_value = [
                    name for name in db.metadata.tables if name not in {"qualification_records", "qualification_documents"}
                ]
                inspector.get_columns.return_value = [{"name": "id", "type": reference_type}]
                with patch("app.schema.inspect", return_value=inspector):
                    create_missing_tables(engine, db.metadata)
                record_sql = next(sql for sql in statements if "CREATE TABLE qualification_records" in sql)
                document_sql = next(sql for sql in statements if "CREATE TABLE qualification_documents" in sql)
                self.assertIn(f"user_id {expected} NOT NULL", record_sql)
                self.assertIn(f"processed_by_id {expected}", record_sql)
                self.assertIn(f"uploaded_by_id {expected}", document_sql)
                self.assertIn("record_id INTEGER NOT NULL", document_sql)
                self.assertIn("data MEDIUMBLOB NOT NULL", document_sql)
                self.assertIn("UNIQUE (record_id)", document_sql)
                inspector.get_columns.assert_called_once_with("users", schema=None)
        self.assertIs(QualificationRecord.__table__.c.user_id.type, originals[0])
        self.assertIs(QualificationDocument.__table__.c.uploaded_by_id.type, originals[1])


if __name__ == "__main__":
    unittest.main()
