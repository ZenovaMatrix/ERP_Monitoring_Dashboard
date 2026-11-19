
import sqlite3
import random
from datetime import datetime, timedelta

DB_PATH = "monitoring.db"
random.seed(42)

now = datetime.now()

message_types = ["INFO", "WARNING", "ERROR"]
message_states = ["WAITING", "PROCESSING", "SUCCESS", "FAILED"]
interfaces = ["SAP-to-ERP", "ERP-to-CRM", "Payment-Gateway", "Inventory-Sync"]
error_messages = [
    "Connection timeout while calling destination system",
    "Authentication failed for the destination API",
    "Invalid payload received from source system",
    "Required field missing in request",
    "Destination system returned HTTP 500",
    "Message processing failed after maximum retries",
]
alert_names = [
    "High CPU Usage",
    "Database Connection Warning",
    "Interface Failure",
    "High Memory Usage",
    "Background Job Failure",
]
severities = ["INFO", "WARNING", "CRITICAL"]
alert_statuses = ["OPEN", "ACKNOWLEDGED", "RESOLVED"]

connection = sqlite3.connect(DB_PATH)
cursor = connection.cursor()

def backdated_time():
    days_ago = random.randint(0, 59)
    seconds_ago = random.randint(0, 86399)
    return (now - timedelta(days=days_ago, seconds=seconds_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

try:
    # Insert 1,000 application messages
    for _ in range(1000):
        created = backdated_time()
        updated = (
            datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            + timedelta(minutes=random.randint(1, 180))
        ).strftime("%Y-%m-%d %H:%M:%S")

        state = random.choice(message_states)
        error_text = (
            random.choice(error_messages)
            if state == "FAILED"
            else None
        )

        cursor.execute("""
            INSERT INTO application_messages
            (message_type, source_system, destination_system, state,
             retry_count, error_text, created_time, updated_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            random.choice(message_types),
            random.choice(interfaces),
            random.choice(["ERP", "CRM", "SAP", "Warehouse"]),
            state,
            random.randint(0, 5),
            error_text,
            created,
            updated,
        ))

    # Insert 600 interface errors
    for _ in range(600):
        cursor.execute("""
            INSERT INTO interface_errors
            (interface_name, error_message, acknowledged, created_time)
            VALUES (?, ?, ?, ?)
        """, (
            random.choice(interfaces),
            random.choice(error_messages),
            random.choice([0, 1]),
            backdated_time(),
        ))

    # Insert 400 monitoring alerts
    for _ in range(400):
        cursor.execute("""
            INSERT INTO monitoring_alerts
            (alert_name, severity, status, message, created_time)
            VALUES (?, ?, ?, ?, ?)
        """, (
            random.choice(alert_names),
            random.choice(severities),
            random.choice(alert_statuses),
            "Simulated monitoring event for dashboard testing",
            backdated_time(),
        ))

    connection.commit()

    print("Successfully inserted 2,000 test records.")

    for table in (
        "application_messages",
        "interface_errors",
        "monitoring_alerts",
    ):
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        print(f"{table}: {cursor.fetchone()[0]} total records")

except Exception:
    connection.rollback()
    raise

finally:
    connection.close()
