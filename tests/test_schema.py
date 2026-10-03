"""Regression coverage for SQL-script and ORM-created MySQL installations."""

import unittest
from unittest.mock import Mock, patch

from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, create_engine, create_mock_engine, inspect
from sqlalchemy.dialects import mysql

from app import db
from app import models  # Register the application's real table definitions.
from app.schema import create_missing_tables


class MissingTableTests(unittest.TestCase):
    def compile_missing_tables(self, metadata, existing_tables, column_types):
        statements = []
        engine = create_mock_engine(
            "mysql+mysqlconnector://",
            lambda statement, *args, **kwargs: statements.append(
                str(statement.compile(dialect=mysql.dialect()))
            ),
        )
        inspector = Mock()
        inspector.get_table_names.return_value = existing_tables
        inspector.get_columns.side_effect = lambda name, schema=None: [
            {"name": key, "type": value} for key, value in column_types[name].items()
        ]
        with patch("app.schema.inspect", return_value=inspector):
            create_missing_tables(engine, metadata)
        return statements, inspector

    def test_leave_years_matches_existing_user_id_and_keeps_models_unchanged(self):
        original_type = models.LeaveYear.__table__.c.imported_by_id.type
        existing = [name for name in db.metadata.tables if name != "leave_years"]
        for reference_type, expected in [
            (mysql.INTEGER(unsigned=True), "INTEGER UNSIGNED"),
            (mysql.INTEGER(), "INTEGER"),
            (mysql.BIGINT(unsigned=True), "BIGINT UNSIGNED"),
        ]:
            with self.subTest(reference_type=expected):
                statements, inspector = self.compile_missing_tables(
                    db.metadata, existing, {"users": {"id": reference_type}}
                )
                self.assertEqual(len(statements), 1)
                self.assertIn("CREATE TABLE leave_years", statements[0])
                self.assertIn(f"imported_by_id {expected},", statements[0])
                self.assertIn("FOREIGN KEY(imported_by_id) REFERENCES users (id)", statements[0])
                inspector.get_columns.assert_called_once_with("users", schema=None)
                self.assertIs(models.LeaveYear.__table__.c.imported_by_id.type, original_type)

    def test_multiple_new_user_foreign_keys_reuse_one_reflection(self):
        metadata = MetaData()
        Table("users", metadata, Column("id", Integer, primary_key=True))
        Table(
            "review", metadata, Column("id", Integer, primary_key=True),
            Column("owner_id", Integer, ForeignKey("users.id")),
            Column("reviewer_id", Integer, ForeignKey("users.id")),
        )
        statements, inspector = self.compile_missing_tables(
            metadata, ["users"], {"users": {"id": mysql.INTEGER(unsigned=True)}}
        )
        self.assertIn("owner_id INTEGER UNSIGNED", statements[0])
        self.assertIn("reviewer_id INTEGER UNSIGNED", statements[0])
        inspector.get_columns.assert_called_once_with("users", schema=None)

    def test_approval_settings_matches_existing_user_id(self):
        existing = [name for name in db.metadata.tables if name != "leave_approval_settings"]
        for reference_type, expected in [
            (mysql.INTEGER(unsigned=True), "INTEGER UNSIGNED"),
            (mysql.INTEGER(), "INTEGER"),
        ]:
            with self.subTest(reference_type=expected):
                statements, inspector = self.compile_missing_tables(
                    db.metadata, existing, {"users": {"id": reference_type}}
                )
                self.assertEqual(len(statements), 1)
                self.assertIn("CREATE TABLE leave_approval_settings", statements[0])
                self.assertIn(f"updated_by_id {expected},", statements[0])
                self.assertIn("FOREIGN KEY(updated_by_id) REFERENCES users (id)", statements[0])

    def test_fresh_mysql_database_has_compatible_foreign_keys(self):
        statements, inspector = self.compile_missing_tables(db.metadata, [], {})
        table_statements = [sql for sql in statements if "CREATE TABLE" in sql]
        self.assertEqual(len(table_statements), len(db.metadata.tables))
        users_index = next(i for i, sql in enumerate(table_statements) if "CREATE TABLE users" in sql)
        years_index = next(i for i, sql in enumerate(table_statements) if "CREATE TABLE leave_years" in sql)
        self.assertLess(users_index, years_index)
        leave_years = next(sql for sql in statements if "CREATE TABLE leave_years" in sql)
        self.assertIn("imported_by_id INTEGER,", leave_years)
        inspector.get_columns.assert_not_called()

    def test_gyap_forms_uses_mediumblob_and_matches_existing_user_ids(self):
        existing = [name for name in db.metadata.tables if name != "gyap_forms"]
        original_type = models.GyapForm.__table__.c.uploaded_by_id.type
        for reference_type, expected in [
            (mysql.INTEGER(unsigned=True), "INTEGER UNSIGNED"),
            (mysql.INTEGER(), "INTEGER"),
        ]:
            with self.subTest(reference_type=expected):
                statements, inspector = self.compile_missing_tables(
                    db.metadata, existing, {"users": {"id": reference_type}}
                )
                self.assertEqual(len(statements), 1)
                self.assertIn("CREATE TABLE gyap_forms", statements[0])
                self.assertIn("data MEDIUMBLOB NOT NULL", statements[0])
                self.assertIn(f"uploaded_by_id {expected},", statements[0])
                self.assertIn("FOREIGN KEY(uploaded_by_id) REFERENCES users (id) ON DELETE SET NULL", statements[0])
                self.assertIs(models.GyapForm.__table__.c.uploaded_by_id.type, original_type)

    def test_completed_schema_is_not_recreated_or_altered(self):
        statements, inspector = self.compile_missing_tables(
            db.metadata, list(db.metadata.tables), {}
        )
        self.assertEqual(statements, [])
        inspector.get_columns.assert_not_called()

    def test_sqlite_startup_is_idempotent_and_preserves_records(self):
        engine = create_engine("sqlite://")
        try:
            create_missing_tables(engine, db.metadata)
            with engine.begin() as connection:
                connection.execute(models.LeaveYear.__table__.insert(), {"year": 2026, "is_open": True})
            create_missing_tables(engine, db.metadata)
            self.assertIn("leave_years", inspect(engine).get_table_names())
            with engine.connect() as connection:
                self.assertEqual(connection.execute(models.LeaveYear.__table__.select()).one().year, 2026)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
