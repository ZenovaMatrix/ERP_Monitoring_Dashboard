# IFS Cloud Monitor (IFSCOESALES)

Panels: DB locks, Application Message status, Background Job status, Invalid objects, High-BLEVEL indexes.

## Deploy (Windows or Linux)
1. Install **Python 3.10+** (python.org). No Oracle Client needed (python-oracledb thin mode).
2. On the DB, run `sql/create_monitor_user.sql` as a DBA. Change the password.
3. In this folder:
   ```
   python -m venv venv
   venv\Scripts\activate          (Linux: source venv/bin/activate)
   pip install -r requirements.txt
   copy .env.example .env         (Linux: cp .env.example .env)
   ```
4. Edit `.env`: set `DB_DSN=host:1521/IFSCOESALES` (or a TNS alias), `DB_PASSWORD`, `DASH_PASS`.
5. Test connectivity: `python app.py`, then open http://localhost:8080 (login from .env).
   Health check: http://localhost:8080/health
6. Run as a service:
   - Windows: install NSSM, then `nssm install IFSMonitor "<path>\venv\Scripts\python.exe" "<path>\app.py"`, set the startup directory, and `nssm start IFSMonitor`.
   - Linux: create a systemd unit with `ExecStart=<path>/venv/bin/python app.py`, `WorkingDirectory=<path>`, `Restart=always`.
7. Open the firewall port (8080) and put it behind HTTPS (IIS/nginx reverse proxy) before exposing beyond your LAN.

## Verify the IFS table names (important)
Message and job table names differ across IFS versions. Find yours:
```sql
SELECT table_name FROM dba_tables WHERE owner='IFSAPP'
 AND (table_name LIKE '%APPLICATION_MESSAGE%' OR table_name LIKE 'TRANSACTION_SYS_LOCAL%' OR table_name LIKE '%DEFERRED%');
SELECT column_name FROM dba_tab_columns WHERE owner='IFSAPP' AND table_name='<TABLE>';
```
Update `APPMSG_TABLE`, `BGJOB_TABLE`, and the `*_STATE_COL` values in `.env`, grant SELECT on them to IFS_MONITOR, and restart.
A failing panel shows its error without breaking the rest.

## Local dashboard: click-through details & date filter (app_local.py)
```
python local_database.py          # (re)creates monitoring.db from database.sql
python seed_backdated_data.py     # adds back-dated background jobs + application messages
python app_local.py               # http://127.0.0.1:8080
```
Click any status under **Background Jobs** / **Application Messages** (or the Jobs in Error / Executing Jobs /
Failed Messages / Waiting Messages cards) to open a pop-up table with the error description, created/updated time,
a Start/End date filter and an **Export CSV** button. The pop-up is served by
`/api/details/<jobs|messages>/<STATE>?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD[&format=csv]`.

## Updated reporting build (2026-11-03)

This package preserves the existing SQLite/PostgreSQL wrapper and existing database files. The detail API now resolves the timestamp and health columns used by the current historical tables, with compatibility fallbacks for the earlier SQLite schema:

- Server history: `recorded_at` (legacy `checked_time`)
- API history: `checked_at` (legacy `checked_time`)
- Database history: `recorded_at` in `database_health_history`; legacy `captured_time` in `database_health`
- Application messages: `created_time`

The detail API validates reporting dates to 2026, rejects a start date after the end date, includes the full end date through 23:59:59, and returns all matching rows (not just the first 500). The modal's export action now requests PDF and uses the same category, status, and date filters as the displayed records. PDF generation uses ReportLab; install the updated requirements before running.

### Install / run on Windows

```cmd
cd path\to\ERP-Monitoring-Tool-main
python -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set the database values for the database you intend to use. Do not share `.env` or put credentials in source control. Then run:

```cmd
python app_local.py
```

Open `http://127.0.0.1:8080`.

### Important verification note

The code was syntax-checked with `python -m py_compile`. This execution environment did not have Flask/SQLAlchemy/ReportLab installed and could not reach the package index, so end-to-end Flask/database/PDF tests could not be run here. After installing requirements on your Windows machine, test each modal and PDF against your configured PostgreSQL database. This package does not run schema migrations or insert records automatically; it does not drop or reset existing tables.


## Notification center (added)

The local dashboard includes an in-app notification bell with a persistent unread badge, read/unread styling, notification details, **Mark All as Read**, a test notification, browser desktop-notification permission controls, and automatic refresh every 30 seconds. Desktop notifications are emitted for newly detected critical events while the dashboard is open; if browser permission is denied, in-app notifications continue to work.

The notification scanner reads the existing monitoring tables when `/api/notifications` is requested and deduplicates events using a stable event key. It monitors failed background jobs, failed application messages, critical/open monitoring alerts, critical server/database health, and failed/unavailable APIs. The source tables are not modified. A single `dashboard_notifications` table is created automatically on first use because read/unread state must persist across refreshes; SQLite and PostgreSQL DDL are both supported by `db_wrapper.py`.

Useful notification endpoints:
- `GET /api/notifications` — scan source tables, return latest notifications and unread count
- `POST /api/notifications/<id>/read` — mark one notification read
- `POST /api/notifications/read-all` — mark all notifications read
- `POST /api/notifications/test` — create an unread test notification

### Severity/status and date filtering

The detail popup supports an independent **Severity / Status** filter in addition to its category filter and date range. For example, choose Server Health, server `IFSAPP02`, severity `WARNING`, start `2026-10-22`, and end `2026-11-25`. The title is generated from the actual query result, e.g. `Server Health History — IFSAPP02 • WARNING (15)`, and each returned row matches the selected server, severity, and inclusive date range. The local demo database and `seed_backdated_data.py` include a deterministic 15-row WARNING scenario for this example. The same status/severity and date filtering mechanism applies to Background Jobs, Application Messages, Alert Summary, Database Health, and API Health.

After updating, run `python -m pip install -r requirements.txt`, then `python app_local.py` and open `http://127.0.0.1:8080`. The notification table is initialized automatically; no manual migration is required.

## History modal filtering update

- Server Health History opened from an individual server card automatically locks the history to that server's displayed health status (for example, IFSAPP02 + WARNING or IFSAPP03 + CRITICAL).
- API Health History opened from an individual API card automatically locks the history to that API's displayed status (for example, Inventory API + DEGRADED or Payment API + DOWN).
- The selected status remains applied when the start/end date range is changed, and the modal title/count reflects the filtered records.
- The visible `Severity / Status` dropdown has been removed from the shared detail modal. History views use the selected dashboard row/card and date range instead.


## Email + SMS alerts (added)

The bell shows **every** alert. Email and SMS are an extra layer that goes only to the people you list, and only for
the alerts you choose (default table below). Nothing is sent until you switch a channel on in `.env`.

| Alert type | Dashboard | Email | SMS |
|---|---|---|---|
| Failed background job (`failed_job`) | Yes | If critical | If critical |
| Failed application message (`failed_message`) | Yes | If critical | If critical |
| Critical server health (`critical_server`) | Yes | Yes | Yes |
| API failure / downtime (`api_failure`) | Yes | Yes | Yes |
| New or open critical monitoring alert (`open_critical_alert`) | Yes | Yes | Yes |
| Normal / low-severity warning | Yes | No | No |

**Set up** - copy the "EMAIL + SMS ALERTS" block from `.env.example` into your `.env`, then fill in:
`ALERT_EMAIL_TO` (your address and your sir's), `SMTP_*`, `ALERT_SMS_TO` (both mobile numbers with country code, e.g. `+91...`),
and the SMS provider keys (`TWILIO_*`, or `SMS_PROVIDER=webhook` + `SMS_WEBHOOK_URL` for another gateway). Set
`ALERT_EMAIL_ENABLED=true` / `ALERT_SMS_ENABLED=true` and restart `python app_local.py`.
Changing recipients or rules later is only an `.env` edit + restart; no code changes.

**Check it works** - `POST /api/alert-channels/test` (for example `curl -X POST http://127.0.0.1:8080/api/alert-channels/test`)
sends a real test email and SMS to everyone. `GET /api/alert-channels` shows what is enabled (recipients masked, no secrets).

**What each alert contains** - title, severity, time, description and the system details (job ID / server / API / error ...).

**How repeats are prevented**
- Every event is delivered once; the result is stored on the notification (`email_status`, `sms_status`) and shown in the notification popup.
- A server or API that stays down creates a new reading every check, but only **one** email/SMS goes out per `ALERT_COOLDOWN_MINUTES` for the same alert type + server/API.
- When email/SMS is first switched on, everything already in the database is marked "skipped: existing" - old history is never sent. Events older than `ALERT_MAX_AGE_HOURS` are also skipped.
- A burst of many new alerts becomes one summary message per person.
- If the SMTP server or SMS gateway is down, the send is retried (`ALERT_MAX_ATTEMPTS`, every `ALERT_RETRY_MINUTES`) and a person who already received it is not sent it again.

**Database change** - six small columns are added automatically to `dashboard_notifications` (`email_status`, `email_sent_to`,
`sms_status`, `sms_sent_to`, `delivery_attempts`, `last_attempt_at`) the first time a channel is enabled. No new table, no
change to the monitoring tables; works on SQLite and PostgreSQL.

The app now also checks for new events in the background every `ALERT_SCAN_SECONDS`, so email/SMS are sent even when nobody
has the dashboard open. `python test_alert_channels.py` runs a self-test with a fake mail server and fake SMS gateway
(it works on a temporary copy of the database).

## Download / Export dropdown (2026-11-12)

Background Jobs, Application Messages, Server Health, API Health, Alert Summary and Database Health each have a
**⬇ Download** dropdown (PDF, Excel `.xlsx`, CSV `.csv`, XLS `.xls`). The same dropdown replaces the old **Export PDF**
button inside the detail pop-up and exports exactly what the table shows (status + date filter).
Endpoint: `/api/details/<jobs|messages|servers|apis|alerts|dbhealth>/<value>?format=pdf|xlsx|csv|xls[&start_date=..&end_date=..]`.
Run `pip install -r requirements.txt` (adds `openpyxl` and `xlwt`). If `xlwt` is missing, `.xls` falls back to an Excel-readable XML sheet.

## Critical-server e-mail alerts (2026-11-12)

`server_critical_alert.py` checks the current server health (the dashboard's Server Health panel) every
`ALERT_SCAN_SECONDS` and e-mails everyone in `ALERT_EMAIL_TO` when a server is CRITICAL. One e-mail lists every
critical server with its status, CPU %, memory %, disk % and last-checked time. It is sent when a server becomes
critical and again every `SERVER_CRITICAL_REPEAT_MINUTES` while it stays critical. Manual check:
`curl -X POST http://127.0.0.1:8080/api/server-critical-alert/run` (add `?force=1` to send again).

## Deploying (2026-11-19)

1. Copy the `erp_project` folder to the server, create a venv, `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env`; set the e-mail lines, `DASHBOARD_URL=http://<server-ip>:8080`, and `DASH_USER` / `DASH_PASS`.
3. Start with `python run_server.py` (Windows: double-click `start_dashboard.bat`, it restarts the app if it stops).
4. Allow TCP port 8080 in the server firewall, then open `http://<server-ip>:8080` from other computers.
`python app_local.py` is the development server (this computer only, no login) - do not use it for deployment.
