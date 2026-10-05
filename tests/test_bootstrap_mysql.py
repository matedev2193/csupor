"""Isolated MySQL bootstrap failures, quoting and worker-lock lifecycle tests."""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy import make_url
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from app.bootstrap import (
    _ensure_mysql_database,
    _mysql_bootstrap_lock,
    initialise_database,
)


def database_error(code, *, errno=True):
    original = Exception(code, "Database operation failed")
    if errno:
        original.errno = code
    return OperationalError(None, None, original)


def engine_fixture(database="csupor"):
    engine = MagicMock()
    engine.url = make_url(
        "mysql+mysqlconnector://installer:private-password@db.example:3307/csupor"
        "?ssl_ca=%2Fcerts%2Fca.pem&charset=latin1"
    ).set(database=database)
    engine.dialect = mysql.dialect()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar.return_value = 1
    return engine, connection


class MySQLDatabaseCreationTests(unittest.TestCase):
    def test_missing_database_name_is_rejected_before_connecting(self):
        for name in (None, ""):
            with self.subTest(name=name):
                engine, _ = engine_fixture()
                engine.url = engine.url._replace(database=name)
                with patch("app.bootstrap.create_engine") as create:
                    with self.assertRaisesRegex(RuntimeError, "database name"):
                        _ensure_mysql_database(engine)
                engine.connect.assert_not_called()
                create.assert_not_called()

    def test_existing_database_does_not_require_database_creation_privilege(self):
        engine, _ = engine_fixture()
        with patch("app.bootstrap.create_engine") as create:
            _ensure_mysql_database(engine)
        create.assert_not_called()
        engine.connect.assert_called_once_with()

    def test_missing_database_uses_same_credentials_and_nonpooled_server_connection(self):
        for errno in (True, False):
            with self.subTest(errno_attribute=errno):
                engine, _ = engine_fixture()
                engine.connect.side_effect = database_error(1049, errno=errno)
                with patch("app.bootstrap.create_engine") as create:
                    server = create.return_value
                    _ensure_mysql_database(engine)
                url = create.call_args.args[0]
                self.assertEqual(url.drivername, engine.url.drivername)
                self.assertEqual(url.username, engine.url.username)
                self.assertEqual(url.password, engine.url.password)
                self.assertEqual(url.host, engine.url.host)
                self.assertEqual(url.port, engine.url.port)
                self.assertIsNone(url.database)
                self.assertEqual(url.query["ssl_ca"], "/certs/ca.pem")
                self.assertEqual(url.query["charset"], "utf8mb4")
                self.assertIs(create.call_args.kwargs["poolclass"], NullPool)
                server.begin.return_value.__enter__.return_value.exec_driver_sql.assert_called_once_with(
                    "CREATE DATABASE IF NOT EXISTS `csupor` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                server.dispose.assert_called_once_with()
                self.assertEqual(engine.url.database, "csupor")
                self.assertEqual(engine.url.query["charset"], "latin1")

    def test_database_identifier_is_quoted_as_one_identifier(self):
        engine, _ = engine_fixture("fresh`; DROP TABLE users; --")
        engine.connect.side_effect = database_error(1049)
        with patch("app.bootstrap.create_engine") as create:
            _ensure_mysql_database(engine)
        connection = create.return_value.begin.return_value.__enter__.return_value
        connection.exec_driver_sql.assert_called_once_with(
            "CREATE DATABASE IF NOT EXISTS `fresh``; DROP TABLE users; --` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )

    def test_authentication_connectivity_and_other_errors_never_trigger_creation(self):
        for code in (1045, 1044, 2003, 2013, 1142):
            for errno in (True, False):
                with self.subTest(code=code, errno_attribute=errno):
                    engine, _ = engine_fixture()
                    failure = database_error(code, errno=errno)
                    engine.connect.side_effect = failure
                    with patch("app.bootstrap.create_engine") as create:
                        with self.assertRaises(OperationalError) as caught:
                            _ensure_mysql_database(engine)
                    self.assertIs(caught.exception, failure)
                    create.assert_not_called()

    def test_creation_permission_error_gives_hosting_panel_guidance_without_secrets(self):
        for code in (1044, 1045, 1142, 1227):
            with self.subTest(code=code):
                engine, _ = engine_fixture()
                engine.connect.side_effect = database_error(1049)
                with patch("app.bootstrap.create_engine") as create:
                    server = create.return_value
                    server.begin.return_value.__enter__.return_value.exec_driver_sql.side_effect = database_error(code)
                    with self.assertRaises(RuntimeError) as caught:
                        _ensure_mysql_database(engine)
                message = str(caught.exception)
                self.assertIn("hosting control panel", message)
                self.assertIn("automatically", message)
                self.assertNotIn("private-password", message)
                self.assertNotIn("installer", message)
                self.assertNotIn("db.example", message)
                self.assertTrue(caught.exception.__suppress_context__)
                server.dispose.assert_called_once_with()

    def test_unrelated_creation_error_is_not_hidden_and_server_engine_is_disposed(self):
        engine, _ = engine_fixture()
        engine.connect.side_effect = database_error(1049)
        failure = database_error(2003)
        with patch("app.bootstrap.create_engine") as create:
            server = create.return_value
            server.begin.return_value.__enter__.side_effect = failure
            with self.assertRaises(OperationalError) as caught:
                _ensure_mysql_database(engine)
        self.assertIs(caught.exception, failure)
        server.dispose.assert_called_once_with()


class MySQLBootstrapLockTests(unittest.TestCase):
    def test_lock_name_is_bounded_stable_database_specific_and_bound_as_a_parameter(self):
        names = []
        for database in ("ő" * 100, "ő" * 100, "other"):
            engine, connection = engine_fixture(database)
            with _mysql_bootstrap_lock(engine):
                self.assertEqual(connection.execute.call_count, 1)
            acquire, release = connection.execute.call_args_list
            name = acquire.args[1]["name"]
            names.append(name)
            self.assertLessEqual(len(name), 64)
            self.assertTrue(name.isascii())
            self.assertEqual(acquire.args[1]["timeout"], 30)
            self.assertEqual(release.args[1], {"name": name})
            self.assertEqual(
                str(acquire.args[0].compile(dialect=mysql.dialect())),
                "SELECT GET_LOCK(%s, %s)",
            )
            self.assertEqual(
                str(release.args[0].compile(dialect=mysql.dialect())),
                "SELECT RELEASE_LOCK(%s)",
            )
            connection.invalidate.assert_not_called()
            engine.connect.assert_called_once_with()
        self.assertEqual(names[0], names[1])
        self.assertNotEqual(names[0], names[2])

    def test_ddl_failure_still_releases_lock_before_connection_returns_to_pool(self):
        engine, connection = engine_fixture()
        failure = database_error(1142)
        with self.assertRaises(OperationalError) as caught:
            with _mysql_bootstrap_lock(engine):
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertEqual(connection.execute.call_count, 2)
        self.assertIn("RELEASE_LOCK", str(connection.execute.call_args.args[0]))
        connection.invalidate.assert_not_called()
        engine.connect.return_value.__exit__.assert_called_once()

    def test_timeout_prevents_schema_work_and_does_not_release_an_unowned_lock(self):
        engine, connection = engine_fixture()
        connection.execute.return_value.scalar.return_value = 0
        body = Mock()
        with self.assertRaisesRegex(RuntimeError, "30 seconds"):
            with _mysql_bootstrap_lock(engine):
                body()
        body.assert_not_called()
        self.assertEqual(connection.execute.call_count, 1)
        connection.invalidate.assert_not_called()

    def test_unknown_acquisition_result_discards_connection(self):
        engine, connection = engine_fixture()
        connection.execute.return_value.scalar.return_value = None
        with self.assertRaises(RuntimeError):
            with _mysql_bootstrap_lock(engine):
                self.fail("A missing lock must block startup")
        connection.invalidate.assert_called_once_with()

    def test_acquisition_transport_failure_discards_potential_lock_holding_connection(self):
        engine, connection = engine_fixture()
        failure = database_error(2013)
        connection.execute.side_effect = failure
        with self.assertRaises(OperationalError) as caught:
            with _mysql_bootstrap_lock(engine):
                self.fail("A failed lock must block startup")
        self.assertIs(caught.exception, failure)
        connection.invalidate.assert_called_once_with()

    def test_release_failure_discards_connection_and_fails_startup(self):
        for release in (0, None, database_error(2013)):
            with self.subTest(release=type(release).__name__):
                engine, connection = engine_fixture()
                connection.execute.return_value.scalar.side_effect = [1, release]
                with self.assertRaises((RuntimeError, OperationalError)):
                    with _mysql_bootstrap_lock(engine):
                        pass
                connection.invalidate.assert_called_once_with()

    def test_release_failure_does_not_replace_original_ddl_error(self):
        engine, connection = engine_fixture()
        failure = database_error(1142)
        connection.execute.return_value.scalar.side_effect = [1, database_error(2013)]
        with self.assertRaises(OperationalError) as caught:
            with _mysql_bootstrap_lock(engine):
                raise failure
        self.assertIs(caught.exception, failure)
        connection.invalidate.assert_called_once_with()


class BootstrapOrderingTests(unittest.TestCase):
    def test_mysql_lock_covers_every_schema_and_default_initialiser(self):
        engine, connection = engine_fixture()
        events = []

        def execute(statement, parameters):
            events.append("acquire" if "GET_LOCK" in str(statement) else "release")
            return SimpleNamespace(scalar=lambda: 1)

        connection.execute.side_effect = execute
        with ExitStack() as stack:
            stack.enter_context(patch("app.bootstrap.db", SimpleNamespace(engine=engine, metadata="metadata")))
            stack.enter_context(patch("app.bootstrap._ensure_mysql_database", side_effect=lambda value: events.append("database")))
            for target, label in (
                ("app.bootstrap.create_missing_tables", "tables"),
                ("app.bootstrap.ensure_qualification_date_columns", "dates"),
                ("app.bootstrap.ensure_work_assignment_flexible_shift_column", "shifts"),
                ("app.leave_approval.initialise_leave_approval_settings", "approvals"),
                ("app.page_access.initialise_page_access_settings", "access"),
            ):
                stack.enter_context(patch(target, side_effect=lambda *args, label=label: events.append(label)))
            initialise_database()
        self.assertEqual(events, ["database", "acquire", "tables", "dates", "shifts", "approvals", "access", "release"])

    def test_sqlite_uses_schema_helpers_without_mysql_creation_or_named_locks(self):
        engine, _ = engine_fixture()
        engine.dialect = SimpleNamespace(name="sqlite")
        with ExitStack() as stack:
            stack.enter_context(patch("app.bootstrap.db", SimpleNamespace(engine=engine, metadata="metadata")))
            create = stack.enter_context(patch("app.bootstrap._ensure_mysql_database"))
            lock = stack.enter_context(patch("app.bootstrap._mysql_bootstrap_lock"))
            helpers = [stack.enter_context(patch(target)) for target in (
                "app.bootstrap.create_missing_tables",
                "app.bootstrap.ensure_qualification_date_columns",
                "app.bootstrap.ensure_work_assignment_flexible_shift_column",
                "app.leave_approval.initialise_leave_approval_settings",
                "app.page_access.initialise_page_access_settings",
            )]
            initialise_database()
        create.assert_not_called()
        lock.assert_not_called()
        for helper in helpers:
            helper.assert_called_once()


if __name__ == "__main__":
    unittest.main()
