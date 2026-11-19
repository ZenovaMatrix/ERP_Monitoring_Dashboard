
from db_wrapper import DatabaseWrapper

db = None

try:
    db = DatabaseWrapper()
    print("Database type:", db.db_type)
    print("\nTables available in PostgreSQL:")

    tables = db.engine_inspector.get_table_names() if hasattr(db, "engine_inspector") else None

    if tables is None:
        from sqlalchemy import inspect
        tables = inspect(db.engine).get_table_names()

    if tables:
        for table in tables:
            print("-", table)
    else:
        print("No tables found.")

except Exception as error:
    print("Error:", error)

finally:
    if db is not None:
        db.close()
