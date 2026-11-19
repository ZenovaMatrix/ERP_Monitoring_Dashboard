import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SQL_FILE = BASE_DIR / "database.sql"
DB_FILE = BASE_DIR / "monitoring.db"


def create_database():
    print("Creating local dummy database...")

    if not SQL_FILE.exists():
        print("ERROR: database.sql was not found.")
        return

    sql = SQL_FILE.read_text(encoding="utf-8")

    with sqlite3.connect(DB_FILE) as conn:
        conn.executescript(sql)
        conn.commit()

    print("Database created successfully.")
    print(f"Database location: {DB_FILE}")


if __name__ == "__main__":
    create_database()