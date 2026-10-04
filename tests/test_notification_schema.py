"""Mail schema compatibility and account-deletion integration coverage."""

import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

from flask import Flask
from sqlalchemy import create_mock_engine, event, text
from sqlalchemy.dialects import mysql

from app import db
from app.account_deletion import delete_user_account
from app.mail_settings_models import MailServerSettings
from app.models import (
    Contract, ContractType, LegalEntity, LeaveRequest, LeaveRequestCategory,
    LeaveRequestStatus, PlaceOfWork, User,
)
from app.notification_models import LeaveNotification, MailBatch
from app.schema import create_missing_tables


MAIL_MODELS = (MailServerSettings, MailBatch, LeaveNotification)
MAIL_TABLES = {model.__tablename__ for model in MAIL_MODELS}


class NotificationMySQLSchemaTests(unittest.TestCase):
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
            for model in MAIL_MODELS
        }
        return statements, tables, inspector

    def test_mail_tables_match_existing_integer_size_and_signedness_without_mutating_orm(self):
        existing = [name for name in db.metadata.tables if name not in MAIL_TABLES]
        original_types = {
            column: column.type
            for model in MAIL_MODELS for column in model.__table__.columns
            if column.foreign_keys
        }
        for reference_type, expected in (
            (mysql.INTEGER(), "INTEGER"),
            (mysql.INTEGER(unsigned=True), "INTEGER UNSIGNED"),
            (mysql.BIGINT(), "BIGINT"),
            (mysql.BIGINT(unsigned=True), "BIGINT UNSIGNED"),
        ):
            with self.subTest(identifier_type=expected):
                statements, tables, inspector = self.compile_schema(
                    existing, {name: reference_type for name in ("users", "leave_requests")},
                )
                self.assertEqual(sum("CREATE TABLE" in sql for sql in statements), 3)
                self.assertTrue(all(tables.values()))
                for table, columns in {
                    "mail_server_settings": ("updated_by_id",),
                    "mail_batches": ("recipient_id",),
                    "leave_notifications": ("recipient_id", "leave_request_id"),
                }.items():
                    for column in columns:
                        self.assertIn(f"{column} {expected}", tables[table])
                self.assertIn("batch_id VARCHAR(32)", tables["leave_notifications"])
                self.assertIn("ON DELETE SET NULL", tables["mail_server_settings"])
                self.assertIn("ON DELETE CASCADE", tables["mail_batches"])
                self.assertIn("ON DELETE CASCADE", tables["leave_notifications"])
                reflected = [call.args[0] for call in inspector.get_columns.call_args_list]
                self.assertCountEqual(reflected, ["users", "leave_requests"])
                for column, original in original_types.items():
                    self.assertIs(column.type, original)

    def test_mixed_existing_identifier_types_are_reflected_independently(self):
        existing = [name for name in db.metadata.tables if name != "leave_notifications"]
        statements, tables, inspector = self.compile_schema(existing, {
            "users": mysql.BIGINT(unsigned=True),
            "leave_requests": mysql.INTEGER(),
        })
        self.assertEqual(sum("CREATE TABLE" in sql for sql in statements), 1)
        self.assertIsNone(tables["mail_batches"])
        self.assertIsNone(tables["mail_server_settings"])
        self.assertIn("recipient_id BIGINT UNSIGNED NOT NULL", tables["leave_notifications"])
        self.assertIn("leave_request_id INTEGER NOT NULL", tables["leave_notifications"])
        self.assertIn("batch_id VARCHAR(32)", tables["leave_notifications"])
        reflected = [call.args[0] for call in inspector.get_columns.call_args_list]
        self.assertCountEqual(reflected, ["users", "leave_requests"])

    def test_fresh_schema_creates_mail_dependencies_first(self):
        statements, _, inspector = self.compile_schema([], {})
        table_sql = [sql for sql in statements if "CREATE TABLE" in sql]
        positions = {
            name: next(index for index, sql in enumerate(table_sql) if f"CREATE TABLE {name} " in sql)
            for name in (*MAIL_TABLES, "users", "leave_requests")
        }
        for parent, child in (
            ("users", "mail_server_settings"), ("users", "mail_batches"),
            ("users", "leave_notifications"), ("leave_requests", "leave_notifications"),
            ("mail_batches", "leave_notifications"),
        ):
            self.assertLess(positions[parent], positions[child])
        inspector.get_columns.assert_not_called()

    def test_completed_schema_has_no_ddl_or_identifier_reflection(self):
        statements, _, inspector = self.compile_schema(list(db.metadata.tables), {})
        self.assertEqual(statements, [])
        inspector.get_columns.assert_not_called()


class NotificationPersistenceTests(unittest.TestCase):
    def setUp(self):
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
        place = PlaceOfWork(legal_entity=entity, address="Example")
        contracts = [Contract(
            user=user, employer=entity, place_of_work=place,
            contract_type=ContractType.teacher, start_date=date(2026, 1, 1),
            job_title="Teacher", working_hours_per_week=40,
        ) for user in (self.target, self.other)]
        self.leaves = [LeaveRequest(
            user=user, contract=contract, category=LeaveRequestCategory.paid_leave,
            start_date=date(2026, 10, 8), end_date=date(2026, 10, 9),
            status=LeaveRequestStatus.approved,
        ) for user, contract in zip((self.target, self.other), contracts)]
        self.settings = MailServerSettings(
            id=1, host="smtp.example.invalid", sender_email="csupor@example.invalid",
            encrypted_password="unchanged-encrypted-secret", updated_by=self.target,
        )
        self.batches = [MailBatch(
            id=str(index) * 32, batch_key=f"digest:{index}:2026-10-04", recipient=user,
            kind="digest", due_at=datetime(2026, 10, 4, 18),
        ) for index, user in enumerate((self.target, self.other), start=1)]
        db.session.add_all([*self.leaves, self.settings, *self.batches])
        db.session.flush()
        # Each inbox contains the user's own request and the other user's request.
        # Deleting either ownership source must remove its corresponding messages.
        self.notifications = [LeaveNotification(
            event_key=str(index) * 32, recipient=user, leave_request=leave_request,
            event_type="approved", payload={"applicant_name": leave_request.user.username},
            is_owner=user is leave_request.user,
            due_at=datetime(2026, 10, 4, 18), batch=batch,
        ) for index, (user, leave_request, batch) in enumerate((
            (self.target, self.leaves[0], self.batches[0]),
            (self.target, self.leaves[1], self.batches[0]),
            (self.other, self.leaves[0], self.batches[1]),
            (self.other, self.leaves[1], self.batches[1]),
        ), start=1)]
        db.session.add_all(self.notifications)
        db.session.commit()
        self.target_id, self.other_id = self.target.id, self.other.id
        self.leave_ids = [leave.id for leave in self.leaves]
        self.batch_ids = [batch.id for batch in self.batches]
        self.notification_ids = [notification.id for notification in self.notifications]

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def mail_rows(self):
        return {
            model.__tablename__: [dict(row) for row in db.session.execute(
                model.__table__.select().order_by(model.__table__.c.id)
            ).mappings()]
            for model in MAIL_MODELS
        }

    def test_repeated_sqlite_startup_keeps_mail_settings_batches_and_notifications(self):
        before = self.mail_rows()
        statements = []

        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", collect)
        try:
            create_missing_tables(db.engine, db.metadata)
            create_missing_tables(db.engine, db.metadata)
        finally:
            event.remove(db.engine, "before_cursor_execute", collect)
        self.assertFalse(any(sql.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP ")) for sql in statements))
        self.assertEqual(self.mail_rows(), before)

    def test_account_deletion_cleans_both_notification_ownerships_and_preserves_shared_settings(self):
        remaining_before = dict(db.session.execute(LeaveNotification.__table__.select().where(
            LeaveNotification.id == self.notification_ids[3]
        )).mappings().one())
        delete_user_account(self.target)
        db.session.commit()
        db.session.expire_all()
        self.assertIsNone(db.session.get(User, self.target_id))
        self.assertIsNotNone(db.session.get(User, self.other_id))
        self.assertIsNone(db.session.get(LeaveRequest, self.leave_ids[0]))
        self.assertIsNotNone(db.session.get(LeaveRequest, self.leave_ids[1]))
        self.assertIsNone(db.session.get(MailBatch, self.batch_ids[0]))
        self.assertIsNotNone(db.session.get(MailBatch, self.batch_ids[1]))
        self.assertEqual(MailBatch.query.count(), 1)
        self.assertEqual(LeaveNotification.query.count(), 1)
        for notification_id in self.notification_ids[:3]:
            self.assertIsNone(db.session.get(LeaveNotification, notification_id))
        remaining_after = dict(db.session.execute(LeaveNotification.__table__.select().where(
            LeaveNotification.id == self.notification_ids[3]
        )).mappings().one())
        self.assertEqual(remaining_after, remaining_before)
        settings = db.session.get(MailServerSettings, 1)
        self.assertIsNone(settings.updated_by_id)
        self.assertEqual(settings.host, "smtp.example.invalid")
        self.assertEqual(settings.encrypted_password, "unchanged-encrypted-secret")

    def test_rollback_restores_notification_ownerships_and_settings_attribution(self):
        before = self.mail_rows()
        delete_user_account(self.target)
        db.session.flush()
        self.assertEqual(LeaveNotification.query.count(), 1)
        db.session.rollback()
        self.assertIsNotNone(db.session.get(User, self.target_id))
        self.assertEqual(self.mail_rows(), before)


if __name__ == "__main__":
    unittest.main()
