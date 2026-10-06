"""Initial administrator provisioning never turns public signup into account takeover."""

from datetime import date
import io
import logging
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, OperationalError

from app import create_app, db
from app.initial_admin import initialise_initial_admin
from app.models import Dependent, User, UserPrivilege, UserProfile
from app.page_access_models import PageAccessSettings, PageRolePermission


ADMIN_ENV = {
    "INITIAL_ADMIN_USERNAME": "installation-owner",
    "INITIAL_ADMIN_EMAIL": "owner@example.invalid",
    "INITIAL_ADMIN_PASSWORD": "A-private-bootstrap-password-123",
}


class InitialAdministratorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(Path(self.directory.name) / "app.db"),
            "SECRET_KEY": "stable-initial-administrator-test-key",
            "EMAIL_WORKER_ENABLED": "false", "EMAIL_SECRET_KEY": "",
        })
        self.environment.start()
        for key in ADMIN_ENV:
            os.environ.pop(key, None)
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        self.environment.stop()
        self.directory.cleanup()

    def start_app(self):
        app = create_app()
        app.config.update(TESTING=True)
        self.apps.append(app)
        return app

    def initialise(self, app, environment=None):
        with patch.dict(os.environ, ADMIN_ENV if environment is None else environment):
            with app.app_context():
                initialise_initial_admin()

    def snapshot(self, app):
        with app.app_context():
            return {
                model.__tablename__: [dict(row) for row in db.session.execute(
                    model.__table__.select().order_by(*model.__table__.primary_key.columns)
                ).mappings()]
                for model in (User, UserProfile, Dependent, PageAccessSettings, PageRolePermission)
            }

    def add_user(self, app, *, username=None, email=None, password=None,
                 privilege=UserPrivilege.employee, profile=True):
        with app.app_context():
            user = User(
                username=username or ADMIN_ENV["INITIAL_ADMIN_USERNAME"],
                email=email or ADMIN_ENV["INITIAL_ADMIN_EMAIL"], privilege=privilege,
            )
            user.set_password(password or ADMIN_ENV["INITIAL_ADMIN_PASSWORD"])
            if profile:
                user.profile = UserProfile(full_name="Existing person", phone_number="saved-phone")
            db.session.add(user)
            db.session.commit()
            return user.id

    def assert_rejected_without_changes(self, app, environment):
        before = self.snapshot(app)
        with self.assertRaises(RuntimeError) as caught:
            self.initialise(app, environment)
        for value in environment.values():
            if value and len(value) > 10:
                self.assertNotIn(value, str(caught.exception))
        self.assertEqual(self.snapshot(app), before)

    def test_unconfigured_installation_does_not_create_a_builtin_account(self):
        app = self.start_app()
        with app.app_context():
            initialise_initial_admin()
            self.assertEqual(User.query.count(), 0)
            self.assertEqual(UserProfile.query.count(), 0)

    def test_configured_fresh_start_creates_one_developer_with_a_hashed_password_and_profile(self):
        with patch.dict(os.environ, ADMIN_ENV):
            app = self.start_app()
        with app.app_context():
            user = User.query.one()
            self.assertEqual(user.username, ADMIN_ENV["INITIAL_ADMIN_USERNAME"])
            self.assertEqual(user.email, ADMIN_ENV["INITIAL_ADMIN_EMAIL"])
            self.assertEqual(user.privilege, UserPrivilege.developer)
            self.assertNotEqual(user.password_hash, ADMIN_ENV["INITIAL_ADMIN_PASSWORD"])
            self.assertNotIn(ADMIN_ENV["INITIAL_ADMIN_PASSWORD"], user.password_hash)
            self.assertTrue(user.check_password(ADMIN_ENV["INITIAL_ADMIN_PASSWORD"]))
            self.assertIsNotNone(user.profile)
            self.assertEqual(UserProfile.query.count(), 1)

    def test_provisioned_developer_can_log_in_and_open_both_configuration_pages(self):
        with patch.dict(os.environ, ADMIN_ENV):
            app = self.start_app()
        for identifier in (ADMIN_ENV["INITIAL_ADMIN_USERNAME"], ADMIN_ENV["INITIAL_ADMIN_EMAIL"]):
            with self.subTest(identifier=identifier):
                client = app.test_client()
                response = client.post("/login", data={
                    "login": identifier, "password": ADMIN_ENV["INITIAL_ADMIN_PASSWORD"],
                }, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                with client.session_transaction() as session:
                    self.assertIn("_user_id", session)
                for path in ("/page-access", "/settings"):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn(ADMIN_ENV["INITIAL_ADMIN_PASSWORD"].encode(), response.data)

    def test_identifier_normalisation_keeps_email_login_compatible(self):
        environment = dict(ADMIN_ENV, INITIAL_ADMIN_USERNAME="  owner  ",
                           INITIAL_ADMIN_EMAIL="  Owner@Example.INVALID  ",
                           INITIAL_ADMIN_PASSWORD="minimum-1234")
        self.assertEqual(len(environment["INITIAL_ADMIN_PASSWORD"]), 12)
        with patch.dict(os.environ, environment):
            app = self.start_app()
        with app.app_context():
            user = User.query.one()
            self.assertEqual(user.username, "owner")
            self.assertEqual(user.email, "owner@example.invalid")
        client = app.test_client()
        response = client.post("/login", data={
            "login": "OWNER@EXAMPLE.INVALID", "password": environment["INITIAL_ADMIN_PASSWORD"],
        })
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertIn("_user_id", session)

    def test_password_whitespace_is_preserved_instead_of_silently_changing_the_secret(self):
        password = "  a meaningful password  "
        with patch.dict(os.environ, dict(ADMIN_ENV, INITIAL_ADMIN_PASSWORD=password)):
            app = self.start_app()
        with app.app_context():
            user = User.query.one()
            self.assertTrue(user.check_password(password))
            self.assertFalse(user.check_password(password.strip()))

    def test_partial_or_empty_configuration_never_creates_an_account(self):
        app = self.start_app()
        keys = list(ADMIN_ENV)
        for mask in range(1, 7):
            environment = {key: ADMIN_ENV[key] for index, key in enumerate(keys) if mask & (1 << index)}
            with self.subTest(present=list(environment)):
                self.assert_rejected_without_changes(app, environment)
        for key in keys:
            with self.subTest(empty=key):
                self.assert_rejected_without_changes(app, dict(ADMIN_ENV, **{key: ""}))
        self.assert_rejected_without_changes(app, dict.fromkeys(keys, ""))

    def test_invalid_identifiers_and_password_lengths_fail_without_partial_writes(self):
        app = self.start_app()
        invalid = [
            ("INITIAL_ADMIN_USERNAME", " " * 10),
            ("INITIAL_ADMIN_USERNAME", "u" * 51),
            ("INITIAL_ADMIN_USERNAME", "owner\x1fhidden"),
            ("INITIAL_ADMIN_EMAIL", "e" * 110 + "@example.invalid"),
            ("INITIAL_ADMIN_EMAIL", "no-at-sign"),
            ("INITIAL_ADMIN_EMAIL", "owner@@example.invalid"),
            ("INITIAL_ADMIN_EMAIL", "owner name@example.invalid"),
            ("INITIAL_ADMIN_EMAIL", "owner@example.invalid\r\nBcc:other@example.invalid"),
            ("INITIAL_ADMIN_EMAIL", "owner@exam\x1fple.invalid"),
            ("INITIAL_ADMIN_PASSWORD", "x" * 11),
            ("INITIAL_ADMIN_PASSWORD", "x" * 1025),
        ]
        for key, value in invalid:
            with self.subTest(field=key, value_length=len(value)):
                self.assert_rejected_without_changes(app, dict(ADMIN_ENV, **{key: value}))

    def test_supported_field_length_boundaries_are_usable(self):
        app = self.start_app()
        username = "u" * 50
        email = "a" * 60 + "@" + "b" * 51 + ".invalid"
        self.assertEqual(len(email), 120)
        environment = dict(ADMIN_ENV, INITIAL_ADMIN_USERNAME=username,
                           INITIAL_ADMIN_EMAIL=email, INITIAL_ADMIN_PASSWORD="p" * 1024)
        self.initialise(app, environment)
        with app.app_context():
            user = User.query.one()
            self.assertEqual(user.username, username)
            self.assertEqual(user.email, email)
            self.assertTrue(user.check_password("p" * 1024))

    def test_any_existing_developer_prevents_creation_promotion_or_validation(self):
        app = self.start_app()
        self.add_user(app, username="different-developer", email="different@example.invalid",
                      privilege=UserPrivilege.developer)
        self.add_user(app)
        before = self.snapshot(app)
        for environment in (
            ADMIN_ENV,
            {"INITIAL_ADMIN_PASSWORD": "invalid"},
            dict(ADMIN_ENV, INITIAL_ADMIN_EMAIL="invalid", INITIAL_ADMIN_PASSWORD="changed"),
        ):
            with self.subTest(supplied=list(environment)):
                self.initialise(app, environment)
                self.assertEqual(self.snapshot(app), before)

    def test_restarts_preserve_changed_password_and_saved_access_matrix(self):
        with patch.dict(os.environ, ADMIN_ENV):
            app = self.start_app()
        changed_password = "Changed-in-account-settings-987"
        with app.app_context():
            user = User.query.one()
            user.set_password(changed_password)
            settings = db.session.get(PageAccessSettings, 1)
            settings.updated_by_id = user.id
            db.session.add(PageRolePermission(page_key="dashboard", role="employee", allowed=False))
            db.session.commit()
        before = self.snapshot(app)
        for environment in (ADMIN_ENV, {"INITIAL_ADMIN_PASSWORD": "stale"}):
            with patch.dict(os.environ, environment):
                restarted = self.start_app()
            self.assertEqual(self.snapshot(restarted), before)
            with restarted.app_context():
                self.assertTrue(User.query.one().check_password(changed_password))
                self.assertFalse(User.query.one().check_password(ADMIN_ENV["INITIAL_ADMIN_PASSWORD"]))

    def test_matching_registered_account_is_promoted_without_changing_personal_records_or_hash(self):
        app = self.start_app()
        client = app.test_client()
        response = client.post("/register", data={
            "username": ADMIN_ENV["INITIAL_ADMIN_USERNAME"],
            "email": ADMIN_ENV["INITIAL_ADMIN_EMAIL"],
            "password": ADMIN_ENV["INITIAL_ADMIN_PASSWORD"],
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            user = User.query.one()
            self.assertEqual(user.privilege, UserPrivilege.employee)
            user.profile.full_name = "Recorded full name"
            db.session.add(Dependent(
                user_id=user.id, name="Existing dependent", date_of_birth=date(2020, 1, 1),
                social_security_number="123456789", dependency_start=date(2020, 1, 1),
            ))
            db.session.commit()
        before = self.snapshot(app)
        self.initialise(app)
        after = self.snapshot(app)
        expected = dict(before["users"][0], privilege=UserPrivilege.developer)
        self.assertEqual(after["users"], [expected])
        for table in before.keys() - {"users"}:
            self.assertEqual(after[table], before[table])

    def test_promotion_supplies_a_missing_profile_without_changing_the_password(self):
        app = self.start_app()
        user_id = self.add_user(app, profile=False)
        before = self.snapshot(app)["users"][0]
        self.initialise(app)
        with app.app_context():
            user = db.session.get(User, user_id)
            self.assertEqual(user.privilege, UserPrivilege.developer)
            self.assertEqual(user.password_hash, before["password_hash"])
            self.assertIsNotNone(user.profile)
            self.assertEqual(UserProfile.query.count(), 1)

    def test_matching_identifiers_without_the_existing_password_do_not_take_over_an_account(self):
        app = self.start_app()
        self.add_user(app, password="Existing-person-private-password")
        self.assert_rejected_without_changes(app, ADMIN_ENV)

    def test_profile_write_failure_rolls_back_new_accounts_and_existing_account_promotions(self):
        app = self.start_app()

        def fail_profile_insert(mapper, connection, target):
            # This fires after the INSERT/UPDATE of the user row, verifying that
            # a later database failure cannot leave a partially provisioned user.
            raise OperationalError("INSERT INTO user_profiles", {
                "email": ADMIN_ENV["INITIAL_ADMIN_EMAIL"], "password_hash": "private-hash",
            }, Exception("permission denied"))

        for existing_account in (False, True):
            with self.subTest(existing_account=existing_account):
                if existing_account:
                    self.add_user(app, profile=False)
                event.listen(UserProfile, "before_insert", fail_profile_insert)
                try:
                    self.assert_rejected_without_changes(app, ADMIN_ENV)
                finally:
                    event.remove(UserProfile, "before_insert", fail_profile_insert)

    def test_failed_race_recovery_hides_original_insert_secrets_from_the_traceback(self):
        app = self.start_app()
        before = self.snapshot(app)
        private_hash = "test-only-private-password-hash-marker"
        insert_statement = "INSERT INTO users (email, password_hash) VALUES (?, ?)"
        recovery_statement = "SELECT id FROM users WHERE privilege = ?"
        insert_error = IntegrityError(insert_statement, {
            "email": ADMIN_ENV["INITIAL_ADMIN_EMAIL"], "password_hash": private_hash,
        }, Exception("test-only unique constraint failure"))
        recovery_error = OperationalError(recovery_statement, {"privilege": "developer"},
                                          Exception("test-only connection lost"))
        with patch("app.initial_admin._has_developer", side_effect=[False, recovery_error]):
            with patch.object(db.session, "commit", side_effect=insert_error):
                with self.assertRaises(RuntimeError) as caught:
                    self.initialise(app)
        self.assertIn("could not save the first administrator", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        rendered_traceback = "".join(traceback.format_exception(caught.exception))
        for private_value in (private_hash, ADMIN_ENV["INITIAL_ADMIN_EMAIL"],
                              insert_statement, recovery_statement):
            self.assertNotIn(private_value, rendered_traceback)
        self.assertEqual(self.snapshot(app), before)

    def test_email_or_username_collisions_do_not_promote_replace_or_merge_people(self):
        cases = (
            (("someone-else", ADMIN_ENV["INITIAL_ADMIN_EMAIL"]),),
            ((ADMIN_ENV["INITIAL_ADMIN_USERNAME"], "other@example.invalid"),),
            (("someone-else", ADMIN_ENV["INITIAL_ADMIN_EMAIL"]),
             (ADMIN_ENV["INITIAL_ADMIN_USERNAME"], "other@example.invalid")),
        )
        for index, people in enumerate(cases):
            with self.subTest(case=index):
                with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///" + str(Path(self.directory.name) / f"collision-{index}.db")}):
                    app = self.start_app()
                for username, email in people:
                    self.add_user(app, username=username, email=email)
                self.assert_rejected_without_changes(app, ADMIN_ENV)

    def test_unrelated_employee_records_do_not_prevent_first_administrator_creation(self):
        app = self.start_app()
        self.add_user(app, username="existing-employee", email="employee@example.invalid")
        before = self.snapshot(app)
        self.initialise(app)
        after = self.snapshot(app)
        self.assertEqual(after["users"][0], before["users"][0])
        self.assertEqual(after["user_profiles"][0], before["user_profiles"][0])
        self.assertEqual(len(after["users"]), 2)
        self.assertEqual(after["users"][1]["privilege"], UserPrivilege.developer)

    def test_public_registration_cannot_supply_bootstrap_secrets_or_developer_privilege(self):
        app = self.start_app()
        client = app.test_client()
        payload = dict(ADMIN_ENV, username="public-user", email="public@example.invalid",
                       password="public-user-test-password", privilege="developer", role="developer")
        response = client.post("/register", data=payload, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/page-access").status_code, 403)
        self.assertEqual(client.get("/settings").status_code, 403)
        with app.app_context():
            user = User.query.one()
            self.assertEqual(user.username, "public-user")
            self.assertEqual(user.privilege, UserPrivilege.employee)

    def test_bootstrap_credentials_are_not_exposed_in_public_pages_or_application_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        with patch.dict(os.environ, ADMIN_ENV):
            app = self.start_app()
        client = app.test_client()
        for path in ("/", "/login", "/register"):
            response = client.get(path, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            for value in ADMIN_ENV.values():
                self.assertNotIn(value.encode(), response.data)
        for value in ADMIN_ENV.values():
            self.assertNotIn(value, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
