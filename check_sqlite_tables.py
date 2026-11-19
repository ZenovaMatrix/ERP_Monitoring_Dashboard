
import sqlite3

database_path = "monitoring.db"
connection = None

try:
    connection = sqlite3.connect(database_path)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)

    tables = cursor.fetchall()

    print("Tables in the original SQLite database:")

    if tables:
        for (table_name,) in tables:
            print(f"\nTable: {table_name}")

            cursor.execute(f'PRAGMA table_info("{table_name}")')
            columns = cursor.fetchall()

            for column in columns:
                print(f"  Column: {column[1]} | Type: {column[2]}")
    else:
        print("No tables found.")

except Exception as error:
    print("Error:", error)

finally:
    if connection:
        connection.close()
