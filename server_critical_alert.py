"""
Critical-server e-mail alert.

Watches the CURRENT health of every server (the same `server_health` table the dashboard's
"Server Health" panel shows) and e-mails ALERT_EMAIL_TO when a server is CRITICAL.
The e-mail lists every server that is currently critical, with its status, CPU %, memory %,
disk % and the time it was checked.

When does an e-mail go out?
  * a server BECOMES critical (it was healthy / warning before, or the app just started and it is critical);
  * a server is STILL critical after SERVER_CRITICAL_REPEAT_MINUTES (reminder, default 60; 0 = never repeat).
A server that recovers is forgotten, so the next time it goes critical you are told again.

State is kept in a tiny table (server_critical_alert_state) so a restart does not re-send.
Settings (.env):
  SERVER_CRITICAL_ALERT_ENABLED=true
  SERVER_CRITICAL_STATUSES=CRITICAL,DOWN,FAILED,UNHEALTHY
  SERVER_CRITICAL_REPEAT_MINUTES=60
Recipients, SMTP details and ALERT_EMAIL_ENABLED are the normal ones (ALERT_EMAIL_TO, SMTP_*).
"""
import logging
import os
import threading
from datetime import datetime, timedelta
from html import escape

import alert_channels
from db_wrapper import DatabaseWrapper

log = logging.getLogger("server-critical-alert")

STATE_TABLE = "server_critical_alert_state"
TS_FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_STATUSES = "CRITICAL,DOWN,FAILED,UNHEALTHY"

_lock = threading.Lock()
_last_failed_at = None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _enabled():
    return (os.getenv("SERVER_CRITICAL_ALERT_ENABLED", "true") or "").strip().lower() in ("1", "true", "yes", "on")


def _statuses():
    raw = os.getenv("SERVER_CRITICAL_STATUSES", DEFAULT_STATUSES) or DEFAULT_STATUSES
    return {s.strip().upper() for s in raw.replace(";", ",").split(",") if s.strip()}


def _repeat_minutes(cfg):
    try:
        return int(os.getenv("SERVER_CRITICAL_REPEAT_MINUTES", str(cfg["cooldown_min"])))
    except ValueError:
        return cfg["cooldown_min"]


# ---------------------------------------------------------------------------
# Reading the current server health
# ---------------------------------------------------------------------------
def _first(columns, *names):
    return next((n for n in names if n in columns), None)


def _read_servers(db):
    """Return [{'name','status','cpu','memory','disk','checked'}] - one row per server, newest data."""
    sources = [("server_health", None), ("server_health_history", "latest")]
    for table, mode in sources:
        cols = db.get_columns(table)
        name_c = _first(cols, "server_name")
        status_c = _first(cols, "status", "health_status")
        if not (cols and name_c and status_c):
            continue
        cpu_c = _first(cols, "cpu_percent", "cpu_usage")
        mem_c = _first(cols, "memory_percent", "memory_usage")
        disk_c = _first(cols, "disk_percent", "disk_usage")
        time_c = _first(cols, "recorded_at", "checked_time", "checked_at", "updated_at", "last_checked")
        select = [f"{name_c} AS name", f"{status_c} AS status"]
        for alias, col in (("cpu", cpu_c), ("memory", mem_c), ("disk", disk_c), ("checked", time_c)):
            select.append(f"{col} AS {alias}" if col else f"NULL AS {alias}")
        if mode == "latest" and time_c:        # history table: keep only each server's newest row
            sql = (f"SELECT {', '.join('h.' + s for s in select)} FROM {table} h "
                   f"JOIN (SELECT {name_c} AS n, MAX({time_c}) AS m FROM {table} GROUP BY {name_c}) x "
                   f"ON h.{name_c} = x.n AND h.{time_c} = x.m")
        else:
            sql = f"SELECT {', '.join(select)} FROM {table}"
        servers = {}
        for row in db.fetch_all(sql):
            servers[str(row["name"])] = row
        return list(servers.values())
    return []


# ---------------------------------------------------------------------------
# State (which critical servers have already been e-mailed)
# ---------------------------------------------------------------------------
def _ensure_state_table(db):
    if not db.table_exists(STATE_TABLE):
        db.execute(f"CREATE TABLE {STATE_TABLE} (server_name VARCHAR(255) PRIMARY KEY, "
                   f"first_seen VARCHAR(40), last_sent_at VARCHAR(40))")


def _load_state(db):
    return {r["server_name"]: r for r in db.fetch_all(f"SELECT server_name, first_seen, last_sent_at FROM {STATE_TABLE}")}


def _save_state(db, name, first_seen, sent_at):
    db.execute(f"DELETE FROM {STATE_TABLE} WHERE server_name = :n", {"n": name})
    db.execute(f"INSERT INTO {STATE_TABLE} (server_name, first_seen, last_sent_at) VALUES (:n, :f, :s)",
               {"n": name, "f": first_seen, "s": sent_at})


def _parse(text):
    try:
        return datetime.strptime(str(text or "")[:19], TS_FMT)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The e-mail
# ---------------------------------------------------------------------------
def _fmt(v, suffix=""):
    if v is None or v == "":
        return "-"
    try:
        return f"{float(v):g}{suffix}"
    except (TypeError, ValueError):
        return f"{v}{suffix}"


def build_email(cfg, critical, new_names, now):
    env = cfg["env_name"]
    names = ", ".join(s["name"] for s in critical)
    subject = f"[{env}] CRITICAL: {len(critical)} server(s) in critical state - {names}"[:180]
    link = cfg["dashboard_url"]

    lines = [f"{len(critical)} server(s) on {env} have a CRITICAL health status.",
             f"Detected at: {now.strftime(TS_FMT)}", ""]
    for i, s in enumerate(critical, 1):
        tag = "NEW" if s["name"] in new_names else "still critical"
        lines += [f"{i}. {s['name']}  [{tag}]",
                  f"   Status : {str(s['status']).upper()}",
                  f"   CPU    : {_fmt(s['cpu'], '%')}",
                  f"   Memory : {_fmt(s['memory'], '%')}",
                  f"   Disk   : {_fmt(s['disk'], '%')}",
                  f"   Last checked: {_fmt(s['checked'])}", ""]
    if link:
        lines.append(f"Dashboard: {link}")
    text = "\n".join(lines)

    th = "padding:8px 10px;background:#172b4d;color:#fff;text-align:left;font-size:13px"
    td = "padding:8px 10px;border-bottom:1px solid #e8ecf4;font-size:13px"
    rows = "".join(
        f"<tr><td style='{td}'><b>{escape(s['name'])}</b></td>"
        f"<td style='{td};color:#b42318;font-weight:700'>{escape(str(s['status']).upper())}</td>"
        f"<td style='{td}'>{escape(_fmt(s['cpu'], '%'))}</td><td style='{td}'>{escape(_fmt(s['memory'], '%'))}</td>"
        f"<td style='{td}'>{escape(_fmt(s['disk'], '%'))}</td><td style='{td}'>{escape(_fmt(s['checked']))}</td>"
        f"<td style='{td}'>{'NEW' if s['name'] in new_names else 'Still critical'}</td></tr>" for s in critical)
    html = (f"<div style='font-family:Segoe UI,Arial,sans-serif;color:#1d2939'>"
            f"<h2 style='color:#b42318;margin:0 0 6px'>Critical server alert</h2>"
            f"<p style='margin:0 0 14px'>{len(critical)} server(s) on <b>{escape(env)}</b> have a CRITICAL health status "
            f"(detected {now.strftime(TS_FMT)}).</p>"
            f"<table style='border-collapse:collapse;width:100%;max-width:760px'><tr>"
            + "".join(f"<th style='{th}'>{h}</th>" for h in ("Server", "Status", "CPU", "Memory", "Disk", "Last checked", "State"))
            + f"</tr>{rows}</table>"
            + (f"<p style='margin-top:16px'><a href='{escape(link)}'>Open the dashboard</a></p>" if link else "")
            + "</div>")
    return subject, text, html


# ---------------------------------------------------------------------------
# The check (called by the background scanner every ALERT_SCAN_SECONDS)
# ---------------------------------------------------------------------------
def check_and_notify(force=False):
    """Look at current server health and e-mail the critical ones. Returns a small report dict."""
    global _last_failed_at
    if not _enabled():
        return {"ok": True, "skipped": "SERVER_CRITICAL_ALERT_ENABLED is false"}
    if not _lock.acquire(blocking=False):
        return {"ok": True, "skipped": "a check is already running"}
    db = None
    try:
        cfg = alert_channels.get_config()
        email = cfg["email"]
        if not email["enabled"]:
            return {"ok": False, "skipped": "ALERT_EMAIL_ENABLED is false"}
        problem = alert_channels._problem("email", cfg)
        if problem:
            return {"ok": False, "skipped": problem}

        db = DatabaseWrapper()
        _ensure_state_table(db)
        now = datetime.now()
        critical_states = _statuses()
        servers = _read_servers(db)
        critical = sorted((s for s in servers if str(s["status"]).strip().upper() in critical_states),
                          key=lambda s: str(s["name"]))
        critical_names = {s["name"] for s in critical}
        state = _load_state(db)

        for name in set(state) - critical_names:           # recovered -> forget, so a new failure alerts again
            db.execute(f"DELETE FROM {STATE_TABLE} WHERE server_name = :n", {"n": name})
            state.pop(name)
        if not critical:
            return {"ok": True, "critical": [], "sent": 0}

        repeat = _repeat_minutes(cfg)
        new_names, due = set(), set()
        for s in critical:
            old = state.get(s["name"])
            last = _parse(old["last_sent_at"]) if old else None
            if old is None or force:
                new_names.add(s["name"]); due.add(s["name"])
            elif repeat > 0 and last and now - last >= timedelta(minutes=repeat):
                due.add(s["name"])
        if not due:
            return {"ok": True, "critical": sorted(critical_names), "sent": 0, "note": "already alerted"}

        wait = timedelta(minutes=cfg["retry_after_min"])
        if _last_failed_at and now - _last_failed_at < wait and not force:
            return {"ok": False, "skipped": "waiting before retrying a failed send"}

        subject, text, html = build_email(cfg, critical, new_names, now)
        delivered, errors = [], []
        for recipient in email["to"]:
            try:
                alert_channels.send_email(cfg, recipient, subject, text, html)
                delivered.append(recipient)
            except Exception as error:
                errors.append(f"{alert_channels.mask(recipient, 'email')}: {error}")
                log.error("Critical-server e-mail to %s failed: %s", alert_channels.mask(recipient, "email"), error)
        if delivered:
            _last_failed_at = None
            stamp = now.strftime(TS_FMT)
            for name in due:
                first = (state.get(name) or {}).get("first_seen") or stamp
                _save_state(db, name, first, stamp)
            log.info("Critical-server e-mail sent to %d recipient(s) for: %s", len(delivered), ", ".join(sorted(due)))
        else:
            _last_failed_at = now
        return {"ok": bool(delivered) and not errors, "critical": sorted(critical_names), "sent": len(delivered),
                "recipients_ok": [alert_channels.mask(r, "email") for r in delivered], "errors": errors}
    except Exception as error:
        log.exception("Critical-server check failed")
        return {"ok": False, "error": str(error)[:300]}
    finally:
        _lock.release()
        if db is not None:
            db.close()
