import csv
import io
import re
from flask import Flask, Response, jsonify, render_template, render_template_string, request, make_response
from datetime import datetime, date
from db_wrapper import DatabaseWrapper
import alert_channels
import server_critical_alert

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Optional dashboard login (HTTP Basic). Active only when DASH_USER and DASH_PASS are set in .env.
# /health stays open so uptime monitors can call it.
# ---------------------------------------------------------------------------
@app.before_request
def require_login():
    import hmac, os
    user, password = os.getenv("DASH_USER", ""), os.getenv("DASH_PASS", "")
    if not user or not password or request.path == "/health":
        return None
    auth = request.authorization
    if auth and hmac.compare_digest(auth.username or "", user) and hmac.compare_digest(auth.password or "", password):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="ERP Monitoring Dashboard"'})


# =========================================================
# DATABASE CONNECTION - USING THE COMMON DB WRAPPER
# =========================================================

def get_count(query):
    db = DatabaseWrapper()
    try:
        row = db.fetch_one(query)
        if row is None:
            return 0
        return next(iter(row.values()), 0) or 0
    finally:
        db.close()


def get_rows(query):
    db = DatabaseWrapper()
    try:
        return db.fetch_all(query)
    finally:
        db.close()


def table_exists(table_name):
    db = DatabaseWrapper()
    try:
        return db.table_exists(table_name)
    finally:
        db.close()


def get_columns(table_name):
    db = DatabaseWrapper()
    try:
        return db.get_columns(table_name)
    finally:
        db.close()
        
        
# =========================================================
# DASHBOARD
# =========================================================

@app.route("/")
def dashboard():

    # =====================================================
    # KPI COUNTS
    # =====================================================

    # Active Users
    active_users = get_count(
        """
        SELECT COUNT(*)
        FROM active_users
        WHERE status = 'ACTIVE'
        """
    )

    # Active Database Sessions
    active_sessions = get_count(
        """
        SELECT COUNT(*)
        FROM database_sessions
        WHERE status = 'ACTIVE'
        """
    )

    # Background Jobs in Error
    jobs_error = get_count(
        """
        SELECT COUNT(*)
        FROM background_jobs
        WHERE state = 'ERROR'
        """
    )

    # Executing Background Jobs
    executing_jobs = get_count(
        """
        SELECT COUNT(*)
        FROM background_jobs
        WHERE state = 'EXECUTING'
        """
    )

    # Failed Application Messages
    failed_messages = get_count(
        """
        SELECT COUNT(*)
        FROM application_messages
        WHERE state = 'FAILED'
        """
    )

    # Waiting Application Messages
    waiting_messages = get_count(
        """
        SELECT COUNT(*)
        FROM application_messages
        WHERE state = 'WAITING'
        """
    )

    # Failed Print Jobs
    # IMPORTANT: print_jobs uses "state", NOT "status"
    failed_print_jobs = get_count(
        """
        SELECT COUNT(*)
        FROM print_jobs
        WHERE state = 'FAILED'
        """
    )

    # =====================================================
    # DATABASE LOCKS
    # =====================================================

    # IMPORTANT:
    # The local database_locks table does NOT contain
    # "blocking_session".
    #
    # Therefore we count the records directly.

    blocking_locks = get_count(
        """
        SELECT COUNT(*)
        FROM database_locks
        """
    )

    # Longest Lock Wait
    lock_columns = get_columns("database_locks")

    if "wait_seconds" in lock_columns:

        longest_lock_wait = get_count(
            """
            SELECT COALESCE(MAX(wait_seconds), 0)
            FROM database_locks
            """
        )

    else:

        longest_lock_wait = 0

    # =====================================================
    # ALERTS
    # =====================================================

    open_alerts = get_count(
        """
        SELECT COUNT(*)
        FROM monitoring_alerts
        WHERE status = 'OPEN'
        """
    )

    critical_alerts = get_count(
        """
        SELECT COUNT(*)
        FROM monitoring_alerts
        WHERE status = 'OPEN'
        AND severity = 'CRITICAL'
        """
    )

    # =====================================================
    # INTERFACE ERRORS
    # =====================================================

    unack_errors = get_count(
        """
        SELECT COUNT(*)
        FROM interface_errors
        WHERE acknowledged = 0
        """
    )

    # =====================================================
    # BACKGROUND JOBS
    # =====================================================

    background_job_data = {}

    if table_exists("background_jobs"):

        background_jobs = get_rows(
            """
            SELECT state, COUNT(*) AS total
            FROM background_jobs
            GROUP BY state
            ORDER BY state
            """
        )

        background_job_data = {
            row["state"]: row["total"]
            for row in background_jobs
        }

    # =====================================================
    # APPLICATION MESSAGES
    # =====================================================

    application_message_data = {}

    if table_exists("application_messages"):

        application_messages = get_rows(
            """
            SELECT state, COUNT(*) AS total
            FROM application_messages
            GROUP BY state
            ORDER BY state
            """
        )

        application_message_data = {
            row["state"]: row["total"]
            for row in application_messages
        }

    # =====================================================
    # SERVER HEALTH
    # =====================================================

    server_health = []

    server_columns = get_columns("server_health")

    required_server_columns = {
        "server_name",
        "cpu_percent",
        "memory_percent",
        "disk_percent",
        "status"
    }

    if required_server_columns.issubset(set(server_columns)):

        server_health = get_rows(
            """
            SELECT
                server_name,
                cpu_percent,
                memory_percent,
                disk_percent,
                status
            FROM server_health
            ORDER BY server_name
            """
        )

    # =====================================================
    # API HEALTH
    # =====================================================

    api_health = []

    api_columns = get_columns("api_health")

    if "api_name" in api_columns and "status" in api_columns:

        if "response_time_ms" in api_columns:

            api_health = get_rows(
                """
                SELECT
                    api_name,
                    status,
                    response_time_ms
                FROM api_health
                ORDER BY api_name
                """
            )

        else:

            api_health = get_rows(
                """
                SELECT
                    api_name,
                    status
                FROM api_health
                ORDER BY api_name
                """
            )

    # =====================================================
    # ALERT SUMMARY
    # =====================================================

    alert_data = {}

    if table_exists("monitoring_alerts"):

        alert_summary = get_rows(
            """
            SELECT
                severity,
                COUNT(*) AS total
            FROM monitoring_alerts
            WHERE status = 'OPEN'
            GROUP BY severity
            """
        )

        alert_data = {
            row["severity"]: row["total"]
            for row in alert_summary
        }

    # =====================================================
    # DATABASE HEALTH
    # =====================================================

    latest_database_health = None

    database_health_columns = get_columns("database_health")

    if database_health_columns:

        # If checked_at exists, get the latest record.
        if "checked_at" in database_health_columns:

            database_health = get_rows(
                """
                SELECT *
                FROM database_health
                ORDER BY checked_at DESC
                LIMIT 1
                """
            )

        else:

            database_health = get_rows(
                """
                SELECT *
                FROM database_health
                LIMIT 1
                """
            )

        if database_health:
            latest_database_health = database_health[0]

    # =====================================================
    # RENDER DASHBOARD
    # =====================================================

    return render_template(
        "dashboard.html",
        last_updated=datetime.now().strftime("%d %b %Y, %H:%M:%S"),

        # KPI values
        active_users=active_users,
        active_sessions=active_sessions,
        jobs_error=jobs_error,
        executing_jobs=executing_jobs,
        failed_messages=failed_messages,
        waiting_messages=waiting_messages,
        failed_print_jobs=failed_print_jobs,
        blocking_locks=blocking_locks,
        longest_lock_wait=longest_lock_wait,
        open_alerts=open_alerts,
        critical_alerts=critical_alerts,
        unack_errors=unack_errors,

        # Dashboard data
        background_job_data=background_job_data,
        application_message_data=application_message_data,
        server_health=server_health,
        api_health=api_health,
        alert_data=alert_data,
        database_health=latest_database_health
    )


# =========================================================
# DATE-WISE REPORTS AND ERROR DETAILS
# =========================================================

def valid_date(value):
    """Return a validated YYYY-MM-DD date string, or None when blank."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("Dates must use YYYY-MM-DD format.")


def date_conditions(start_date, end_date, column="created_time"):
    """Build safe SQL conditions after validating date input."""
    conditions = []
    if start_date:
        conditions.append(f"{column} >= '{start_date} 00:00:00'")
    if end_date:
        conditions.append(f"{column} <= '{end_date} 23:59:59'")
    return conditions


@app.route("/report")
def report():
    try:
        start_date = valid_date(request.args.get("start_date", ""))
        end_date = valid_date(request.args.get("end_date", ""))
    except ValueError as error:
        return str(error), 400

    if start_date and end_date and start_date > end_date:
        return "Start date cannot be after end date.", 400

    conditions = date_conditions(start_date, end_date)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    try:
        messages = get_rows(
            "SELECT message_id, message_type, source_system, destination_system, "
            "state, retry_count, error_text, created_time, updated_time "
            "FROM application_messages" + where_clause + " ORDER BY created_time DESC"
        )
        errors = get_rows(
            "SELECT error_id, interface_name, error_message, acknowledged, created_time "
            "FROM interface_errors" + where_clause + " ORDER BY created_time DESC"
        )
        alerts = get_rows(
            "SELECT alert_id, alert_name, severity, status, message, created_time "
            "FROM monitoring_alerts" + where_clause + " ORDER BY created_time DESC"
        )
    except Exception as error:
        app.logger.exception("Unable to load date-wise report")
        return f"Unable to load report. Check the application log: {error}", 500

    return render_template_string(
        REPORT_HTML,
        start_date=start_date or "",
        end_date=end_date or "",
        messages=messages,
        errors=errors,
        alerts=alerts,
    )


@app.route("/details/errors")
def error_details():
    try:
        start_date = valid_date(request.args.get("start_date", ""))
        end_date = valid_date(request.args.get("end_date", ""))
    except ValueError as error:
        return str(error), 400

    if start_date and end_date and start_date > end_date:
        return "Start date cannot be after end date.", 400

    conditions = ["acknowledged = 0"]
    conditions.extend(date_conditions(start_date, end_date))

    try:
        errors = get_rows(
            "SELECT error_id, interface_name, error_message, acknowledged, created_time "
            "FROM interface_errors WHERE " + " AND ".join(conditions) +
            " ORDER BY created_time DESC"
        )
    except Exception as error:
        app.logger.exception("Unable to load interface error details")
        return f"Unable to load error details. Check the application log: {error}", 500

    return render_template_string(
        ERRORS_HTML,
        errors=errors,
        start_date=start_date or "",
        end_date=end_date or "",
    )


# =========================================================
# CLICK-THROUGH DETAILS  (every card / panel on the dashboard)
# =========================================================
# One JSON endpoint powers every pop-up on the dashboard:
#   /api/details/jobs/ERROR?start_date=2026-10-01&end_date=2026-10-31
#   /api/details/servers/IFSAPP02
#   /api/details/apis/ALL
# Use ALL as the value to see every record of that category.
# Add &format=csv to download the filtered rows as a CSV report.
#
# To make a NEW card clickable, add one entry to DETAIL_SOURCES below and put
#   class="js-detail" data-cat="<key>" data-value="<VALUE or ALL>"
# on the button in templates/dashboard.html.

MAX_DETAIL_ROWS = 500

DETAIL_SOURCES = {
    "jobs": {
        "title": "Background Jobs", "table": "background_jobs",
        "filter_col": "state", "fixed_where": "", "date_col": "{created}",
        "columns": [  # (json key, SQL expression, heading)
            ("id", "job_id", "Job ID"), ("name", "job_name", "Job Name"), ("state", "state", "Status"),
            ("description", "error_message", "Error Description"),
            ("created", "{created}", "Created At"), ("updated", "{updated}", "Updated At")],
    },
    "messages": {
        "title": "Application Messages", "table": "application_messages",
        "filter_col": "state", "fixed_where": "", "date_col": "created_time",
        "columns": [
            ("id", "message_id", "ID"), ("name", "message_type", "Type"), ("source", "source_system", "Source"),
            ("destination", "destination_system", "Destination"), ("state", "state", "Status"),
            ("description", "error_text", "Error Description"), ("retries", "retry_count", "Retries"),
            ("created", "created_time", "Created At"), ("updated", "updated_time", "Updated At")],
    },
    "alerts": {
        "title": "Open Alerts", "table": "monitoring_alerts",
        "filter_col": "severity", "fixed_where": "status = 'OPEN'", "date_col": "created_time",
        "columns": [
            ("id", "alert_id", "Alert ID"), ("name", "alert_name", "Alert"), ("state", "severity", "Severity"),
            ("status", "status", "Status"), ("description", "message", "Description"),
            ("created", "created_time", "Created At")],
    },
    "servers": {
        "title": "Server Health History", "table": "server_health_history",
        "filter_col": "server_name", "fixed_where": "", "date_col": "recorded_at",
        "columns": [
            ("id", "history_id", "History ID"), ("name", "server_name", "Server"),
            ("state", "health_status", "Health Status"), ("cpu", "cpu_usage", "CPU %"),
            ("memory", "memory_usage", "Memory %"), ("disk", "disk_usage", "Disk %"),
            ("response", "response_time", "Response Time"), ("description", "error_description", "Error Description"),
            ("created", "recorded_at", "Recorded At")],
    },
    "apis": {
        "title": "API Health History", "table": "api_health_history",
        "filter_col": "api_name", "fixed_where": "", "date_col": "checked_at",
        "columns": [
            ("id", "history_id", "History ID"), ("name", "api_name", "API"), ("endpoint", "endpoint", "Endpoint"),
            ("state", "api_status", "Status"), ("http", "http_status_code", "HTTP Status"),
            ("response", "response_time", "Response Time"), ("description", "error_description", "Error Description"),
            ("created", "checked_at", "Checked At"), ("updated", "updated_at", "Updated At")],
    },
    "dbhealth": {
        "title": "Database Health History", "table": "database_health_history",
        "filter_col": "health_status", "fixed_where": "", "date_col": "recorded_at",
        "columns": [
            ("id", "history_id", "History ID"), ("name", "database_name", "Database"),
            ("type", "database_type", "Database Type"), ("connection", "connection_status", "Connection"),
            ("state", "health_status", "Health Status"), ("cpu", "cpu_usage", "CPU %"),
            ("memory", "memory_usage", "Memory %"), ("sessions", "active_sessions", "Active Sessions"),
            ("description", "error_description", "Error Description"), ("created", "recorded_at", "Recorded At")],
    },
    "printjobs": {
        "title": "Print Jobs", "table": "print_jobs",
        "filter_col": "state", "fixed_where": "", "date_col": "created_time",
        "columns": [
            ("id", "print_job_id", "ID"), ("name", "report_name", "Report"), ("printer", "printer_name", "Printer"),
            ("state", "state", "Status"), ("user", "submitted_by", "Submitted By"),
            ("description", "error_text", "Error Description"), ("created", "created_time", "Created At"),
            ("updated", "completed_time", "Completed At")],
    },
    "locks": {
        "title": "Database Locks", "table": "database_locks",
        "filter_col": None, "fixed_where": "", "date_col": "created_time",
        "columns": [
            ("id", "lock_id", "Lock ID"), ("name", "object_name", "Object"), ("type", "lock_type", "Lock Type"),
            ("blocking", "blocking_session_id", "Blocking Session"), ("blocked", "blocked_session_id", "Blocked Session"),
            ("wait", "wait_seconds", "Wait (sec)"), ("created", "created_time", "Created At")],
    },
    "errors": {
        "title": "Unacknowledged Interface Errors", "table": "interface_errors",
        "filter_col": "interface_name", "fixed_where": "acknowledged = 0", "date_col": "created_time",
        "columns": [
            ("id", "error_id", "Error ID"), ("name", "interface_name", "Interface"),
            ("description", "error_message", "Error Description"), ("created", "created_time", "Created At")],
    },
    "users": {
        "title": "Active Users", "table": "active_users",
        "filter_col": None, "fixed_where": "status = 'ACTIVE'", "date_col": "login_time",
        "columns": [
            ("id", "user_id", "User ID"), ("name", "username", "Username"), ("fullname", "full_name", "Name"),
            ("dept", "department", "Department"), ("state", "status", "Status"),
            ("created", "login_time", "Login Time"), ("updated", "last_activity_time", "Last Activity")],
    },
    "sessions": {
        "title": "Active DB Sessions", "table": "database_sessions",
        "filter_col": None, "fixed_where": "status = 'ACTIVE'", "date_col": "logon_time",
        "columns": [
            ("id", "session_id", "Session ID"), ("name", "username", "User"), ("machine", "machine", "Machine"),
            ("module", "module", "Module"), ("state", "status", "Status"),
            ("idle", "last_call_seconds", "Last Call (sec)"), ("created", "logon_time", "Logon Time")],
    },
}


# =========================================================
# EXPORT (PDF / XLSX / CSV / XLS) - shared by all six report sections
# =========================================================
EXPORT_FORMATS = ("pdf", "xlsx", "csv", "xls")


def _cell_text(v):
    return "" if v is None else str(v)


def _xml_escape(v):
    from xml.sax.saxutils import escape
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", escape(_cell_text(v)))


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def build_export(fmt, category, title, value, start_date, end_date, total, columns, rows):
    """Return a Flask Response that downloads the filtered records in the requested format."""
    labels = [label for _k, _e, label in columns]
    keys = [key for key, _e, _l in columns]
    matrix = [[row.get(k) for k in keys] for row in rows]
    summary = f"Filter: {value} | Start: {start_date or 'Any'} | End: {end_date or 'Any'} | Records: {total}"
    safe_value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "ALL"
    base = f"{category}_{safe_value}_{start_date or 'all'}_to_{end_date or 'all'}"
    sheet_name = re.sub(r"[\\/*?:\[\]]", " ", title)[:31]

    def send(data, mimetype, ext):
        return Response(data, mimetype=mimetype,
                        headers={"Content-Disposition": f'attachment; filename="{base}.{ext}"'})

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(labels)
        for r in matrix:
            writer.writerow([_cell_text(v) for v in r])
        # BOM so Excel opens UTF-8 text correctly
        return send("\ufeff" + buf.getvalue(), "text/csv; charset=utf-8", "csv")

    if fmt == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name or "Report"
        ws.append(labels)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="172B4D")
            c.alignment = Alignment(vertical="center")
        for r in matrix:
            ws.append([v if (_is_number(v) or v is None) else _cell_text(v) for v in r])
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(labels))}{len(matrix) + 1}"
        for i, label in enumerate(labels, 1):
            longest = max([len(label)] + [len(_cell_text(r[i - 1])) for r in matrix[:500]])
            ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 60)
        out = io.BytesIO()
        wb.save(out)
        return send(out.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx")

    if fmt == "xls":
        try:
            import xlwt  # true binary .xls (Excel 97-2003)
        except ImportError:
            xlwt = None
        if xlwt is not None:
            wb = xlwt.Workbook(encoding="utf-8")
            ws = wb.add_sheet(sheet_name or "Report")
            head = xlwt.easyxf("font: bold on, colour white; pattern: pattern solid, fore_colour dark_blue;")
            for c, label in enumerate(labels):
                ws.write(0, c, label, head)
                ws.col(c).width = 256 * min(max(len(label) + 4, 12), 50)
            for r, rowvals in enumerate(matrix[:65535], 1):  # .xls limit is 65,536 rows
                for c, v in enumerate(rowvals):
                    if v is not None:
                        ws.write(r, c, v if _is_number(v) else _cell_text(v))
            ws.set_panes_frozen(True)
            ws.set_horz_split_pos(1)
            out = io.BytesIO()
            wb.save(out)
            return send(out.getvalue(), "application/vnd.ms-excel", "xls")
        # Fallback with no extra package: Excel 2003 XML Spreadsheet, opens in Excel/LibreOffice as .xls
        parts = ['<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<?mso-application progid="Excel.Sheet"?>\n'
                 '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
                 'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
                 '<Styles><Style ss:ID="h"><Font ss:Bold="1" ss:Color="#FFFFFF"/>'
                 '<Interior ss:Color="#172B4D" ss:Pattern="Solid"/></Style></Styles>'
                 f'<Worksheet ss:Name="{_xml_escape(sheet_name or "Report")}"><Table>']
        parts.append("<Row>" + "".join(f'<Cell ss:StyleID="h"><Data ss:Type="String">{_xml_escape(l)}</Data></Cell>' for l in labels) + "</Row>")
        for r in matrix:
            cells = []
            for v in r:
                if v is None:
                    cells.append("<Cell/>")
                elif _is_number(v):
                    cells.append(f'<Cell><Data ss:Type="Number">{v}</Data></Cell>')
                else:
                    cells.append(f'<Cell><Data ss:Type="String">{_xml_escape(v)}</Data></Cell>')
            parts.append("<Row>" + "".join(cells) + "</Row>")
        parts.append("</Table></Worksheet></Workbook>")
        return send("".join(parts).encode("utf-8"), "application/vnd.ms-excel", "xls")

    # ---- PDF ----
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.units import mm
    from xml.sax.saxutils import escape
    pdf_buffer = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buffer, pagesize=landscape(A4), rightMargin=12*mm, leftMargin=12*mm,
                            topMargin=12*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7, leading=8.5)
    head_style = ParagraphStyle("head", parent=cell_style, fontName="Helvetica-Bold", textColor=colors.white)
    story = [Paragraph(escape(title), styles["Title"]), Paragraph(escape(summary), styles["Normal"]), Spacer(1, 8)]
    data = [[Paragraph(escape(l), head_style) for l in labels]]
    for r in matrix:
        data.append([Paragraph(escape(_cell_text(v) if v is not None else "—"), cell_style) for v in r])
    tbl = Table(data, repeatRows=1)
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#172b4d")),
                             ("GRID", (0, 0), (-1, -1), .35, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5fa")])]))
    story.append(tbl)

    def page_number(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(landscape(A4)[0] - 12*mm, 7*mm, f"Page {document.page}")
        canvas.restoreState()
    doc.build(story, onFirstPage=page_number, onLaterPages=page_number)
    return send(pdf_buffer.getvalue(), "application/pdf", "pdf")


@app.route("/api/details/<category>/<value>")
def api_details(category, value):
    source = DETAIL_SOURCES.get(category)
    if source is None:
        return jsonify(error="Unknown category."), 404

    try:
        start_date = valid_date(request.args.get("start_date", ""))
        end_date = valid_date(request.args.get("end_date", ""))
    except ValueError as error:
        return jsonify(error=str(error)), 400

    if start_date and end_date and start_date > end_date:
        return jsonify(error="Start date cannot be after end date."), 400

    table = source["table"]
    available = get_columns(table)
    if not available and category == "dbhealth":
        table = "database_health"
        available = get_columns(table)
    if not available:
        return jsonify(error=f"Table {table} was not found. Run: python seed_backdated_data.py"), 404

    # Older copies of background_jobs have no created/updated columns
    created_col = "created_time" if "created_time" in available else "start_time"
    updated_col = "updated_time" if "updated_time" in available else "end_time"
    fill = {"created": created_col, "updated": updated_col}
    date_candidates = {
        "server_health_history": ["recorded_at", "checked_time"],
        "api_health_history": ["checked_at", "checked_time"],
        "database_health_history": ["recorded_at", "checked_at"],
        "database_health": ["recorded_at", "captured_time", "checked_at"],
    }
    date_col = next((c for c in date_candidates.get(table, [source["date_col"].format(**fill)]) if c in available), None)
    if not date_col:
        return jsonify(error=f"No supported date column found in {table}. Available columns: {', '.join(available)}"), 500
    column_aliases = {
        "health_status": ["health_status", "status"], "api_status": ["api_status", "status"],
        "cpu_usage": ["cpu_usage", "cpu_percent"], "memory_usage": ["memory_usage", "memory_percent"],
        "disk_usage": ["disk_usage", "disk_percent"], "http_status_code": ["http_status_code", "http_status"],
        "response_time": ["response_time", "response_time_ms"], "error_description": ["error_description", "error_message"],
        "recorded_at": ["recorded_at", "checked_time", "captured_time"], "checked_at": ["checked_at", "checked_time"],
        "updated_at": ["updated_at", "updated_time"], "database_name": ["database_name"],
        "history_id": ["history_id", "health_id"], "database_type": ["database_type"], "active_sessions": ["active_sessions"],
        "connection_status": ["connection_status"], "database_name": ["database_name"],
    }
    # Resolve the current schema or a known earlier schema without altering data.
    selected_columns = []
    for key, expr, label in source["columns"]:
        expr = expr.format(**fill)
        candidate = next((c for c in column_aliases.get(expr, [expr]) if c in available), None)
        if candidate:
            selected_columns.append((key, candidate, label))
    if not selected_columns:
        return jsonify(error=f"No compatible detail columns found in {table}."), 500
    select_parts = [f"{expr} AS {key}" for key, expr, _label in selected_columns]

    # Values are always bound parameters - never pasted into the SQL text
    where, params = [], {}
    if source["fixed_where"]:
        where.append(source["fixed_where"])
    show_all = value.upper() == "ALL"
    filter_col = source["filter_col"]
    if filter_col and filter_col not in available:
        aliases = {"health_status": "status", "api_status": "status", "state": "status"}
        filter_col = aliases.get(filter_col, filter_col)
    if filter_col and filter_col in available and not show_all:
        where.append(f"{filter_col} = :value")
        params["value"] = value
    # Server/API cards carry their current status into the detail view. This is
    # an implicit filter (not a user-facing dropdown) and remains active while
    # the user changes the date range. Other categories continue using the
    # status/severity represented by the clicked dashboard row, if any.
    locked_status = request.args.get("locked_status", "").strip().upper()
    if locked_status and category in ("servers", "apis"):
        status_candidates = ("health_status", "status") if category == "servers" else ("api_status", "status")
        status_col = next((c for c in status_candidates if c in available), None)
        if status_col:
            where.append(f"UPPER({status_col}) = :locked_status")
            params["locked_status"] = locked_status
    base_where = " WHERE " + " AND ".join(where) if where else ""

    where_dates = list(where)
    if start_date:
        where_dates.append(f"{date_col} >= :start_dt")
        params["start_dt"] = f"{start_date} 00:00:00"
    if end_date:
        where_dates.append(f"{date_col} <= :end_dt")
        params["end_dt"] = f"{end_date} 23:59:59"
    where_sql = " WHERE " + " AND ".join(where_dates) if where_dates else ""

    base_params = {k: v for k, v in params.items() if k in ("value", "locked_status")}

    db = DatabaseWrapper()
    try:
        total = (db.fetch_one(f"SELECT COUNT(*) AS c FROM {table}{where_sql}", params) or {}).get("c", 0)
        rows = db.fetch_all(
            f"SELECT {', '.join(select_parts)} FROM {table}{where_sql} "
            f"ORDER BY {date_col} DESC",
            params,
        )
        bounds = db.fetch_one(
            f"SELECT MIN({date_col}) AS first_date, MAX({date_col}) AS last_date FROM {table}{base_where}",
            base_params,
        ) or {}
    except Exception as error:
        app.logger.exception("Unable to load details")
        return jsonify(error=f"Unable to load records: {error}"), 500
    finally:
        db.close()

    export_format = (request.args.get("format") or "").strip().lower()
    if export_format:
        if export_format not in EXPORT_FORMATS:
            return jsonify(error=f"Unsupported format '{export_format}'. Use: {', '.join(EXPORT_FORMATS)}."), 400
        try:
            return build_export(export_format, category, source["title"], value, start_date, end_date,
                                total, selected_columns, rows)
        except Exception as error:
            app.logger.exception("%s export failed", export_format.upper())
            return jsonify(error=f"{export_format.upper()} generation failed: {error}"), 500

    # The page builds the full title ("<title> — <item> • <STATUS> (<count>)") from this plain title.
    return jsonify(
        title=source["title"],
        value="ALL" if show_all else value,
        total=total,
        shown=len(rows),
        columns=[{"key": k, "label": label} for k, _e, label in selected_columns],
        rows=rows,
        first_date=(bounds.get("first_date") or "")[:10],
        last_date=(bounds.get("last_date") or "")[:10],
    )



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
            known_keys = {r["event_key"] for r in db.fetch_all("SELECT event_key FROM dashboard_notifications")}
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
                    if event["event_key"] in known_keys:
                        continue
                    known_keys.add(event["event_key"])
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
        # Email / SMS for the notifications that were just stored (and retries). Runs in the background.
        alert_channels.process_pending_async()
        return inserted
    finally:
        db.close()


@app.get("/api/notifications")
def api_notifications():
    try:
        new_events = scan_monitoring_events()
        db = DatabaseWrapper()
        try:
            extra = [c for c in ("email_status", "sms_status") if c in db.get_columns("dashboard_notifications")]
            rows = db.fetch_all("SELECT notification_id,event_type,title,message,severity,details,event_time,is_read,created_at"
                                + "".join("," + c for c in extra) + " FROM dashboard_notifications ORDER BY notification_id DESC LIMIT 100")
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

# =========================================================
# EMAIL / SMS ALERT CHANNELS  (logic lives in alert_channels.py, settings in .env)
# =========================================================

@app.get("/api/alert-channels")
def api_alert_channels():
    """Which channels are on, who receives alerts (masked) and the rules. Secrets are never returned."""
    return jsonify(ok=True, **alert_channels.status_summary())


@app.post("/api/alert-channels/test")
def api_alert_channels_test():
    """Send a real test email + SMS to every configured recipient right now."""
    try:
        return jsonify(ok=True, result=alert_channels.send_test())
    except Exception as error:
        app.logger.exception("Alert channel test failed")
        return jsonify(ok=False, error=str(error)[:300]), 500


@app.post("/api/server-critical-alert/run")
def api_server_critical_alert_run():
    """Check server health now and e-mail the critical servers. Add ?force=1 to send even if already alerted."""
    result = server_critical_alert.check_and_notify(force=request.args.get("force") == "1")
    return jsonify(result), (200 if result.get("ok") or result.get("skipped") else 500)


def start_background_scanner():
    """Scan + send email/SMS every ALERT_SCAN_SECONDS even when nobody has the dashboard open."""
    import os, time
    interval = max(10, int(os.getenv("ALERT_SCAN_SECONDS", "30") or 30))

    def loop():
        time.sleep(5)
        while True:
            try:
                scan_monitoring_events()
            except Exception:
                app.logger.exception("Background notification scan failed")
            try:
                server_critical_alert.check_and_notify()      # e-mail when any server is CRITICAL
            except Exception:
                app.logger.exception("Critical-server e-mail check failed")
            time.sleep(interval)

    threading.Thread(target=loop, name="notification-scanner", daemon=True).start()
    app.logger.info("Background notification scanner running every %ss.", interval)


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():
    db = None
    try:
        db = DatabaseWrapper()
        db.test_connection()
        return jsonify(status="ok", database="connected", database_type=db.db_type)
    except Exception as e:
        return jsonify(status="down", database="disconnected", error=str(e)), 500
    finally:
        if db is not None:
            db.close()


# The dashboard page now lives in templates/dashboard.html


# =========================================================
# DATE-WISE REPORT HTML
# =========================================================

REPORT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>ERP Date-wise Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; background: #f4f6f8; color: #222; }
        header { background: #172b4d; color: white; padding: 20px 28px; }
        main { max-width: 1500px; margin: auto; padding: 22px; }
        .panel { background: white; padding: 18px; margin: 18px 0; border-radius: 9px; box-shadow: 0 2px 8px #00000012; overflow-x: auto; }
        label { display: inline-block; margin: 5px 10px 5px 0; }
        input, button { padding: 9px; }
        button { cursor: pointer; background: #175cd3; color: white; border: 0; border-radius: 4px; }
        table { border-collapse: collapse; width: 100%; font-size: 14px; }
        th, td { padding: 10px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: top; }
        th { background: #172b4d; color: white; }
        a { color: #175cd3; }
        .muted { color: #666; }
    </style>
</head>
<body>
<header><h1>ERP Date-wise Report</h1><p>Application messages, interface errors, and monitoring alerts</p></header>
<main>
    <p><a href="/">← Back to Dashboard</a> | <a href="/details/errors">Unacknowledged Error Details</a></p>
    <section class="panel">
        <form method="get" action="/report">
            <label>Start date <input type="date" name="start_date" value="{{ start_date }}"></label>
            <label>End date <input type="date" name="end_date" value="{{ end_date }}"></label>
            <button type="submit">Filter Report</button>
            <a href="/report">Clear dates</a>
        </form>
        <p class="muted">Leave either date blank to keep that side of the range open.</p>
    </section>
    <section class="panel">
        <h2>Application Messages ({{ messages|length }})</h2>
        <table><thead><tr><th>ID</th><th>Type</th><th>Source</th><th>Destination</th><th>State</th><th>Retries</th><th>Error Description</th><th>Created</th><th>Updated</th></tr></thead><tbody>
        {% for r in messages %}<tr><td>{{ r.message_id }}</td><td>{{ r.message_type }}</td><td>{{ r.source_system }}</td><td>{{ r.destination_system }}</td><td>{{ r.state }}</td><td>{{ r.retry_count }}</td><td>{{ r.error_text or "—" }}</td><td>{{ r.created_time }}</td><td>{{ r.updated_time }}</td></tr>{% else %}<tr><td colspan="9">No application messages found for this date range.</td></tr>{% endfor %}
        </tbody></table>
    </section>
    <section class="panel">
        <h2>Interface Errors ({{ errors|length }})</h2>
        <table><thead><tr><th>ID</th><th>Interface</th><th>Description</th><th>Acknowledged</th><th>Created</th></tr></thead><tbody>
        {% for r in errors %}<tr><td>{{ r.error_id }}</td><td>{{ r.interface_name }}</td><td>{{ r.error_message }}</td><td>{{ "Yes" if r.acknowledged else "No" }}</td><td>{{ r.created_time }}</td></tr>{% else %}<tr><td colspan="5">No interface errors found for this date range.</td></tr>{% endfor %}
        </tbody></table>
    </section>
    <section class="panel">
        <h2>Monitoring Alerts ({{ alerts|length }})</h2>
        <table><thead><tr><th>ID</th><th>Alert</th><th>Severity</th><th>Status</th><th>Description</th><th>Created</th></tr></thead><tbody>
        {% for r in alerts %}<tr><td>{{ r.alert_id }}</td><td>{{ r.alert_name }}</td><td>{{ r.severity }}</td><td>{{ r.status }}</td><td>{{ r.message }}</td><td>{{ r.created_time }}</td></tr>{% else %}<tr><td colspan="6">No monitoring alerts found for this date range.</td></tr>{% endfor %}
        </tbody></table>
    </section>
</main>
</body>
</html>
"""


# =========================================================
# UNACKNOWLEDGED ERROR DETAILS HTML
# =========================================================

ERRORS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Unacknowledged Interface Errors</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; background: #f4f6f8; color: #222; }
        header { background: #172b4d; color: white; padding: 20px 28px; }
        main { max-width: 1300px; margin: auto; padding: 22px; }
        .panel { background: white; padding: 18px; border-radius: 9px; box-shadow: 0 2px 8px #00000012; overflow-x: auto; }
        label { display: inline-block; margin: 5px 10px 5px 0; }
        input, button { padding: 9px; }
        button { cursor: pointer; background: #175cd3; color: white; border: 0; border-radius: 4px; }
        table { border-collapse: collapse; width: 100%; }
        th, td { padding: 10px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: top; }
        th { background: #172b4d; color: white; }
        a { color: #175cd3; }
    </style>
</head>
<body>
<header><h1>Unacknowledged Interface Errors</h1><p>Actual error records and descriptions</p></header>
<main>
    <p><a href="/">← Back to Dashboard</a> | <a href="/report">All Reports</a></p>
    <section class="panel">
        <form method="get" action="/details/errors">
            <label>Start date <input type="date" name="start_date" value="{{ start_date }}"></label>
            <label>End date <input type="date" name="end_date" value="{{ end_date }}"></label>
            <button type="submit">Filter Errors</button>
            <a href="/details/errors">Clear dates</a>
        </form>
        <h2>Matching unacknowledged errors: {{ errors|length }}</h2>
        <table><thead><tr><th>Error ID</th><th>Interface</th><th>Description</th><th>Created Time</th></tr></thead><tbody>
        {% for r in errors %}<tr><td>{{ r.error_id }}</td><td>{{ r.interface_name }}</td><td>{{ r.error_message }}</td><td>{{ r.created_time }}</td></tr>{% else %}<tr><td colspan="4">No unacknowledged errors found for this date range.</td></tr>{% endfor %}
        </tbody></table>
    </section>
</main>
</body>
</html>
"""


# =========================================================
# START APPLICATION
# =========================================================

if __name__ == "__main__":
    print("Starting ERP Monitoring Dashboard...")

    db = None
    try:
        db = DatabaseWrapper()
        db.test_connection()
        print(f"Database type: {db.db_type}")
        print("Database connection successful!")

    except Exception as error:
        print("Database connection failed:", error)
        raise

    finally:
        if db is not None:
            db.close()

    start_background_scanner()
    print("Open http://127.0.0.1:8080 in your browser")
    app.run(host="127.0.0.1", port=8080, debug=False)