
from db_wrapper import DatabaseWrapper

db = None

try:
    db = DatabaseWrapper()

    print("Database type:", db.db_type)
    print("Connection:", db.test_connection())

    # Test fetch_one()
    print("\n1. Testing fetch_one()")
    print(db.fetch_one("SELECT 1 AS test_value"))

    # Test fetch_all()
    print("\n2. Testing fetch_all()")
    print(db.fetch_all("SELECT 1 AS test_value"))

    # Test execute(), table_exists() and get_columns()
    print("\n3. Testing table methods")
    db.execute("""
        CREATE TABLE IF NOT EXISTS wrapper_test (
            id INTEGER PRIMARY KEY,
            name VARCHAR(100)
        )
    """)

    print("Table exists:", db.table_exists("wrapper_test"))
    print("Columns:", db.get_columns("wrapper_test"))

    print("\nAll tested methods completed.")

except Exception as error:
    print("Test failed:", error)

finally:
    if db is not None:
        db.close()
