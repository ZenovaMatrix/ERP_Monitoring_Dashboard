
import sqlite3

db_path = "monitoring.db"

connection = sqlite3.connect(db_path)
cursor = connection.cursor()

cursor.execute("""
    SELECT name
    FROM sqlite_master
    WHERE type = 'table'
      AND name NOT LIKE 'sqlite_%'
    ORDER BY name
""")

tables = cursor.fetchall()

print("\n===== DATABASE TABLES AND COLUMNS =====")

for (table_name,) in tables:
    print(f"\nTable: {table_name}")

    cursor.execute(f'PRAGMA table_info("{table_name}")')
    columns = cursor.fetchall()

    for column in columns:
        print(f"  Column: {column[1]} | Type: {column[2]}")

    cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
    count = cursor.fetchone()[0]
    print(f"  Existing records: {count}")

connection.close()
