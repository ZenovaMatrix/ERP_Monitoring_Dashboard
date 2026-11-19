
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import URL
from pymongo import MongoClient


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class DatabaseWrapper:
    """Reusable database wrapper for SQLite, PostgreSQL, Oracle and MongoDB."""

    def __init__(self):
        self.db_type = os.getenv("DB_TYPE", "sqlite").strip().lower()
        self.engine = None
        self.mongo_client = None
        self.mongo_db = None

        if self.db_type == "sqlite":
            db_path = Path(os.getenv("SQLITE_PATH", "monitoring.db"))

            if not db_path.is_absolute():
                db_path = BASE_DIR / db_path

            self.engine = create_engine(
                URL.create(
                    drivername="sqlite",
                    database=str(db_path)
                )
            )

        elif self.db_type in ("postgres", "postgresql"):
            self.engine = create_engine(
                URL.create(
                    drivername="postgresql+psycopg",
                    username=os.getenv("DB_USER"),
                    password=os.getenv("DB_PASSWORD"),
                    host=os.getenv("DB_HOST", "localhost"),
                    port=int(os.getenv("DB_PORT", "5432")),
                    database=os.getenv("DB_NAME"),
                ),
                pool_pre_ping=True,
            )

        elif self.db_type == "oracle":
            service_name = os.getenv("DB_SERVICE_NAME")

            if not service_name:
                raise ValueError(
                    "Please set DB_SERVICE_NAME in your .env file."
                )

            self.engine = create_engine(
                URL.create(
                    drivername="oracle+oracledb",
                    username=os.getenv("DB_USER"),
                    password=os.getenv("DB_PASSWORD"),
                    host=os.getenv("DB_HOST", "localhost"),
                    port=int(os.getenv("DB_PORT", "1521")),
                    query={"service_name": service_name},
                ),
                pool_pre_ping=True,
            )

        elif self.db_type == "mongodb":
            mongo_uri = os.getenv("MONGO_URI")
            database_name = os.getenv("MONGO_DATABASE")

            if not mongo_uri or not database_name:
                raise ValueError(
                    "Please set MONGO_URI and MONGO_DATABASE in .env."
                )

            self.mongo_client = MongoClient(
                mongo_uri,
                serverSelectionTimeoutMS=5000,
            )
            self.mongo_db = self.mongo_client[database_name]

        else:
            raise ValueError(
                f"Unsupported DB_TYPE: {self.db_type}"
            )

    def test_connection(self):
        """Check whether the selected database is reachable."""

        if self.db_type == "mongodb":
            self.mongo_client.admin.command("ping")
            return True

        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return True

    def fetch_all(self, query, params=None):
        """Run a SQL query and return all rows as dictionaries."""

        if self.db_type == "mongodb":
            raise NotImplementedError(
                "MongoDB uses find_documents(), not SQL."
            )

        with self.engine.connect() as connection:
            result = connection.execute(
                text(query),
                params or {},
            )
            return [
                dict(row)
                for row in result.mappings().all()
            ]

    def fetch_one(self, query, params=None):
        """Run a SQL query and return one row as a dictionary."""

        if self.db_type == "mongodb":
            raise NotImplementedError(
                "MongoDB uses find_documents(), not SQL."
            )

        with self.engine.connect() as connection:
            result = connection.execute(
                text(query),
                params or {},
            )
            row = result.mappings().first()

            return dict(row) if row is not None else None

    def execute(self, query, params=None):
        """Execute a SQL INSERT, UPDATE or DELETE statement."""

        if self.db_type == "mongodb":
            raise NotImplementedError(
                "Use MongoDB document methods instead."
            )

        with self.engine.begin() as connection:
            result = connection.execute(
                text(query),
                params or {},
            )
            return result.rowcount

    def table_exists(self, table_name):
        """Check whether a relational table or MongoDB collection exists."""

        if self.db_type == "mongodb":
            return table_name in self.mongo_db.list_collection_names()

        return inspect(self.engine).has_table(table_name)

    def get_columns(self, table_name):
        """Return column names for a relational database table."""

        if self.db_type == "mongodb":
            return []

        inspector = inspect(self.engine)

        if not inspector.has_table(table_name):
            return []

        return [
            column["name"]
            for column in inspector.get_columns(table_name)
        ]

    def find_documents(self, collection, filter_query=None):
        """Fetch MongoDB documents from a collection."""

        if self.db_type != "mongodb":
            raise TypeError("This method requires MongoDB.")

        return list(
            self.mongo_db[collection].find(
                filter_query or {},
                {"_id": 0},
            )
        )

    def insert_document(self, collection, document):
        """Insert a document into MongoDB."""

        if self.db_type != "mongodb":
            raise TypeError("This method requires MongoDB.")

        result = self.mongo_db[collection].insert_one(document)
        return str(result.inserted_id)

    def close(self):
        """Release database connections and resources."""

        if self.engine is not None:
            self.engine.dispose()

        if self.mongo_client is not None:
            self.mongo_client.close()
