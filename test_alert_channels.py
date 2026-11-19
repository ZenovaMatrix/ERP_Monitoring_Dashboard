"""
Self-contained test for the email + SMS alert channels.

    python test_alert_channels.py

It copies monitoring.db to a temporary folder (your real database is never touched), starts a fake SMTP server
and a fake SMS gateway on localhost, and checks the rules below. Nothing is sent to real people.
"""
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="erp_alert_test_"))
shutil.copy(HERE / "monitoring.db", TMP / "test.db")

MAIL, SMS, SMS_FAIL = [], [], set()


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        def say(text):
            self.wfile.write((text + "\r\n").encode()); self.wfile.flush()
        say("220 fake-smtp")
        in_data, lines, rcpt = False, [], []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            text = raw.decode().rstrip("\r\n")
            if in_data:
                if text == ".":
                    in_data = False
                    MAIL.append({"to": rcpt, "text": "\n".join(lines)}); lines, rcpt = [], []
                    say("250 queued")
                else:
                    lines.append(text[1:] if text.startswith("..") else text)
                continue
            up = text.upper()
            if up.startswith(("EHLO", "HELO")): say("250 fake-smtp")
            elif up.startswith("RCPT"): rcpt.append(text[text.index("<") + 1:text.index(">")]); say("250 ok")
            elif up == "DATA": in_data = True; say("354 go ahead")
            elif up == "QUIT": say("221 bye"); return
            else: say("250 ok")


class SMSHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        to = body.get("To", [""])[0]
        if to in SMS_FAIL:
            self.send_response(500); self.end_headers(); self.wfile.write(b'{"message":"gateway down"}'); return
        SMS.append({"to": to, "body": body.get("Body", [""])[0], "path": self.path})
        self.send_response(201); self.end_headers(); self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class QuietTCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


smtp_server = QuietTCP(("127.0.0.1", 0), SMTPHandler)
sms_server = HTTPServer(("127.0.0.1", 0), SMSHandler)
for srv in (smtp_server, sms_server):
    threading.Thread(target=srv.serve_forever, daemon=True).start()

os.environ.update({
    "DB_TYPE": "sqlite", "SQLITE_PATH": str(TMP / "test.db"),
    "ALERT_EMAIL_ENABLED": "true", "ALERT_EMAIL_TO": "you@example.com,sir@example.com",
    "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(smtp_server.server_address[1]), "SMTP_SECURITY": "none",
    "ALERT_SMS_ENABLED": "true", "ALERT_SMS_TO": "+910000000001,+910000000002", "SMS_PROVIDER": "twilio",
    "TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "tok", "TWILIO_FROM": "+15550000000",
    "TWILIO_API_BASE": f"http://127.0.0.1:{sms_server.server_address[1]}",
    "ENV_NAME": "TEST-ERP", "ALERT_COOLDOWN_MINUTES": "60", "ALERT_MAX_INDIVIDUAL": "5",
})
sys.path.insert(0, str(HERE))
import app_local                      # noqa: E402
import alert_channels as ac           # noqa: E402
from db_wrapper import DatabaseWrapper  # noqa: E402

passed = failed = 0


def check(name, condition, extra=""):
    global passed, failed
    if condition:
        passed += 1; print(f"  PASS  {name}")
    else:
        failed += 1; print(f"  FAIL  {name} {extra}")


def cycle():
    """What the background scanner does every 30 s: detect new events, then deliver."""
    app_local.scan_monitoring_events()
    time.sleep(0.2)                       # let the async delivery thread start
    for _ in range(100):                  # ...and wait for it to finish
        if not ac._lock.locked():
            break
        time.sleep(0.05)
    ac.process_pending()                  # (no-op when nothing left; also covers the 'busy' case)


def reset():
    MAIL.clear(); SMS.clear()


def sql(query, params=None, fetch=False):
    db = DatabaseWrapper()
    try:
        return db.fetch_all(query, params) if fetch else db.execute(query, params)
    finally:
        db.close()


NEXT_ID = [90000]


def add_failed_job(when=None, name="Invoice Export"):
    NEXT_ID[0] += 1
    t = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    sql("INSERT INTO background_jobs (job_id, job_name, queue_name, state, progress_percent, start_time, end_time, "
        "error_message, created_time, updated_time) VALUES (:i,:n,'FIN_QUEUE','ERROR',10,:t,:t,'ORA-01555: snapshot too old',:t,:t)",
        {"i": NEXT_ID[0], "n": name, "t": t})
    return NEXT_ID[0]


def add_server_reading(status="CRITICAL", cpu=95):
    cols = set(app_local.get_columns("server_health_history"))
    t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tcol = "recorded_at" if "recorded_at" in cols else "checked_time"
    scol = "health_status" if "health_status" in cols else "status"
    ccol = "cpu_usage" if "cpu_usage" in cols else "cpu_percent"
    sql(f"INSERT INTO server_health_history (server_name, {ccol}, {scol}, {tcol}) VALUES ('IFSAPP03', :c, :s, :t)",
        {"c": cpu, "s": status, "t": t})


print("\n1. First run on an existing database must NOT flood anyone with old history")
cycle()
total_rows = sql("SELECT COUNT(*) AS c FROM dashboard_notifications", fetch=True)[0]["c"]
check("old events are in the bell (%d notifications)" % total_rows, total_rows > 0)
check("no email sent for history", len(MAIL) == 0, f"got {len(MAIL)}")
check("no SMS sent for history", len(SMS) == 0, f"got {len(SMS)}")

print("\n2. One new failed job -> one email + one SMS to EACH recipient")
reset(); job = add_failed_job(); cycle()
check("2 emails (you + sir)", sorted(m["to"][0] for m in MAIL) == ["sir@example.com", "you@example.com"], str([m["to"] for m in MAIL]))
check("2 SMS (both numbers)", sorted(m["to"] for m in SMS) == ["+910000000001", "+910000000002"], str(SMS))
text = MAIL[0]["text"] if MAIL else ""
check("email has title, severity, time, description, details",
      all(x in text for x in (f"#{job}", "CRITICAL", "ORA-01555", "Job Name", "Invoice Export", "Time")), text[:300])
check("SMS has severity + title + description", bool(SMS) and all(x in SMS[0]["body"] for x in ("CRITICAL", f"#{job}", "ORA-01555")), str(SMS[:1]))
check("subject carries environment + severity", bool(MAIL) and "[TEST-ERP] CRITICAL" in MAIL[0]["text"], MAIL[0]["text"][:200] if MAIL else "")
row = sql("SELECT email_status, sms_status FROM dashboard_notifications WHERE title LIKE :t", {"t": f"%#{job}"}, fetch=True)[0]
check("delivery status stored as sent/sent", (row["email_status"], row["sms_status"]) == ("sent", "sent"), str(row))

print("\n3. Same event is never sent twice")
reset(); cycle(); cycle(); cycle()
check("no repeat email/SMS after more scans", len(MAIL) == 0 and len(SMS) == 0, f"{len(MAIL)} mails, {len(SMS)} sms")

print("\n4. A sustained condition (server stays CRITICAL) = ONE alert per cooldown")
reset(); add_server_reading(); add_server_reading(); cycle()
check("two new CRITICAL readings in one run -> 1 email per recipient", len(MAIL) == 2, f"{len(MAIL)}")
reset(); add_server_reading(); cycle()
check("another CRITICAL reading 1 minute later -> suppressed by cooldown", len(MAIL) == 0 and len(SMS) == 0, f"{len(MAIL)} mails")
statuses = [r["email_status"] for r in sql("SELECT email_status FROM dashboard_notifications WHERE event_type='critical_server' "
                                           "ORDER BY notification_id DESC LIMIT 1", fetch=True)]
check("suppression is recorded on the notification", statuses and statuses[0].startswith("skipped: same alert"), str(statuses))

print("\n5. Burst of 8 failures -> ONE digest per recipient, not 8")
reset()
for i in range(8):
    add_failed_job(name=f"Batch job {i}")
cycle()
check("digest email: 2 (one per recipient)", len(MAIL) == 2, f"{len(MAIL)}")
check("digest SMS: 2", len(SMS) == 2, f"{len(SMS)}")
check("digest mentions the count", bool(MAIL) and "8 new alerts" in MAIL[0]["text"], MAIL[0]["text"][:200] if MAIL else "")

print("\n6. Old events and non-critical types are not sent")
reset(); add_failed_job(when=datetime.now() - timedelta(days=3)); cycle()
check("3-day-old failure -> dashboard only", len(MAIL) == 0 and len(SMS) == 0)
reset(); app_local.app.test_client().post("/api/notifications/test"); cycle()
check("test notification from the bell -> dashboard only", len(MAIL) == 0 and len(SMS) == 0)

print("\n7. Gateway failure for ONE number: retried later, other number NOT sent twice")
reset(); SMS_FAIL.add("+910000000002"); add_failed_job(name="Retry job"); cycle()
check("SMS to number 1 delivered, number 2 failed", [m["to"] for m in SMS] == ["+910000000001"], str([m["to"] for m in SMS]))
st = sql("SELECT sms_status, sms_sent_to, delivery_attempts FROM dashboard_notifications ORDER BY notification_id DESC LIMIT 1", fetch=True)[0]
check("status recorded as failed", st["sms_status"].startswith("failed"), str(st))
reset(); cycle()
check("no immediate retry storm (waits ALERT_RETRY_MINUTES)", len(SMS) == 0)
sql("UPDATE dashboard_notifications SET last_attempt_at = :t WHERE sms_status LIKE 'failed%'",
    {"t": (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")})
SMS_FAIL.clear(); cycle()
check("retry sends only to the missing number", [m["to"] for m in SMS] == ["+910000000002"], str([m["to"] for m in SMS]))
st = sql("SELECT sms_status FROM dashboard_notifications ORDER BY notification_id DESC LIMIT 1", fetch=True)[0]
check("status becomes sent", st["sms_status"] == "sent", str(st))
check("email for it was not sent again", len(MAIL) == 0, f"{len(MAIL)}")

print("\n8. Test endpoint + status endpoint")
reset(); client = app_local.app.test_client()
res = client.post("/api/alert-channels/test").get_json()
check("manual test sends to everyone", res["ok"] and res["result"]["email"]["ok"] and res["result"]["sms"]["ok"] and len(MAIL) == 2 and len(SMS) == 2, str(res))
info = client.get("/api/alert-channels").get_json()
check("status shows masked recipients, no secrets",
      info["email"]["recipients"] == ["y***@example.com", "s***@example.com"] and "tok" not in json.dumps(info), str(info["email"]))
lst = client.get("/api/notifications").get_json()
check("bell list carries email/sms status", "email_status" in lst["notifications"][0], str(lst["notifications"][0].keys()))

print("\n9. Misconfiguration is reported, not silent")
reset(); os.environ["SMTP_HOST"] = ""; add_failed_job(name="Config job"); cycle()
st = sql("SELECT email_status, sms_status FROM dashboard_notifications ORDER BY notification_id DESC LIMIT 1", fetch=True)[0]
check("email marked failed: SMTP_HOST is not set", "SMTP_HOST" in (st["email_status"] or ""), str(st))
check("SMS still delivered", st["sms_status"] == "sent", str(st))

print("\n10. Everything off by default")
for k in ("ALERT_EMAIL_ENABLED", "ALERT_SMS_ENABLED"):
    os.environ[k] = "false"
reset(); add_failed_job(name="Disabled job"); cycle()
check("nothing sent when disabled", len(MAIL) == 0 and len(SMS) == 0)

smtp_server.shutdown(); sms_server.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
