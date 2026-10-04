"""Create missing tables and add compatible optional fields to existing records."""

from sqlalchemy import Integer, MetaData, inspect
from sqlalchemy.exc import DBAPIError


QUALIFICATION_DATE_TABLES = ("educational_qualifications", "professional_exams")


def _is_duplicate_date_column(error, dialect_name):
    """Recognise only the duplicate-column error expected from a startup race."""
    original = error.orig
    if dialect_name in {"mysql", "mariadb"}:
        code = getattr(original, "errno", None)
        if code is None and getattr(original, "args", ()):
            code = original.args[0]
        return code == 1060
    if dialect_name == "sqlite":
        return str(original).casefold() == "duplicate column name: date_obtained"
    return False


def ensure_qualification_date_columns(engine) -> None:
    """Add nullable exact dates without inventing dates for year-only records.

    Fresh databases already have these columns from the ORM metadata. Existing
    SQLite/MySQL/MariaDB installations receive only the two missing DATE fields;
    the legacy year, constraints, identifiers and all existing rows stay intact.
    """
    quote = engine.dialect.identifier_preparer.quote_identifier
    for table_name in QUALIFICATION_DATE_TABLES:
        columns = inspect(engine).get_columns(table_name)
        if any(column["name"] == "date_obtained" for column in columns):
            continue
        statement = f"ALTER TABLE {quote(table_name)} ADD COLUMN {quote('date_obtained')} DATE NULL"
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(statement)
        except DBAPIError as error:
            # Another worker may have added it after our initial inspection.
            # Do not hide lock, permission, connectivity or unrelated DDL errors.
            if not _is_duplicate_date_column(error, engine.dialect.name):
                raise
            refreshed_columns = inspect(engine).get_columns(table_name)
            if not any(column["name"] == "date_obtained" for column in refreshed_columns):
                raise


def create_missing_tables(engine, metadata) -> None:
    """Match new MySQL integer foreign keys to the actual referenced column.

    The SQL installation script uses unsigned user IDs, whereas older
    installations created by SQLAlchemy use signed IDs. MySQL requires both
    sides of an integer foreign key to have the same size and signedness.
    Adapt a separate DDL metadata copy, without altering existing tables or
    mutating the application's shared ORM models.
    """
    if engine.dialect.name not in {"mysql", "mariadb"}:
        metadata.create_all(engine)
        return

    inspector = inspect(engine)
    existing_tables = {
        (schema, name)
        for schema in {table.schema for table in metadata.tables.values()}
        for name in inspector.get_table_names(schema=schema)
    }
    ddl_metadata = MetaData()
    for table in metadata.tables.values():
        table.to_metadata(ddl_metadata)

    missing_tables = [
        table for table in ddl_metadata.sorted_tables
        if (table.schema, table.name) not in existing_tables
    ]
    reflected_columns = {}
    for table in missing_tables:
        for foreign_key in table.foreign_keys:
            if not isinstance(foreign_key.parent.type, Integer):
                continue
            referenced_column = foreign_key.column
            parent = referenced_column.table
            parent_key = (parent.schema, parent.name)
            if parent_key in existing_tables:
                if parent_key not in reflected_columns:
                    reflected_columns[parent_key] = {
                        column["name"]: column["type"]
                        for column in inspector.get_columns(parent.name, schema=parent.schema)
                    }
                referenced_type = reflected_columns[parent_key][referenced_column.name]
            else:
                referenced_type = referenced_column.type

            if isinstance(referenced_type, Integer):
                foreign_key.parent.type = referenced_type.copy()

    ddl_metadata.create_all(engine, tables=missing_tables, checkfirst=True)
