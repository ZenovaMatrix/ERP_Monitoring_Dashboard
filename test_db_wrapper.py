
from db_wrapper import DatabaseWrapper

db = None

try:
    db = DatabaseWrapper()

    db.test_connection()
    print("Database type:", db.db_type)
    print("Database connection successful!")

    result = db.fetch_one("SELECT 1 AS test_value")
    print("Test query result:", result)

except Exception as error:
    print("Database connection failed:", error)

finally:
    if db is not None:
        db.close()
