from pathlib import Path
p=Path(__file__).parent/'app_local.py'
s=p.read_text()
insert=r'''

# =========================================================
# PERSISTENT IN-APP NOTIFICATIONS (SQLite / PostgreSQL)
# =========================================================
# This small table is required to persist read/unread state across refreshes.
# Monitoring source tables remain read-only; event_key prevents duplicates.
import hashlib
import json
import threading
from datetime import datetime

_notification_lock = threading.Lock()


def ensure_notification_table(db=None):
    owns_db = db is None
    db = db or DatabaseWrapper()
    try:
        if db.table_exists("dashboard_notifications"):
            return
        if db.db_type in ("postgres", "postgresql"):
            ddl = """CREATE TABLE IF NOT EXISTS dashboard_notifications (
                notification_id BIGSERIAL PRIMARY KEY,
                event_key VARCHAR(255) NOT NULL UNIQUE,
                event_type VARCHAR(80) NOT NULL,
                title VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                severity VARCHAR(20) NOT NULL,
                details TEXT,
                event_time VARCHAR(40),
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at VARCHAR(40) NOT NULL
            )"""
        else:
            ddl = """CREATE TABLE IF NOT EXISTS dashboard_notifications (
                notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key VARCHAR(255) NOT NULL UNIQUE,
                event_type VARCHAR(80) NOT NULL,
                title VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                severity VARCHAR(20) NOT NULL,
                details TEXT,
                event_time VARCHAR(40),
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at VARCHAR(40) NOT NULL
            )"""
        db.execute(ddl)
    finally:
        if owns_db:
            db.close()


def _pick(row, *names, default=None):
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lower and lower[name.lower()] is not None:
            return lower[name.lower()]
    return default


def _truthy_number(v):
    try:
        return float(v) >= 90
    except (TypeError, ValueError):
        return False


def _notification_candidate(table, row):
    """Return a normalized event only when a row represents a qualifying event."""
    state = str(_pick(row, "state", "status", "health_status", "api_status", default="")).upper()
    severity = str(_pick(row, "severity", default="")).upper()
    title = message = event_type = None
    details = dict(row)
    event_time = _pick(row, "created_time", "recorded_at", "checked_at", "checked_time", "captured_time", "updated_time", "start_time")
    if table == "background_jobs" and state in ("ERROR", "FAILED"):
        event_type, severity = "failed_job", "CRITICAL"
        title = f"Background Job Failed — #{_pick(row, 'job_id', 'id', default='Unknown')}"
        message = str(_pick(row, "error_message", "description", default=f"Job {_pick(row, 'job_name', 'name', default='')} failed"))
    elif table == "application_messages" and state in ("FAILED", "ERROR"):
        event_type, severity = "failed_message", "CRITICAL"
        title = f"Application Message Failed — #{_pick(row, 'message_id', 'id', default='Unknown')}"
        message = str(_pick(row, "error_text", "error_message", "message", default="Application message failed"))
    elif table == "monitoring_alerts" and str(_pick(row, "status", default="")).upper() == "OPEN" and severity == "CRITICAL":
        event_type = "open_critical_alert"
        title = str(_pick(row, "alert_name", default="Open Critical Alert"))
        message = str(_pick(row, "message", "description", default="A critical alert remains open"))
    elif table == "server_health_history":
        critical = state in ("CRITICAL", "DOWN", "FAILED", "UNHEALTHY") or any(_truthy_number(_pick(row, col)) for col in ("cpu_usage", "cpu_percent", "memory_usage", "memory_percent", "disk_usage", "disk_percent"))
        if critical:
            event_type, severity = "critical_server", "CRITICAL"
            title = f"Critical Server Alert — {_pick(row, 'server_name', 'name', default='Server')}"
            message = str(_pick(row, "error_description", "error_message", default=f"Server health is {state or 'critical'}"))
    elif table == "api_health_history":
        http = _pick(row, "http_status_code", "http_status")
        try:
            http_failed = int(http) >= 500
        except (TypeError, ValueError):
            http_failed = False
        if state in ("DOWN", "FAILED", "UNAVAILABLE", "ERROR") or http_failed:
            event_type, severity = "api_failure", "CRITICAL"
            title = f"API Failure — {_pick(row, 'api_name', default='API')}"
            message = str(_pick(row, "error_description", "error_message", default=f"API status: {state or http}"))
    elif table in ("database_health_history", "database_health"):
        critical = state in ("CRITICAL", "DOWN", "FAILED", "UNHEALTHY") or any(_truthy_number(_pick(row, col)) for col in ("cpu_usage", "cpu_percent", "memory_usage", "memory_percent", "disk_usage", "disk_percent"))
        if critical:
            event_type, severity = "critical_database", "CRITICAL"
            title = f"Critical Database Alert — {_pick(row, 'database_name', default='Database')}"
            message = str(_pick(row, "error_description", "error_message", default=f"Database health is {state or 'critical'}"))
    if not title:
        return None
    identity = _pick(row, "job_id", "message_id", "alert_id", "history_id", "health_id", "api_id", "id")
    if identity is None:
        identity = json.dumps(details, sort_keys=True, default=str)
    raw_key = f"{table}:{event_type}:{identity}:{event_time or ''}"
    event_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return {"event_key": event_key, "event_type": event_type, "title": title,
            "message": message[:2000], "severity": severity or "WARNING",
            "details": json.dumps(details, default=str), "event_time": str(event_time or ""),
            "created_at": datetime.utcnow().isoformat(timespec="seconds")}


def scan_monitoring_events():
    """Scan existing monitoring tables and persist only previously unseen events."""
    db = DatabaseWrapper()
    inserted = []
    try:
        ensure_notification_table(db)
        sources = ("background_jobs", "application_messages", "monitoring_alerts",
                   "server_health_history", "api_health_history", "database_health_history", "database_health")
        with _notification_lock:
            for table in sources:
                if not db.table_exists(table):
                    continue
                try:
                    rows = db.fetch_all(f"SELECT * FROM {table}")
                except Exception:
                    app.logger.exception("Notification scan failed for %s", table)
                    continue
                for row in rows:
                    event = _notification_candidate(table, row)
                    if not event:
                        continue
                    exists = db.fetch_one("SELECT notification_id FROM dashboard_notifications WHERE event_key = :event_key", {"event_key": event["event_key"]})
                    if exists:
                        continue
                    try:
                        db.execute("""INSERT INTO dashboard_notifications
                            (event_key,event_type,title,message,severity,details,event_time,is_read,created_at)
                            VALUES (:event_key,:event_type,:title,:message,:severity,:details,:event_time,0,:created_at)""", event)
                        created = db.fetch_one("SELECT notification_id FROM dashboard_notifications WHERE event_key = :event_key", {"event_key": event["event_key"]})
                        if created:
                            event["notification_id"] = created.get("notification_id")
                        inserted.append(event)
                    except Exception:
                        # A concurrent scanner may have inserted the same unique event key.
                        app.logger.debug("Notification already exists: %s", event["event_key"])
        return inserted
    finally:
        db.close()


@app.get("/api/notifications")
def api_notifications():
    try:
        new_events = scan_monitoring_events()
        db = DatabaseWrapper()
        try:
            rows = db.fetch_all("""SELECT notification_id,event_type,title,message,severity,details,event_time,is_read,created_at
                FROM dashboard_notifications ORDER BY notification_id DESC LIMIT 100""")
            unread = db.fetch_one("SELECT COUNT(*) AS count FROM dashboard_notifications WHERE is_read = 0") or {"count": 0}
        finally:
            db.close()
        for row in rows:
            try:
                row["details"] = json.loads(row.get("details") or "{}")
            except (TypeError, ValueError):
                row["details"] = {}
            row["is_read"] = bool(row.get("is_read"))
        return jsonify(ok=True, notifications=rows, unread_count=unread.get("count", 0), new_ids=[e.get("notification_id") for e in new_events if e.get("notification_id")])
    except Exception as error:
        app.logger.exception("Unable to load notifications")
        return jsonify(ok=False, error=str(error)[:300], notifications=[], unread_count=0), 500


@app.post("/api/notifications/<int:notification_id>/read")
def api_notification_read(notification_id):
    db = DatabaseWrapper()
    try:
        ensure_notification_table(db)
        db.execute("UPDATE dashboard_notifications SET is_read = 1 WHERE notification_id = :id", {"id": notification_id})
        return jsonify(ok=True, notification_id=notification_id)
    finally:
        db.close()


@app.post("/api/notifications/read-all")
def api_notifications_read_all():
    db = DatabaseWrapper()
    try:
        ensure_notification_table(db)
        db.execute("UPDATE dashboard_notifications SET is_read = 1 WHERE is_read = 0")
        return jsonify(ok=True)
    finally:
        db.close()


@app.post("/api/notifications/test")
def api_notification_test():
    db = DatabaseWrapper()
    try:
        ensure_notification_table(db)
        event = {"event_key": "test:" + hashlib.sha256(datetime.utcnow().isoformat().encode()).hexdigest(),
                 "event_type": "test", "title": "Test Notification", "message": "Browser and in-app notification test succeeded.",
                 "severity": "WARNING", "details": json.dumps({"source": "manual test", "status": "TEST"}),
                 "event_time": datetime.utcnow().isoformat(timespec="seconds"), "created_at": datetime.utcnow().isoformat(timespec="seconds")}
        db.execute("""INSERT INTO dashboard_notifications
            (event_key,event_type,title,message,severity,details,event_time,is_read,created_at)
            VALUES (:event_key,:event_type,:title,:message,:severity,:details,:event_time,0,:created_at)""", event)
        return jsonify(ok=True)
    finally:
        db.close()
'''
# Insert before health check so routes and helpers are registered.
marker='\n# =========================================================\n# HEALTH CHECK\n# ========================================================='
assert marker in s
s=s.replace(marker, insert+marker)
# add severity filter to details route after filter-col/value condition
old='''    if filter_col and filter_col in available and not show_all:\n        where.append(f"{filter_col} = :value")\n        params["value"] = value\n    base_where = " WHERE " + " AND ".join(where) if where else ""\n'''
new='''    if filter_col and filter_col in available and not show_all:\n        where.append(f"{filter_col} = :value")\n        params["value"] = value\n    # Optional independent status/severity filter (particularly useful for server history).\n    severity_filter = request.args.get("severity", "").strip().upper()\n    if severity_filter and severity_filter != "ALL":\n        severity_col = next((c for c in ("severity", "health_status", "api_status", "state", "status") if c in available), None)\n        if severity_col:\n            where.append(f"UPPER({severity_col}) = :severity")\n            params["severity"] = severity_filter\n    base_where = " WHERE " + " AND ".join(where) if where else ""\n'''
assert old in s
s=s.replace(old,new)
# title customize server health when filtered severity
old='''    return jsonify(\n        title=source["title"],\n        value="ALL" if show_all else value,\n        total=total,'''
new='''    display_title = source["title"]\n    if category == "servers" and severity_filter and severity_filter != "ALL":\n        display_title = f"{source['title']} — {value} • {severity_filter}"\n    return jsonify(\n        title=display_title,\n        value="ALL" if show_all else value,\n        total=total,'''
assert old in s
s=s.replace(old,new)
p.write_text(s)
