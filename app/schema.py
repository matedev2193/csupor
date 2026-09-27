"""Create missing tables while respecting identifiers in an existing database."""

from sqlalchemy import Integer, MetaData, inspect


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
