"""Build a fresh installation before any HTTP request or mail worker can run."""

from contextlib import contextmanager, nullcontext
from hashlib import sha256

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from . import db
from .schema import (
    create_missing_tables,
    ensure_qualification_date_columns,
    ensure_work_assignment_flexible_shift_column,
)


MYSQL_DIALECTS = {"mysql", "mariadb"}
BOOTSTRAP_LOCK_TIMEOUT = 30


def _mysql_error_code(error):
    original = error.orig
    code = getattr(original, "errno", None)
    if code is None and getattr(original, "args", ()):
        code = original.args[0]
    return code


def _ensure_mysql_database(engine):
    """Create only a missing configured database, using the same credentials.

    Existing databases require no server-level CREATE privilege. Authentication,
    connectivity and other failures never trigger a different database or user.
    """
    url = engine.url
    if not url.database:
        raise RuntimeError("Set a database name in the CSUPOR database settings.")
    try:
        with engine.connect():
            return
    except DBAPIError as error:
        if _mysql_error_code(error) != 1049:
            raise

    server_url = URL.create(
        drivername=url.drivername, username=url.username, password=url.password,
        host=url.host, port=url.port, database=None,
        query={**url.query, "charset": "utf8mb4"},
    )
    # Never pool a connection without its configured database selected.
    server_engine = create_engine(server_url, poolclass=NullPool)
    try:
        database_name = engine.dialect.identifier_preparer.quote_identifier(url.database)
        with server_engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE DATABASE IF NOT EXISTS {database_name} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    except DBAPIError as error:
        if _mysql_error_code(error) not in {1044, 1045, 1142, 1227}:
            raise
        raise RuntimeError(
            "CSUPOR cannot create the configured database with this database user. "
            "Create an empty database in your hosting control panel, use its name "
            "in the app's database settings, and allow the database user to create "
            "and alter tables. The app will build the tables automatically."
        ) from None
    finally:
        server_engine.dispose()


@contextmanager
def _mysql_bootstrap_lock(engine):
    """Serialise all startup DDL across workers sharing a MySQL database.

    MySQL named locks belong to a connection, not a transaction. Keep that
    connection until every table, compatible column and default is ready, and
    release the lock explicitly before returning it to the pool.
    """
    database_name = engine.url.database or ""
    lock_name = "csupor:bootstrap:" + sha256(database_name.casefold().encode("utf-8")).hexdigest()[:40]
    with engine.connect() as connection:
        try:
            acquired = connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": lock_name, "timeout": BOOTSTRAP_LOCK_TIMEOUT},
            ).scalar()
        except BaseException:
            # The server might have acquired the lock before a lost response.
            connection.invalidate()
            raise
        if acquired != 1:
            if acquired is None:
                connection.invalidate()
            raise RuntimeError(
                "CSUPOR could not obtain the database initialisation lock within "
                "30 seconds. Wait for the other app instance to finish starting, "
                "then restart this instance."
            )
        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            try:
                released = connection.execute(
                    text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name},
                ).scalar()
                if released != 1:
                    raise RuntimeError("CSUPOR could not release the database initialisation lock.")
            except BaseException:
                # Returning a live connection here could indefinitely retain a
                # named lock. Disconnect it, preserving an original DDL failure.
                connection.invalidate()
                if not body_failed:
                    raise


def initialise_database():
    """Register the whole schema and initialise it without overwriting data."""
    # Keep fresh setup independent of which blueprint happens to import a model.
    from . import (  # noqa: F401
        mail_settings_models,
        models,
        notification_models,
        page_access_models,
        worktime_models,
    )
    from .leave_approval import initialise_leave_approval_settings
    from .page_access import initialise_page_access_settings

    engine = db.engine
    is_mysql = engine.dialect.name in MYSQL_DIALECTS
    if is_mysql:
        _ensure_mysql_database(engine)
    with _mysql_bootstrap_lock(engine) if is_mysql else nullcontext():
        create_missing_tables(engine, db.metadata)
        ensure_qualification_date_columns(engine)
        ensure_work_assignment_flexible_shift_column(engine)
        initialise_leave_approval_settings()
        initialise_page_access_settings()
