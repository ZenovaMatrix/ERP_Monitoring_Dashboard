
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from db_wrapper import DatabaseWrapper

SQLITE_PATH = Path(__file__).resolve().parent / "monitoring.db"

sqlite_connection = None
db = None

try:
    # Connect to the original SQLite database.
    sqlite_connection = sqlite3.connect(str(SQLITE_PATH))
    sqlite_connection.row_factory = sqlite3.Row

    # Connect to PostgreSQL using your existing .env settings.
    db = DatabaseWrapper()

    if db.db_type != "postgresql":
        raise RuntimeError(
            "DB_TYPE must be postgresql in .env before migration."
        )

    # Use the wrapper's existing SQLAlchemy engine.
    engine = db.engine

    cursor = sqlite_connection.cursor()
    cursor.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)
    tables = [row["name"] for row in cursor.fetchall()]

    print("Source tables:", tables)

    # Create compatible tables and copy rows in one transaction.
    with engine.begin() as connection:
        for table in tables:
            # Quote identifiers using SQLAlchemy's dialect.
            preparer = engine.dialect.identifier_preparer
            quoted_table = preparer.quote(table)

            cursor.execute(f'PRAGMA table_info("{table}")')
            columns = cursor.fetchall()

            column_definitions = []
            column_names = []

            for column in columns:
                name = column["name"]
                sqlite_type = (column["type"] or "").upper()
                primary_key = column["pk"] > 0

                # Map SQLite types to PostgreSQL-compatible types.
                if "INT" in sqlite_type:
                    pg_type = "INTEGER"
                elif any(t in sqlite_type for t in
                         ("CHAR", "CLOB", "TEXT")):
                    pg_type = "TEXT"
                elif any(t in sqlite_type for t in
                         ("REAL", "FLOA", "DOUB")):
                    pg_type = "DOUBLE PRECISION"
                elif "BLOB" in sqlite_type:
                    pg_type = "BYTEA"
                else:
                    pg_type = "TEXT"

                definition = f"{preparer.quote(name)} {pg_type}"

                if primary_key:
                    definition += " PRIMARY KEY"

                column_definitions.append(definition)
                column_names.append(name)

            create_sql = (
                f"CREATE TABLE IF NOT EXISTS {quoted_table} "
                f"({', '.join(column_definitions)})"
            )
            connection.execute(text(create_sql))

            # Check whether the target table already contains data.
            count = connection.execute(
                text(f"SELECT COUNT(*) FROM {quoted_table}")
            ).scalar_one()

            if count > 0:
                print(f"Skipping {table}: PostgreSQL table is not empty.")
                continue

            cursor.execute(f'SELECT * FROM "{table}"')
            rows = cursor.fetchall()

            if rows:
                quoted_columns = ", ".join(
                    preparer.quote(name) for name in column_names
                )
                placeholders = ", ".join(
                    f":v{i}" for i in range(len(column_names))
                )

                insert_sql = text(
                    f"INSERT INTO {quoted_table} "
                    f"({quoted_columns}) VALUES ({placeholders})"
                )

                values = [
                    {f"v{i}": row[name]
                     for i, name in enumerate(column_names)}
                    for row in rows
                ]

                connection.execute(insert_sql, values)

            print(f"Migrated {table}: {len(rows)} rows")

    print("\nMigration completed.")

except Exception as error:
    print("Migration failed:", error)
    raise

finally:
    if sqlite_connection is not None:
        sqlite_connection.close()
    if db is not None:
        db.close()
