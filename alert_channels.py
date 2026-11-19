"""
Email + SMS delivery for dashboard notifications.

How it fits together
--------------------
app_local.py's scanner stores every new event in ``dashboard_notifications`` (that is what feeds the bell).
This module looks at those rows and, when an event qualifies, sends an email and an SMS to every configured
recipient. It never looks at the monitoring tables itself, so there is exactly one definition of "an event".

Duplicate protection (an event is never sent twice)
---------------------------------------------------
* Each notification row has per-channel delivery columns (email_status / sms_status ...). A row whose channel
  status is 'sent' is never processed again for that channel, and a recipient that already received it is
  skipped on a retry.
* Cooldown: while the same condition keeps producing new rows (a server that stays CRITICAL creates a new
  history row every check) only ONE email/SMS goes out per ALERT_COOLDOWN_MINUTES.
* Rows that already existed when this feature was first switched on are marked 'skipped: existing' - turning
  it on never sends a flood about old history. Events older than ALERT_MAX_AGE_HOURS are skipped the same way.
* A burst (more than ALERT_MAX_INDIVIDUAL new alerts at once) is sent as ONE digest email / SMS.

Settings live in .env (see .env.example) - nothing here needs code changes to change recipients or rules.
SMTP uses the standard library; SMS supports Twilio or any HTTP gateway (webhook). No extra packages needed.
"""
import base64
import json
import logging
import os
import smtplib
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr

from db_wrapper import DatabaseWrapper

log = logging.getLogger("alert-channels")

TABLE = "dashboard_notifications"
TS_FMT = "%Y-%m-%d %H:%M:%S"
DELIVERY_COLUMNS = (
    ("email_status", "TEXT"), ("email_sent_to", "TEXT"),
    ("sms_status", "TEXT"), ("sms_sent_to", "TEXT"),
    ("delivery_attempts", "INTEGER DEFAULT 0"), ("last_attempt_at", "TEXT"),
)

# Which alert types go to which channel (user-approved matrix):
#   always   = send for every event of this type
#   critical = send only when the event severity is CRITICAL
#   never    = dashboard only
DEFAULT_RULES = ("failed_job=critical,failed_message=critical,critical_server=always,critical_database=always,"
                 "api_failure=always,open_critical_alert=always")
VALID_RULES = ("always", "critical", "never")

_lock = threading.Lock()          # one delivery run at a time (prevents double sends)
_schema_ready = False


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _env(name, default=""):
    return (os.getenv(name, default) or "").strip()


def _flag(name, default="false"):
    return _env(name, default).lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _list(name):
    return [x.strip() for x in _env(name).replace(";", ",").split(",") if x.strip()]


def _rules(name):
    rules = {}
    for chunk in (DEFAULT_RULES + "," + _env(name)).split(","):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            if value.strip().lower() in VALID_RULES:
                rules[key.strip()] = value.strip().lower()
    return rules


def get_config():
    """Read settings on every use, so editing .env + restarting is all that is ever needed."""
    return {
        "env_name": _env("ENV_NAME", "ERP Monitoring"),
        "dashboard_url": _env("DASHBOARD_URL"),
        # critical-server e-mails are sent by server_critical_alert.py (one e-mail listing all critical servers)
        "server_summary": _flag("SERVER_CRITICAL_ALERT_ENABLED", "true"),
        "max_age_hours": _int("ALERT_MAX_AGE_HOURS", 24),
        "cooldown_min": _int("ALERT_COOLDOWN_MINUTES", 60),
        "max_individual": max(1, _int("ALERT_MAX_INDIVIDUAL", 5)),
        "max_attempts": max(1, _int("ALERT_MAX_ATTEMPTS", 4)),
        "retry_after_min": max(1, _int("ALERT_RETRY_MINUTES", 5)),
        "email": {
            "enabled": _flag("ALERT_EMAIL_ENABLED"),
            "to": _list("ALERT_EMAIL_TO"),
            "host": _env("SMTP_HOST"), "port": _int("SMTP_PORT", 587),
            "user": _env("SMTP_USER"), "password": os.getenv("SMTP_PASS", ""),
            "security": _env("SMTP_SECURITY", "starttls").lower(),     # starttls | ssl | none
            "from": _env("ALERT_EMAIL_FROM") or _env("SMTP_USER") or "erp-monitor@localhost",
            "from_name": _env("ALERT_EMAIL_FROM_NAME"),      # optional display name, e.g. Zenova Matrix
            "rules": _rules("ALERT_EMAIL_RULES"),
        },
        "sms": {
            "enabled": _flag("ALERT_SMS_ENABLED"),
            "to": _list("ALERT_SMS_TO"),
            "provider": _env("SMS_PROVIDER", "twilio").lower(),         # twilio | webhook
            "twilio_sid": _env("TWILIO_ACCOUNT_SID"), "twilio_token": _env("TWILIO_AUTH_TOKEN"),
            "twilio_from": _env("TWILIO_FROM"), "twilio_service": _env("TWILIO_MESSAGING_SERVICE_SID"),
            "twilio_base": _env("TWILIO_API_BASE", "https://api.twilio.com").rstrip("/"),
            "webhook_url": _env("SMS_WEBHOOK_URL"), "webhook_token": _env("SMS_WEBHOOK_TOKEN"),
            "rules": _rules("ALERT_SMS_RULES"),
        },
    }


def _problem(channel, cfg):
    """Return why a channel that is switched on cannot send yet, or '' when it is ready."""
    c = cfg[channel]
    if not c["to"]:
        return f"no recipients (ALERT_{channel.upper()}_TO is empty)"
    if channel == "email":
        return "" if c["host"] else "SMTP_HOST is not set"
    if c["provider"] == "twilio":
        if not (c["twilio_sid"] and c["twilio_token"]):
            return "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set"
        return "" if (c["twilio_from"] or c["twilio_service"]) else "TWILIO_FROM is not set"
    if c["provider"] == "webhook":
        return "" if c["webhook_url"] else "SMS_WEBHOOK_URL is not set"
    return f"unknown SMS_PROVIDER '{c['provider']}'"


def mask(value, channel):
    if channel == "email" and "@" in value:
        name, domain = value.split("@", 1)
        return name[:1] + "***@" + domain
    return "*" * max(0, len(value) - 4) + value[-4:]


def status_summary():
    """Safe-to-show view of the configuration (recipients are masked, secrets never included)."""
    cfg = get_config()
    out = {}
    for channel in ("email", "sms"):
        c = cfg[channel]
        out[channel] = {
            "enabled": c["enabled"],
            "ready": c["enabled"] and not _problem(channel, cfg),
            "problem": _problem(channel, cfg) if c["enabled"] else "",
            "recipients": [mask(x, channel) for x in c["to"]],
            "rules": c["rules"],
        }
    out["sms"]["provider"] = cfg["sms"]["provider"]
    out["max_age_hours"], out["cooldown_minutes"] = cfg["max_age_hours"], cfg["cooldown_min"]
    return out


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------
def send_email(cfg, recipient, subject, body, html=None):
    c = cfg["email"]
    msg = EmailMessage()
    sender = c["from"]
    if c.get("from_name") and "<" not in sender:       # ALERT_EMAIL_FROM_NAME + ALERT_EMAIL_FROM -> "Name <address>"
        sender = formataddr((c["from_name"], sender))
    msg["Subject"], msg["From"], msg["To"] = subject, sender, recipient
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    if c["security"] == "ssl":
        smtp = smtplib.SMTP_SSL(c["host"], c["port"], timeout=20, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(c["host"], c["port"], timeout=20)
    with smtp:
        smtp.ehlo()
        if c["security"] == "starttls":
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if c["user"]:
            smtp.login(c["user"], c["password"])
        refused = smtp.send_message(msg)
        if refused:
            raise RuntimeError(f"recipient refused: {refused}")


def send_sms(cfg, recipient, text):
    c = cfg["sms"]
    if c["provider"] == "twilio":
        data = {"To": recipient, "Body": text}
        data["MessagingServiceSid" if c["twilio_service"] else "From"] = c["twilio_service"] or c["twilio_from"]
        req = urllib.request.Request(
            f"{c['twilio_base']}/2010-04-01/Accounts/{c['twilio_sid']}/Messages.json",
            data=urllib.parse.urlencode(data).encode(), method="POST")
        auth = base64.b64encode(f"{c['twilio_sid']}:{c['twilio_token']}".encode()).decode()
        req.add_header("Authorization", "Basic " + auth)
    else:  # generic HTTP gateway: POST {"to": "...", "message": "..."} (+ optional bearer token)
        req = urllib.request.Request(c["webhook_url"], data=json.dumps({"to": recipient, "message": text}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        if c["webhook_token"]:
            req.add_header("Authorization", "Bearer " + c["webhook_token"])
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"SMS gateway HTTP {error.code}: {detail}") from None


# ---------------------------------------------------------------------------
# Message text
# ---------------------------------------------------------------------------
_SKIP_DETAIL_KEYS = {"password", "token", "secret"}


def _clean(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _detail_lines(details):
    lines = []
    for key, value in (details or {}).items():
        if value in (None, "") or str(key).lower() in _SKIP_DETAIL_KEYS:
            continue
        lines.append(f"  {str(key).replace('_', ' ').title()}: {_clean(value, 300)}")
    return lines[:20]


def email_for(cfg, rows):
    env = cfg["env_name"]
    link = f"\nDashboard: {cfg['dashboard_url']}\n" if cfg["dashboard_url"] else ""
    if len(rows) == 1:
        r = rows[0]
        subject = f"[{env}] {r['severity']}: {_clean(r['title'], 120)}"
        body = (f"{r['title']}\n{'=' * min(len(r['title']), 70)}\n\n"
                f"Severity   : {r['severity']}\nTime       : {r['when']}\nEnvironment: {env}\n\n"
                f"Description:\n  {r['message']}\n\nSystem details:\n" + "\n".join(_detail_lines(r["details"])) + f"\n{link}")
        return subject, body
    subject = f"[{env}] {len(rows)} new alerts need attention"
    parts = [f"{len(rows)} new alerts were detected on {env}:\n"]
    for i, r in enumerate(rows, 1):
        parts.append(f"{i}. [{r['severity']}] {r['title']}\n   {r['when']}\n   {_clean(r['message'], 300)}\n")
    return subject, "\n".join(parts) + link


def sms_for(cfg, rows):
    env = cfg["env_name"]
    if len(rows) == 1:
        r = rows[0]
        return _clean(f"[{env}] {r['severity']}: {r['title']} - {r['message']} ({r['when']})", 300)
    return _clean(f"[{env}] {len(rows)} new alerts: " + "; ".join(_clean(r["title"], 50) for r in rows[:4])
                  + ("; ..." if len(rows) > 4 else "") + ". Check the dashboard.", 300)


# ---------------------------------------------------------------------------
# Database bookkeeping
# ---------------------------------------------------------------------------
def ensure_schema(db):
    """Add the delivery columns to dashboard_notifications once (SQLite + PostgreSQL)."""
    global _schema_ready
    if _schema_ready:
        return
    existing = set(db.get_columns(TABLE))
    if not existing:
        return                                           # table not created yet - scanner will do that first
    added = False
    for name, ddl in DELIVERY_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}")
            added = True
    if added:
        # First time switched on: everything already stored is history, never send it.
        db.execute(f"UPDATE {TABLE} SET email_status = 'skipped: existing', sms_status = 'skipped: existing', "
                   f"delivery_attempts = 0 WHERE email_status IS NULL AND sms_status IS NULL")
        log.info("Email/SMS delivery columns added; existing notifications marked as not to be sent.")
    _schema_ready = True


def _parse_time(text):
    text = str(text or "").replace("T", " ")[:19]
    try:
        return datetime.strptime(text, TS_FMT)
    except ValueError:
        return None


def _load_row(row):
    try:
        details = json.loads(row.get("details") or "{}")
    except (TypeError, ValueError):
        details = {}
    when = row.get("event_time") or row.get("created_at") or ""
    return {
        "id": int(row["notification_id"]), "type": row["event_type"], "title": row["title"],
        "message": _clean(row["message"], 600), "severity": str(row["severity"]).upper(), "details": details,
        "when": _clean(when, 40), "when_dt": _parse_time(row.get("event_time")) or _parse_time(row.get("created_at")),
        "email_status": row.get("email_status"), "sms_status": row.get("sms_status"),
        "email_sent_to": [x for x in (row.get("email_sent_to") or "").split("|") if x],
        "sms_sent_to": [x for x in (row.get("sms_sent_to") or "").split("|") if x],
        "attempts": int(row.get("delivery_attempts") or 0), "last_attempt": _parse_time(row.get("last_attempt_at")),
    }


def _policy_skip(channel, cfg, row, now):
    """Return a reason this row must NOT be sent on this channel, or '' when it qualifies."""
    if channel == "email" and row["type"] == "critical_server" and cfg["server_summary"]:
        return "sent by the critical-server summary e-mail"
    rule = cfg[channel]["rules"].get(row["type"], "never")
    if rule == "never":
        return "dashboard only for this alert type"
    if rule == "critical" and row["severity"] != "CRITICAL":
        return f"{row['severity'].lower()} severity (only critical is sent)"
    if row["when_dt"] and now - row["when_dt"] > timedelta(hours=cfg["max_age_hours"]):
        return f"older than {cfg['max_age_hours']}h"
    return ""


def _in_cooldown(db, channel, cfg, row, now):
    """True if the same alert type + subject was already sent on this channel within the cooldown window."""
    cutoff = (now - timedelta(minutes=cfg["cooldown_min"])).strftime(TS_FMT)
    hit = db.fetch_one(
        f"SELECT notification_id FROM {TABLE} WHERE event_type = :t AND title = :title AND notification_id <> :id "
        f"AND {channel}_status = 'sent' AND last_attempt_at >= :cutoff LIMIT 1",
        {"t": row["type"], "title": row["title"], "id": row["id"], "cutoff": cutoff})
    return bool(hit)


# ---------------------------------------------------------------------------
# The delivery run
# ---------------------------------------------------------------------------
def process_pending(db=None):
    """
    Send email / SMS for every notification that has not been handled yet, and retry the ones that failed.
    Safe to call as often as you like (every scan does): finished rows are never touched again.
    Returns {"email": n_sent_messages, "sms": n_sent_messages}.
    """
    if not _lock.acquire(blocking=False):
        return {"email": 0, "sms": 0, "busy": True}
    own = db is None
    result = {"email": 0, "sms": 0}
    try:
        cfg = get_config()
        if not (cfg["email"]["enabled"] or cfg["sms"]["enabled"]):
            return result
        db = db or DatabaseWrapper()
        ensure_schema(db)
        if not _schema_ready:
            return result
        now = datetime.now()
        raw = db.fetch_all(
            f"SELECT * FROM {TABLE} WHERE email_status IS NULL OR sms_status IS NULL "
            f"OR ((email_status LIKE 'failed%' OR sms_status LIKE 'failed%') AND COALESCE(delivery_attempts, 0) < :max) "
            f"ORDER BY notification_id ASC LIMIT 500", {"max": cfg["max_attempts"]})
        rows = [_load_row(r) for r in raw]
        if not rows:
            return result
        updates = {r["id"]: {} for r in rows}

        for channel in ("email", "sms"):
            c = cfg[channel]
            todo = []
            for r in rows:
                status = r[f"{channel}_status"]
                retry_due = (status or "").startswith("failed") and r["attempts"] < cfg["max_attempts"] and (
                    r["last_attempt"] is None or now - r["last_attempt"] >= timedelta(minutes=cfg["retry_after_min"]))
                if status is None:
                    if not c["enabled"]:
                        updates[r["id"]][f"{channel}_status"] = f"skipped: {channel} disabled"
                        continue
                    why = _policy_skip(channel, cfg, r, now)
                    if why:
                        updates[r["id"]][f"{channel}_status"] = f"skipped: {why}"
                    elif _in_cooldown(db, channel, cfg, r, now):
                        updates[r["id"]][f"{channel}_status"] = f"skipped: same alert already sent in the last {cfg['cooldown_min']} min"
                    else:
                        todo.append(r)
                elif retry_due:
                    todo.append(r)
            # one message per (type, subject) inside a run - a sustained condition is ONE alert
            seen, unique = {}, []
            for r in todo:
                key = (r["type"], r["title"])
                if key in seen:
                    updates[r["id"]][f"{channel}_status"] = f"skipped: duplicate of #{seen[key]} in the same run"
                else:
                    seen[key] = r["id"]
                    unique.append(r)
            if not unique:
                continue
            problem = _problem(channel, cfg)
            if problem:
                for r in unique:
                    updates[r["id"]].update({f"{channel}_status": f"failed: {problem}", "_attempt": True})
                log.warning("%s alerts waiting but %s", channel, problem)
                continue

            groups = [unique] if len(unique) > cfg["max_individual"] else [[r] for r in unique]
            for group in groups:
                needed = [x for x in c["to"] if any(x not in r[f"{channel}_sent_to"] for r in group)]
                errors, delivered = [], []
                for recipient in needed:
                    try:
                        if channel == "email":
                            subject, body = email_for(cfg, group)
                            send_email(cfg, recipient, subject, body)
                        else:
                            send_sms(cfg, recipient, sms_for(cfg, group))
                        delivered.append(recipient)
                        result[channel] += 1
                    except Exception as error:
                        log.error("%s to %s failed: %s", channel, mask(recipient, channel), error)
                        errors.append(f"{mask(recipient, channel)}: {_clean(error, 120)}")
                for r in group:
                    sent_to = list(dict.fromkeys(r[f"{channel}_sent_to"] + delivered))
                    ok = all(x in sent_to for x in c["to"])
                    updates[r["id"]].update({
                        f"{channel}_status": "sent" if ok else "failed: " + "; ".join(errors)[:300],
                        f"{channel}_sent_to": "|".join(sent_to), "_attempt": True})

        for r in rows:
            u = updates[r["id"]]
            if not u:
                continue
            attempted = u.pop("_attempt", False)
            cols = dict(u)
            if attempted:
                cols["delivery_attempts"] = r["attempts"] + 1
                cols["last_attempt_at"] = now.strftime(TS_FMT)
            sets = ", ".join(f"{k} = :{k}" for k in cols)
            db.execute(f"UPDATE {TABLE} SET {sets} WHERE notification_id = :nid", {**cols, "nid": r["id"]})
        if result["email"] or result["sms"]:
            log.info("Alert delivery: %d email(s), %d SMS sent.", result["email"], result["sms"])
        return result
    finally:
        _lock.release()
        if own and db is not None:
            db.close()


def process_pending_async():
    """Fire-and-forget, so a slow SMTP / SMS gateway never delays the dashboard."""
    threading.Thread(target=_safe_process, name="alert-delivery", daemon=True).start()


def _safe_process():
    try:
        process_pending()
    except Exception:
        log.exception("Alert delivery run failed")


# ---------------------------------------------------------------------------
# Manual test (bypasses rules, cooldown and age; used by the test endpoint / script)
# ---------------------------------------------------------------------------
def send_test():
    cfg = get_config()
    now = datetime.now().strftime(TS_FMT)
    row = {"id": 0, "type": "test", "title": "Test alert from the ERP Monitoring Dashboard", "severity": "CRITICAL",
           "when": now, "message": "If you received this, email/SMS alerts are configured correctly.",
           "details": {"source": "manual test", "status": "TEST"}}
    report = {}
    for channel in ("email", "sms"):
        c = cfg[channel]
        if not c["enabled"]:
            report[channel] = {"ok": False, "detail": f"ALERT_{channel.upper()}_ENABLED is false"}
            continue
        problem = _problem(channel, cfg)
        if problem:
            report[channel] = {"ok": False, "detail": problem}
            continue
        results = {}
        for recipient in c["to"]:
            try:
                if channel == "email":
                    send_email(cfg, recipient, *email_for(cfg, [row]))
                else:
                    send_sms(cfg, recipient, sms_for(cfg, [row]))
                results[mask(recipient, channel)] = "sent"
            except Exception as error:
                results[mask(recipient, channel)] = "failed: " + _clean(error, 200)
        report[channel] = {"ok": all(v == "sent" for v in results.values()), "recipients": results}
    return report
