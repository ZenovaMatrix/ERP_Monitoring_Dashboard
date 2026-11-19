"""
Seed back-dated Background Jobs and Application Messages for the local dummy DB.

Safe to run more than once: it only adds rows when the tagged seed rows are missing.
Usage:  python seed_backdated_data.py
"""
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "monitoring.db"
FMT = "%Y-%m-%d %H:%M:%S"
random.seed(7)
now = datetime.now()

JOB_NAMES = {
    "ERROR": [("ImportCustomerData", "INT_QUEUE", "Database connection lost while importing customer data."),
              ("Customer Sync", "INT_QUEUE", "Customer ID missing"),
              ("Invoice Export", "FIN_QUEUE", "Target SAP endpoint rejected the file (HTTP 500)."),
              ("MRP Run", "SCM_QUEUE", "ORA-01555: snapshot too old")],
    "EXECUTING": [("Inventory Recalculation", "SCM_QUEUE", None), ("Price List Update", "SAL_QUEUE", None)],
    "POSTED": [("Month End Posting", "FIN_QUEUE", None), ("Purchase Order Release", "SCM_QUEUE", None)],
    "READY": [("Invoice Posting", "FIN_QUEUE", None), ("Shipment Planning", "SCM_QUEUE", None)],
    "WARNING": [("Supplier Payment", "FIN_QUEUE", "Two records skipped"),
                ("Stock Reconciliation", "SCM_QUEUE", "Negative stock found for 3 parts")],
}
JOB_PER_STATE = {"ERROR": 8, "EXECUTING": 3, "POSTED": 6, "READY": 6, "WARNING": 5}

MSG_TYPES = [("CustomerSync", "CRM", "IFS"), ("ShipmentSync", "WMS", "IFS"),
             ("InvoiceExport", "IFS", "SAP"), ("SupplierImport", "PORTAL", "IFS"),
             ("OrderExport", "IFS", "WMS")]
MSG_ERRORS = ["Invalid customer number", "Connection timeout", "Authentication failed for destination API",
              "Required field missing in request", "Destination system returned HTTP 500"]
MSG_PER_STATE = {"FAILED": 8, "PROCESSING": 3, "SUCCESS": 12, "WAITING": 5}


def back(days_max=45):
    return now - timedelta(days=random.randint(1, days_max), seconds=random.randint(0, 86399))


def ensure_job_columns(cur):
    cols = [r[1] for r in cur.execute("PRAGMA table_info(background_jobs)")]
    for col in ("created_time", "updated_time"):
        if col not in cols:
            cur.execute(f"ALTER TABLE background_jobs ADD COLUMN {col} TEXT")
    # Back-fill from start/end time for the original rows
    cur.execute("UPDATE background_jobs SET created_time = COALESCE(created_time, start_time, '2026-10-27 00:00:00')")
    cur.execute("UPDATE background_jobs SET updated_time = COALESCE(updated_time, end_time, created_time)")


# ---------------------------------------------------------------------------
# Extra back-dated data for: Alerts, Server / API / Database health history,
# Print jobs, Database locks and Interface errors
# ---------------------------------------------------------------------------
def ensure_history_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS server_health_history (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT, server_name TEXT NOT NULL,
        cpu_percent REAL, memory_percent REAL, disk_percent REAL, status TEXT, checked_time TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS api_health_history (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT, api_name TEXT NOT NULL, endpoint TEXT,
        http_status INTEGER, response_time_ms INTEGER, status TEXT, checked_time TEXT NOT NULL)""")


def seed_once(cur, table, id_col, first_id, label):
    """Return True when the seed rows (id >= first_id) are not there yet."""
    n = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {id_col} >= ?", (first_id,)).fetchone()[0]
    if n:
        print(f"{label}: seed rows already present - skipped.")
        return False
    print(f"{label}: inserted back-dated rows.")
    return True


def seed_extra(cur):
    ensure_history_tables(cur)

    # --- Monitoring alerts (OPEN ones are what the dashboard counts) ---
    if seed_once(cur, "monitoring_alerts", "alert_id", 5100, "Monitoring alerts"):
        names = [("High CPU Usage", "CPU above 90% on application server"),
                 ("Database Lock Detected", "Database session is blocked"),
                 ("Interface Failure", "CustomerSync interface failed"),
                 ("Background Job Failed", "Scheduled job ended in ERROR"),
                 ("High Memory Usage", "Memory above 85% on application server"),
                 ("API Response Slow", "API response time above threshold")]
        for i in range(36):
            name, msg = random.choice(names)
            sev = random.choices(["CRITICAL", "WARNING", "INFO"], [3, 4, 2])[0]
            status = random.choices(["OPEN", "ACKNOWLEDGED", "RESOLVED"], [5, 2, 3])[0]
            cur.execute("INSERT INTO monitoring_alerts (alert_id, alert_name, severity, status, message, created_time) "
                        "VALUES (?,?,?,?,?,?)", (5100 + i, name, sev, status, msg, back().strftime(FMT)))

    # --- Server health history (every ~20h per server) ---
    if cur.execute("SELECT COUNT(*) FROM server_health_history").fetchone()[0] == 0:
        base = {"IFSAPP01": (65, 70, 60), "IFSAPP02": (88, 82, 75), "IFSAPP03": (92, 90, 87)}
        for server, (c, m, d) in base.items():
            for k in range(40):
                t = now - timedelta(hours=20 * k + random.randint(0, 5))
                cpu = max(20, min(99, c + random.randint(-25, 8)))
                mem = max(20, min(99, m + random.randint(-20, 8)))
                dsk = max(20, min(99, d + random.randint(-6, 3)))
                st = "CRITICAL" if max(cpu, mem, dsk) >= 90 else "WARNING" if max(cpu, mem, dsk) >= 80 else "HEALTHY"
                cur.execute("INSERT INTO server_health_history (server_name, cpu_percent, memory_percent, disk_percent, "
                            "status, checked_time) VALUES (?,?,?,?,?,?)", (server, cpu, mem, dsk, st, t.strftime(FMT)))
        print("Server health history: inserted back-dated rows.")

    # Deterministic acceptance-test scenario: IFSAPP02 / WARNING / 22-Oct to 25-Nov 2026
    # Keep exactly 15 matching rows in the local demo DB so the filter example is reproducible.
    warning_count = cur.execute(
        "SELECT COUNT(*) FROM server_health_history WHERE server_name = ? AND UPPER(status) = ? "
        "AND checked_time >= ? AND checked_time <= ?",
        ("IFSAPP02", "WARNING", "2026-10-22 00:00:00", "2026-11-25 23:59:59")
    ).fetchone()[0]
    for idx in range(max(0, 15 - warning_count)):
        day = 22 + (warning_count + idx) % 15
        hour = (warning_count + idx) % 24
        checked = f"2026-10-{day:02d} {hour:02d}:15:00" if day <= 31 else f"2026-11-{day-31:02d} {hour:02d}:15:00"
        cur.execute(
            "INSERT INTO server_health_history (server_name, cpu_percent, memory_percent, disk_percent, status, checked_time) "
            "VALUES (?,?,?,?,?,?)",
            ("IFSAPP02", 82.0 + (idx % 4), 84.0 + (idx % 3), 78.0 + (idx % 2), "WARNING", checked)
        )
    if warning_count < 15:
        print(f"IFSAPP02 warning history: added {15-warning_count} row(s); demo filter now has 15 WARNING records.")

    # --- API health history ---
    if cur.execute("SELECT COUNT(*) FROM api_health_history").fetchone()[0] == 0:
        apis = [("Customer API", "/api/customers", 200, 180), ("Order API", "/api/orders", 200, 250),
                ("Payment API", "/api/payments", 503, 5000), ("Inventory API", "/api/inventory", 200, 2500)]
        for name, ep, http, rt in apis:
            for k in range(40):
                t = now - timedelta(hours=20 * k + random.randint(0, 5))
                if http == 503:
                    code, ms = random.choice([(503, 5000), (503, 4800), (200, 900), (500, 5000)])
                else:
                    code, ms = 200, max(60, int(rt * random.uniform(0.4, 1.6)))
                st = "DOWN" if code >= 500 else "DEGRADED" if ms >= 2000 else "UP"
                cur.execute("INSERT INTO api_health_history (api_name, endpoint, http_status, response_time_ms, status, "
                            "checked_time) VALUES (?,?,?,?,?,?)", (name, ep, code, ms, st, t.strftime(FMT)))
        print("API health history: inserted back-dated rows.")

    # --- Database health snapshots ---
    if seed_once(cur, "database_health", "health_id", 8100, "Database health"):
        for i in range(40):
            t = now - timedelta(hours=18 * (i + 1) + random.randint(0, 4))
            cpu, mem = random.randint(25, 92), random.randint(40, 90)
            st = "CRITICAL" if cpu >= 90 else "WARNING" if cpu >= 80 else "HEALTHY"
            cur.execute("INSERT INTO database_health (health_id, database_name, captured_time, connection_status, "
                        "cpu_percent, memory_percent, status) VALUES (?,?,?,?,?,?,?)",
                        (8100 + i, "IFS_DATABASE", t.strftime(FMT), "CONNECTED", cpu, mem, st))

    # --- Print jobs ---
    if seed_once(cur, "print_jobs", "print_job_id", 3100, "Print jobs"):
        reports = ["Customer Invoice", "Purchase Order", "Picking List", "Delivery Note", "Pay Slip"]
        for i in range(24):
            st = random.choices(["FAILED", "COMPLETED", "QUEUED", "PROCESSING"], [5, 6, 2, 1])[0]
            created = back()
            cur.execute("INSERT INTO print_jobs (print_job_id, report_name, printer_name, state, submitted_by, page_count, "
                        "error_text, created_time, completed_time) VALUES (?,?,?,?,?,?,?,?,?)",
                        (3100 + i, random.choice(reports), random.choice(["PRINTER01", "PRINTER02", "PRINTER03"]), st,
                         random.choice(["FINUSER1", "SCMUSER1", "WMSUSER"]), random.randint(1, 12),
                         random.choice(["Printer unavailable", "Spooler service stopped", "Paper jam reported"]) if st == "FAILED" else None,
                         created.strftime(FMT), (created + timedelta(minutes=1)).strftime(FMT) if st == "COMPLETED" else None))

    # --- Database locks ---
    if seed_once(cur, "database_locks", "lock_id", 100, "Database locks"):
        objs = ["CUSTOMER_ORDER_TAB", "PURCHASE_ORDER_TAB", "INVENTORY_PART_TAB", "SUPPLIER_INVOICE_TAB"]
        for i in range(14):
            cur.execute("INSERT INTO database_locks (lock_id, blocking_session_id, blocked_session_id, object_name, "
                        "lock_type, wait_seconds, created_time) VALUES (?,?,?,?,?,?,?)",
                        (100 + i, random.choice([101, 104]), random.choice([102, 103]), random.choice(objs),
                         "ROW LOCK", random.randint(30, 1200), back().strftime(FMT)))

    # --- Interface errors (unacknowledged ones are counted on the dashboard) ---
    if seed_once(cur, "interface_errors", "error_id", 6100, "Interface errors"):
        msgs = ["Invalid customer number", "Connection timeout", "Authentication failed for destination API",
                "Required field missing in request", "Destination system returned HTTP 500"]
        for i in range(20):
            cur.execute("INSERT INTO interface_errors (error_id, interface_name, error_message, acknowledged, created_time) "
                        "VALUES (?,?,?,?,?)",
                        (6100 + i, random.choice(["CustomerSync", "ShipmentSync", "InvoiceExport", "OrderExport"]),
                         random.choice(msgs), random.choices([0, 1], [2, 3])[0], back().strftime(FMT)))


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    ensure_job_columns(cur)

    # ---- Background jobs (IDs 1100+ are seeded rows) ----
    if cur.execute("SELECT COUNT(*) FROM background_jobs WHERE job_id >= 1100").fetchone()[0] == 0:
        job_id = 1100
        for state, n in JOB_PER_STATE.items():
            for _ in range(n):
                name, queue, err = random.choice(JOB_NAMES[state])
                created = back()
                updated = created + timedelta(minutes=random.randint(1, 90))
                end = None if state in ("EXECUTING", "POSTED") else updated.strftime(FMT)
                cur.execute(
                    "INSERT INTO background_jobs (job_id, job_name, queue_name, state, progress_percent, "
                    "start_time, end_time, error_message, created_time, updated_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job_id, name, queue, state, 100 if state in ("READY", "WARNING") else random.randint(0, 80),
                     created.strftime(FMT), end, err, created.strftime(FMT), updated.strftime(FMT)))
                job_id += 1
        print("Inserted back-dated background jobs.")
    else:
        print("Background job seed rows already present - skipped.")

    # ---- Application messages (IDs 2100+ are seeded rows) ----
    total_msgs = cur.execute("SELECT COUNT(*) FROM application_messages").fetchone()[0]
    if total_msgs < 100 and cur.execute("SELECT COUNT(*) FROM application_messages WHERE message_id >= 2100").fetchone()[0] == 0:
        msg_id = 2100
        for state, n in MSG_PER_STATE.items():
            for _ in range(n):
                mtype, src, dst = random.choice(MSG_TYPES)
                created = back()
                updated = created + timedelta(minutes=random.randint(1, 60))
                cur.execute(
                    "INSERT INTO application_messages (message_id, message_type, source_system, destination_system, "
                    "state, retry_count, error_text, created_time, updated_time) VALUES (?,?,?,?,?,?,?,?,?)",
                    (msg_id, mtype, src, dst, state, random.randint(1, 5) if state == "FAILED" else 0,
                     random.choice(MSG_ERRORS) if state == "FAILED" else None,
                     created.strftime(FMT), updated.strftime(FMT)))
                msg_id += 1
        print("Inserted back-dated application messages.")
    else:
        print("Application messages already have enough rows - skipped.")

    seed_extra(cur)

    con.commit()
    for t in ("background_jobs", "application_messages"):
        print(t, cur.execute(f"SELECT state, COUNT(*) FROM {t} GROUP BY state").fetchall())
    con.close()


if __name__ == "__main__":
    main()
