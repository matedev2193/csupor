"""Page-access schema preserves saved permissions and deployed identifier types."""

import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from flask import Flask
from sqlalchemy import create_mock_engine, event, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError

from app import db
from app.account_deletion import delete_user_account
from app.models import User, UserPrivilege
from app.page_access_models import PageAccessSettings, PageRolePermission
from app.schema import create_missing_tables


ACCESS_MODELS = (PageAccessSettings, PageRolePermission)
ACCESS_TABLES = {model.__tablename__ for model in ACCESS_MODELS}


class PageAccessMySQLSchemaTests(unittest.TestCase):
    def compile_schema(self, existing, user_id_type=None):
        statements = []
        engine = create_mock_engine(
            "mysql+mysqlconnector://",
            lambda statement, *args, **kwargs: statements.append(
                str(statement.compile(dialect=mysql.dialect()))
            ),
        )
        inspector = Mock()
        inspector.get_table_names.return_value = existing
        inspector.get_columns.return_value = [{"name": "id", "type": user_id_type}]
        with patch("app.schema.inspect", return_value=inspector):
            create_missing_tables(engine, db.metadata)
        return statements, inspector

    def test_new_settings_foreign_key_matches_existing_identifier_type(self):
        existing = [name for name in db.metadata.tables if name not in ACCESS_TABLES]
        original_type = PageAccessSettings.__table__.c.updated_by_id.type
        for reference_type, expected in (
            (mysql.INTEGER(), "INTEGER"),
            (mysql.INTEGER(unsigned=True), "INTEGER UNSIGNED"),
            (mysql.BIGINT(), "BIGINT"),
            (mysql.BIGINT(unsigned=True), "BIGINT UNSIGNED"),
        ):
            with self.subTest(user_id_type=expected):
                statements, inspector = self.compile_schema(existing, reference_type)
                table_sql = [sql for sql in statements if "CREATE TABLE" in sql]
                self.assertEqual(len(table_sql), 2)
                settings_sql = next(sql for sql in table_sql if "CREATE TABLE page_access_settings " in sql)
                permissions_sql = next(sql for sql in table_sql if "CREATE TABLE page_role_permissions " in sql)
                self.assertIn(f"updated_by_id {expected}", settings_sql)
                self.assertIn("ON DELETE SET NULL", settings_sql)
                self.assertIn("CHECK (id = 1)", settings_sql)
                self.assertIn("PRIMARY KEY (page_key, `role`)", permissions_sql)
                self.assertIn("page_key VARCHAR(64) NOT NULL", permissions_sql)
                self.assertIn("`role` VARCHAR(20) NOT NULL", permissions_sql)
                self.assertIn("allowed BOOL NOT NULL", permissions_sql)
                inspector.get_columns.assert_called_once_with("users", schema=None)
                self.assertIs(PageAccessSettings.__table__.c.updated_by_id.type, original_type)

    def test_fresh_database_creates_users_before_access_settings(self):
        statements, inspector = self.compile_schema([])
        table_sql = [sql for sql in statements if "CREATE TABLE" in sql]
        users_index = next(i for i, sql in enumerate(table_sql) if "CREATE TABLE users " in sql)
        settings_index = next(i for i, sql in enumerate(table_sql) if "CREATE TABLE page_access_settings " in sql)
        self.assertLess(users_index, settings_index)
        inspector.get_columns.assert_not_called()

    def test_existing_access_tables_are_not_recreated_or_changed(self):
        statements, inspector = self.compile_schema(list(db.metadata.tables))
        self.assertEqual(statements, [])
        inspector.get_columns.assert_not_called()


class PageAccessPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.assertEqual(db.session.execute(text("PRAGMA foreign_keys")).scalar(), 1)
        create_missing_tables(db.engine, db.metadata)
        self.editor = User(username="access-editor", email="access-editor@example.invalid",
                           password_hash="unused", privilege=UserPrivilege.developer)
        self.other = User(username="other", email="other@example.invalid", password_hash="unused")
        db.session.add_all([self.editor, self.other])
        db.session.flush()
        self.editor_id = self.editor.id
        self.other_id = self.other.id
        self.settings = PageAccessSettings(
            id=1, revision="a" * 32, updated_at=datetime(2026, 10, 5, 18),
            updated_by_id=self.editor.id,
        )
        db.session.add_all([
            self.settings,
            PageRolePermission(page_key="dashboard", role="employee", allowed=False),
            PageRolePermission(page_key="dashboard", role="hr", allowed=True),
            PageRolePermission(page_key="contracts", role="employee", allowed=True),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def rows(self, model):
        return [dict(row) for row in db.session.execute(
            model.__table__.select().order_by(*model.__table__.primary_key.columns)
        ).mappings()]

    def test_repeated_startup_retains_explicit_grants_denials_and_revision(self):
        before = {model.__tablename__: self.rows(model) for model in ACCESS_MODELS}
        statements = []

        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", collect)
        try:
            create_missing_tables(db.engine, db.metadata)
            create_missing_tables(db.engine, db.metadata)
        finally:
            event.remove(db.engine, "before_cursor_execute", collect)
        self.assertFalse(any(sql.lstrip().upper().startswith(
            ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ")
        ) for sql in statements))
        db.session.remove()
        self.assertEqual({model.__tablename__: self.rows(model) for model in ACCESS_MODELS}, before)

    def test_one_permission_per_page_and_role_while_other_pairs_remain_independent(self):
        before = self.rows(PageRolePermission)
        db.session.add(PageRolePermission(page_key="dashboard", role="employee", allowed=True))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        self.assertEqual(self.rows(PageRolePermission), before)

    def test_settings_singleton_constraint_prevents_second_configuration(self):
        before = self.rows(PageAccessSettings)
        db.session.add(PageAccessSettings(id=2, revision="b" * 32))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        self.assertEqual(self.rows(PageAccessSettings), before)

    def test_unknown_role_is_rejected_without_replacing_existing_permissions(self):
        before = self.rows(PageRolePermission)
        db.session.add(PageRolePermission(page_key="dashboard", role="administrator", allowed=True))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        self.assertEqual(self.rows(PageRolePermission), before)

    def test_database_user_deletion_clears_attribution_and_keeps_permissions(self):
        before_permissions = self.rows(PageRolePermission)
        before_settings = self.rows(PageAccessSettings)[0]
        db.session.execute(User.__table__.delete().where(User.id == self.editor_id))
        db.session.commit()
        db.session.expire_all()
        self.assertIsNone(db.session.get(User, self.editor_id))
        self.assertIsNotNone(db.session.get(User, self.other_id))
        self.assertEqual(self.rows(PageRolePermission), before_permissions)
        self.assertEqual(self.rows(PageAccessSettings), [dict(before_settings, updated_by_id=None)])

    def test_account_deletion_preserves_permissions_and_shared_configuration(self):
        before_permissions = self.rows(PageRolePermission)
        before_settings = self.rows(PageAccessSettings)[0]
        delete_user_account(self.editor)
        db.session.commit()
        db.session.expire_all()
        self.assertIsNone(db.session.get(User, self.editor_id))
        self.assertIsNotNone(db.session.get(User, self.other_id))
        self.assertEqual(self.rows(PageRolePermission), before_permissions)
        after_settings = self.rows(PageAccessSettings)[0]
        self.assertIsNone(after_settings["updated_by_id"])
        self.assertEqual(after_settings["updated_at"], before_settings["updated_at"])
        self.assertEqual(after_settings["id"], 1)

    def test_rolled_back_deletion_restores_attribution_and_permissions(self):
        before = {model.__tablename__: self.rows(model) for model in ACCESS_MODELS}
        delete_user_account(self.editor)
        db.session.flush()
        self.assertIsNone(db.session.get(PageAccessSettings, 1).updated_by_id)
        db.session.rollback()
        self.assertIsNotNone(db.session.get(User, self.editor_id))
        self.assertEqual({model.__tablename__: self.rows(model) for model in ACCESS_MODELS}, before)


if __name__ == "__main__":
    unittest.main()
