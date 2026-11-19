"""IFS Cloud Monitoring Dashboard - read-only Oracle monitoring."""
import os, re, time, threading, logging, hmac
from datetime import datetime, timezone
import oracledb
from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response, send_from_directory

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ifs-monitor")

def env(k, d=None):
    return os.getenv(k, d)

def safe_ident(v):
    if not re.fullmatch(r"[A-Za-z0-9_$.]+", v or ""):
        raise ValueError(f"Invalid identifier: {v!r}")
    return v.upper()

OWNER = safe_ident(env("IFS_OWNER", "IFSAPP"))
APPMSG_T, APPMSG_C = safe_ident(env("APPMSG_TABLE")), safe_ident(env("APPMSG_STATE_COL", "STATE"))
BGJOB_T, BGJOB_C = safe_ident(env("BGJOB_TABLE")), safe_ident(env("BGJOB_STATE_COL", "STATE"))
TTL = int(env("CACHE_TTL_SEC", "20"))
TIMEOUT = int(env("QUERY_TIMEOUT_MS", "15000"))
BLEVEL = int(env("BLEVEL_THRESHOLD", "3"))

import atexit
_pool, _pool_lock = None, threading.Lock()

@atexit.register
def _close_pool():
    global _pool
    try:
        if _pool is not None:
            _pool.close(force=True)
            _pool = None
    except Exception:
        pass
def pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = oracledb.create_pool(
                user=env("DB_USER"), password=env("DB_PASSWORD"), dsn=env("DB_DSN"),
                min=1, max=int(env("POOL_MAX", "4")), increment=1, getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
                wait_timeout=5000, ping_interval=30)
        return _pool

# Table/column names come from validated env values; all user data uses binds.
PANELS = {
    "locks": ("""
        SELECT s.sid, s.serial# AS serial, s.username, s.status, s.osuser, s.machine,
               SUBSTR(s.module,1,40) AS module, s.blocking_session AS blocked_by,
               s.seconds_in_wait AS wait_sec, s.event,
               (SELECT o.object_name FROM v$locked_object lo JOIN dba_objects o ON o.object_id = lo.object_id
                 WHERE lo.session_id = s.sid AND ROWNUM = 1) AS locked_object, s.sql_id
          FROM v$session s
         WHERE s.type = 'USER'
           AND (s.blocking_session IS NOT NULL
                OR s.sid IN (SELECT blocking_session FROM v$session WHERE blocking_session IS NOT NULL))
         ORDER BY s.seconds_in_wait DESC FETCH FIRST 100 ROWS ONLY""", {}),
    "appmsg": (f"SELECT {APPMSG_C} AS state, COUNT(*) AS cnt FROM {APPMSG_T} GROUP BY {APPMSG_C} ORDER BY cnt DESC", {}),
    "bgjobs": (f"SELECT {BGJOB_C} AS state, COUNT(*) AS cnt FROM {BGJOB_T} GROUP BY {BGJOB_C} ORDER BY cnt DESC", {}),
    "invalid": ("""
        SELECT owner, object_type, object_name, status, TO_CHAR(last_ddl_time,'YYYY-MM-DD HH24:MI') AS last_ddl
          FROM dba_objects
         WHERE status = 'INVALID' AND owner = :owner
         ORDER BY object_type, object_name FETCH FIRST 500 ROWS ONLY""", {"owner": OWNER}),
    "blevel": ("""
        SELECT owner, table_name, index_name, blevel, leaf_blocks, num_rows,
               TO_CHAR(last_analyzed,'YYYY-MM-DD') AS last_analyzed
          FROM dba_indexes
         WHERE owner = :owner AND blevel >= :thr
         ORDER BY blevel DESC, leaf_blocks DESC FETCH FIRST 500 ROWS ONLY""", {"owner": OWNER, "thr": BLEVEL}),
}

_cache, _cache_lock = {}, threading.Lock()
def run_panel(name):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and now - hit["t"] < TTL:
            return hit["d"]
    sql, binds = PANELS[name]
    try:
        with pool().acquire() as conn:
            conn.call_timeout = TIMEOUT
            with conn.cursor() as cur:
                cur.execute(sql, binds)
                cols = [c[0].lower() for c in cur.description]
                rows = [[(v.isoformat() if hasattr(v, "isoformat") else v) for v in r] for r in cur.fetchall()]
        data = {"ok": True, "columns": cols, "rows": rows}
    except Exception as e:  # keep one failing panel from breaking the dashboard
        log.exception("panel %s failed", name)
        data = {"ok": False, "error": str(e).split("\n")[0][:300], "columns": [], "rows": []}
    data["ts"] = datetime.now(timezone.utc).isoformat()
    with _cache_lock:
        _cache[name] = {"t": now, "d": data}
    return data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)

def find_index():
    for d in (os.path.join(BASE_DIR, "static"), BASE_DIR):
        if os.path.isfile(os.path.join(d, "index.html")):
            return d
    return None

@app.before_request
def auth():
    user = env("DASH_USER")
    if not user or request.path == "/health":
        return
    a = request.authorization
    if not (a and hmac.compare_digest(a.username, user) and hmac.compare_digest(a.password or "", env("DASH_PASS", ""))):
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="IFS Monitor"'})

@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Cache-Control"] = "no-store"
    return r

@app.get("/")
def index():
    d = find_index()
    if not d:
        return Response("index.html not found. Put it in %s" % os.path.join(BASE_DIR, "static"), 500)
    return send_from_directory(d, "index.html")

@app.get("/<name>.png")
def logo(name):
    d = find_index()
    f = name + ".png"
    if name not in ("logo", "ifs-logo") or not d or not os.path.isfile(os.path.join(d, f)):
        return Response("", 404)
    return send_from_directory(d, f)

DETAIL = {"appmsg": (APPMSG_T, APPMSG_C), "bgjobs": (BGJOB_T, BGJOB_C)}
DETAIL_LIMIT = int(env("DETAIL_LIMIT", "200"))
MAXCELL = 4000

def _lob_handler(cursor, metadata):
    # Fetch CLOBs as text (error messages, payloads) and BLOBs as bytes (shown as size only)
    if metadata.type_code is oracledb.DB_TYPE_CLOB:
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if metadata.type_code is oracledb.DB_TYPE_BLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)

def _cell(v):
    if isinstance(v, (bytes, bytearray)):
        return "<binary %d bytes>" % len(v)
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ")
    if isinstance(v, str) and len(v) > MAXCELL:
        return v[:MAXCELL] + " ...[truncated]"
    return v

@app.get("/api/detail/<name>")
def api_detail(name):
    if name not in DETAIL:
        return jsonify(error="unknown panel"), 404
    t, c = DETAIL[name]
    if request.args.get("isnull") == "1":
        where, binds = f"{c} IS NULL", {}
    else:
        where, binds = f"{c} = :s", {"s": request.args.get("state", "")}
    sql = f"SELECT * FROM {t} WHERE {where} FETCH FIRST {DETAIL_LIMIT} ROWS ONLY"
    try:
        with pool().acquire() as conn:
            conn.call_timeout = TIMEOUT
            with conn.cursor() as cur:
                cur.outputtypehandler = _lob_handler
                cur.execute(sql, binds)
                cols = [d[0].lower() for d in cur.description]
                rows = [[_cell(v) for v in r] for r in cur.fetchall()]
        return jsonify(ok=True, columns=cols, rows=rows, limit=DETAIL_LIMIT, truncated=len(rows) >= DETAIL_LIMIT)
    except Exception as e:
        log.exception("detail %s failed", name)
        return jsonify(ok=False, error=str(e).split("\n")[0][:300], columns=[], rows=[])

@app.get("/api/<name>")
def api(name):
    if name not in PANELS:
        return jsonify(error="unknown panel"), 404
    return jsonify(run_panel(name))

@app.get("/health")
def health():
    try:
        with pool().acquire() as c:
            c.ping()
        return jsonify(status="ok")
    except Exception as e:
        return jsonify(status="down", error=str(e)[:200]), 503

# ------------------------------------------------------- Platform status ----
import ifs_platform
PLAT_TTL = int(env("PLATFORM_TTL_SEC", "30"))
_plat, _plat_lock = {"t": 0, "d": None}, threading.Lock()

def db_one(sql):
    with pool().acquire() as conn:
        conn.call_timeout = TIMEOUT
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()

def run_platform():
    with _plat_lock:
        if _plat["d"] is None or time.time() - _plat["t"] >= PLAT_TTL:
            try:
                _plat["d"] = ifs_platform.run_all(db_one)
            except Exception as e:
                log.exception("platform checks failed")
                _plat["d"] = {"configured": True, "checks": [], "error": str(e)[:200]}
            _plat["t"] = time.time()
        return _plat["d"]

_pods = {"t": 0, "d": None}
def get_pods():
    with _plat_lock:
        if _pods["d"] is None or time.time() - _pods["t"] >= 15:
            cfg = ifs_platform.load_cfg()
            if not cfg or not cfg.get("namespace"):
                _pods["d"] = {"configured": False}
            else:
                try:
                    _pods["d"] = {"configured": True, "ok": True, "namespace": cfg["namespace"], "rows": ifs_platform.pods_list(cfg)}
                except FileNotFoundError:
                    _pods["d"] = {"configured": True, "ok": False, "error": "kubectl not found in PATH"}
                except Exception as e:
                    _pods["d"] = {"configured": True, "ok": False, "error": str(e)[:300]}
            _pods["t"] = time.time()
        return _pods["d"]

@app.get("/api/pods")
def api_pods():
    return jsonify(get_pods())

@app.get("/api/pods/<name>")
def api_pod_detail(name):
    cfg = ifs_platform.load_cfg()
    if not cfg:
        return jsonify(error="platform_checks.json not found"), 404
    try:
        return jsonify(ifs_platform.pod_detail(cfg, name))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=str(e)[:300])

@app.get("/api/platform")
def api_platform():
    return jsonify(run_platform())

# ------------------------------------------------- DB server CPU / memory ----
from collections import deque
H_INTERVAL = int(env("HOST_SAMPLE_SEC", "60"))
THR = dict(cpu_warn=float(env("CPU_WARN_PCT", "80")), cpu_crit=float(env("CPU_CRIT_PCT", "90")),
           mem_warn=float(env("MEM_WARN_PCT", "85")), mem_crit=float(env("MEM_CRIT_PCT", "95")))
_host_hist = deque(maxlen=int(env("HOST_HISTORY_POINTS", "240")))
_host_lock, _host_err, _prev_os = threading.Lock(), "", {}
SQL_SYSM = ("SELECT metric_name, value FROM v$sysmetric WHERE metric_name IN "
            "('Host CPU Utilization (%)','CPU Usage Per Sec') ORDER BY intsize_csec")
SQL_OS = ("SELECT stat_name, value FROM v$osstat WHERE stat_name IN "
          "('NUM_CPUS','LOAD','PHYSICAL_MEMORY_BYTES','FREE_MEMORY_BYTES','BUSY_TIME','IDLE_TIME')")
SQL_MEM = ("SELECT (SELECT NVL(SUM(value),0) FROM v$sga), "
           "(SELECT NVL(MAX(value),0) FROM v$pgastat WHERE name = 'total PGA allocated') FROM dual")

def sample_host():
    """One CPU/memory sample of the DB server, read through Oracle (v$sysmetric, v$osstat, v$sga, v$pgastat)."""
    with _host_lock:
        with pool().acquire() as conn:
            conn.call_timeout = TIMEOUT
            with conn.cursor() as cur:
                cur.execute(SQL_SYSM); m = {k: float(v) for k, v in cur.fetchall() if v is not None}  # longest interval wins
                cur.execute(SQL_OS); o = {k: float(v) for k, v in cur.fetchall() if v is not None}
                cur.execute(SQL_MEM); sga, pga = (float(x or 0) for x in cur.fetchone())
        gb = lambda b: round(b / 1024 ** 3, 2)
        ncpu, phys, free = o.get("NUM_CPUS"), o.get("PHYSICAL_MEMORY_BYTES"), o.get("FREE_MEMORY_BYTES")
        cpu = m.get("Host CPU Utilization (%)")
        if cpu is None and "BUSY_TIME" in o and "IDLE_TIME" in o:       # fallback: delta of cumulative counters
            cur_v, prev = (o["BUSY_TIME"], o["IDLE_TIME"]), _prev_os.get("v"); _prev_os["v"] = cur_v
            if prev and (cur_v[0] - prev[0]) + (cur_v[1] - prev[1]) > 0:
                cpu = 100 * (cur_v[0] - prev[0]) / ((cur_v[0] - prev[0]) + (cur_v[1] - prev[1]))
        ora_cpu = m["CPU Usage Per Sec"] / ncpu if ncpu and "CPU Usage Per Sec" in m else None  # centisec/sec / cpus
        mem_pct = (phys - free) / phys * 100 if phys and free is not None else None
        smp = dict(ts=time.time(), time=datetime.now().strftime("%H:%M"),
                   host_cpu=None if cpu is None else round(cpu, 1), ora_cpu=None if ora_cpu is None else round(ora_cpu, 1),
                   ncpu=int(ncpu) if ncpu else None, load=None if "LOAD" not in o else round(o["LOAD"], 2),
                   phys_gb=gb(phys) if phys else None, mem_pct=None if mem_pct is None else round(mem_pct, 1),
                   mem_used_gb=gb(phys - free) if phys and free is not None else None,
                   ora_mem_gb=gb(sga + pga), ora_pct=round((sga + pga) / phys * 100, 1) if phys else None)
        _host_hist.append(smp)
        return smp

def host_loop():
    global _host_err
    while True:
        try:
            sample_host(); _host_err = ""
        except Exception as e:
            _host_err = str(e).split("\n")[0][:300]; log.error("host sample failed: %s", _host_err)
        time.sleep(H_INTERVAL)

@app.get("/api/host")
def api_host():
    if not _host_hist:
        try:
            sample_host()
        except Exception as e:
            return jsonify(ok=False, error=str(e).split("\n")[0][:300])
    return jsonify(ok=True, latest=_host_hist[-1], thr=THR, err=_host_err,
                   history=[dict(t=x["time"], cpu=x["host_cpu"], mem=x["mem_pct"]) for x in _host_hist])

# ---------------------------------------------------------------- Alerts ----
import smtplib, json, urllib.request
from collections import deque
from email.message import EmailMessage

def _i(k, d): return int(env(k, str(d)))
A_ON = env("ALERTS_ENABLED", "true").lower() == "true"
A_INTERVAL, A_COOLDOWN, A_CONFIRM = _i("ALERT_INTERVAL_SEC", 60), _i("ALERT_COOLDOWN_SEC", 1800), _i("ALERT_CONFIRM", 2)
A_LOCK, A_APPMSG, A_BGJOB = _i("LOCK_WAIT_SEC", 300), _i("ALERT_APPMSG_ERR", 0), _i("ALERT_BGJOB_ERR", 0)
A_INVALID, A_BLEVEL = _i("ALERT_INVALID_MAX", 0), _i("ALERT_BLEVEL_MAX", -1)  # -1 disables
ERR_RE = re.compile(env("ERROR_STATE_REGEX", "err|fail"), re.I)
ENV_NAME = env("ENV_NAME", "IFSCOESALES")

_alock = threading.Lock()
_active, _pending, _history = {}, {}, deque(maxlen=200)
_iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _err_rows(d):
    ci = d["columns"].index("cnt")
    return [(r[0], r[ci]) for r in d["rows"] if ERR_RE.search(str(r[0] or ""))]

def check_rules():
    """Return {key: (severity, title, detail)} for every condition currently breached."""
    out = {}
    pd = get_pods()
    if pd.get("ok"):
        crit = [r for r in pd["rows"] if r["state"] == "crit"]; warn = [r for r in pd["rows"] if r["state"] == "warn"]
        if crit:
            out["pods_crit"] = ("critical", f"{len(crit)} pod(s) not ready in {pd['namespace']}", ", ".join(f"{r['name']} ({r['status']})" for r in crit[:6]))
        if warn:
            out["pods_warn"] = ("warning", f"{len(warn)} pod(s) degraded or recently restarted in {pd['namespace']}",
                                ", ".join(f"{r['name']} ({r['status']}, restarted {r['last_restart']})" for r in warn[:6]))
    if _host_hist:
        h = _host_hist[-1]
        for key, lab, v, w, c in (("host_cpu", "DB server CPU", h["host_cpu"], THR["cpu_warn"], THR["cpu_crit"]),
                                  ("host_mem", "DB server memory", h["mem_pct"], THR["mem_warn"], THR["mem_crit"])):
            if v is not None and v >= w:
                out[key] = ("critical" if v >= c else "warning", f"{lab} at {v}%", f"Thresholds: warning {w}%, critical {c}%")
    for r in run_platform().get("checks", []):
        if r.get("type") == "pods":
            continue
        if r["status"] in ("crit", "warn"):
            out["plat_" + r["id"]] = ("critical" if r["status"] == "crit" else "warning",
                                      f"{r['name']}: {'DOWN / CRITICAL' if r['status'] == 'crit' else 'WARNING'} ({r['value'] or '-'})", r["detail"])
    try:
        with pool().acquire() as c:
            c.ping()
    except Exception as e:
        out["db_down"] = ("critical", "Database unreachable", str(e).split("\n")[0][:300])
        return out
    d = run_panel("locks")
    if d["ok"]:
        cw, cb, cs, cu = (d["columns"].index(x) for x in ("wait_sec", "blocked_by", "sid", "username"))
        bl = sorted([r for r in d["rows"] if r[cb] is not None and (r[cw] or 0) >= A_LOCK], key=lambda r: -(r[cw] or 0))
        if bl:
            ex = "; ".join(f"SID {r[cs]} ({r[cu]}) blocked by {r[cb]} for {r[cw]}s" for r in bl[:5])
            out["locks"] = ("critical", f"{len(bl)} session(s) blocked for >= {A_LOCK}s", ex)
    for key, thr, label in (("appmsg", A_APPMSG, "Application Message"), ("bgjobs", A_BGJOB, "Background Job")):
        d = run_panel(key)
        if d["ok"]:
            errs = _err_rows(d); n = sum(c for _, c in errs)
            if n > thr:
                out[key] = ("warning", f"{n} {label}(s) in error state", ", ".join(f"{s}: {c}" for s, c in errs))
    d = run_panel("invalid")
    if d["ok"] and len(d["rows"]) > A_INVALID:
        out["invalid"] = ("warning", f"{len(d['rows'])} invalid object(s) in {OWNER}",
                          "; ".join(f"{r[1]} {r[2]}" for r in d["rows"][:5]) + (" ..." if len(d["rows"]) > 5 else ""))
    if A_BLEVEL >= 0:
        d = run_panel("blevel")
        if d["ok"] and len(d["rows"]) > A_BLEVEL:
            out["blevel"] = ("warning", f"{len(d['rows'])} index(es) with BLEVEL >= {BLEVEL}",
                             "; ".join(f"{r[2]} (blevel {r[3]})" for r in d["rows"][:5]))
    return out

def notify(subject, body):
    """Send via configured channels. Returns {channel: 'ok' | error}."""
    res = {}
    if env("SMTP_HOST") and env("MAIL_TO"):
        try:
            m = EmailMessage(); m["Subject"], m["From"] = subject, env("MAIL_FROM", env("SMTP_USER", "ifs-monitor@localhost"))
            m["To"] = env("MAIL_TO"); m.set_content(body)
            with smtplib.SMTP(env("SMTP_HOST"), _i("SMTP_PORT", 25), timeout=15) as s:
                if env("SMTP_TLS", "false").lower() == "true":
                    s.starttls()
                if env("SMTP_USER"):
                    s.login(env("SMTP_USER"), env("SMTP_PASS", ""))
                s.send_message(m)
            res["email"] = "ok"
        except Exception as e:
            log.error("email failed: %s", e); res["email"] = str(e)[:200]
    if env("TEAMS_WEBHOOK_URL"):
        try:
            req = urllib.request.Request(env("TEAMS_WEBHOOK_URL"), data=json.dumps({"text": f"**{subject}**\n\n{body}"}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            res["teams"] = "ok"
        except Exception as e:
            log.error("teams failed: %s", e); res["teams"] = str(e)[:200]
    return res

def evaluate():
    now, firing, events = time.time(), check_rules(), []
    with _alock:
        for k, (sev, title, detail) in firing.items():
            _pending[k] = _pending.get(k, 0) + 1
            a = _active.get(k)
            if a is None:
                if _pending[k] >= A_CONFIRM:
                    _active[k] = dict(sev=sev, title=title, detail=detail, since=now, last_sent=now)
                    events.append(("FIRING", sev, title, detail))
            else:
                a.update(sev=sev, title=title, detail=detail)
                if now - a["last_sent"] >= A_COOLDOWN:
                    a["last_sent"] = now; events.append(("REMINDER", sev, title, detail))
        for k in [k for k in _pending if k not in firing]:
            _pending.pop(k)
        for k in [k for k in _active if k not in firing]:
            a = _active.pop(k)
            events.append(("RESOLVED", a["sev"], a["title"], f"Cleared after {int((now - a['since']) / 60)} min"))
    for ev, sev, title, detail in events:
        _history.appendleft(dict(time=_iso(now), event=ev, severity=sev, title=title, detail=detail))
        log.warning("ALERT %s [%s] %s - %s", ev, sev, title, detail)
        notify(f"[{ENV_NAME}] {ev}: {title}", f"Environment: {ENV_NAME}\nSeverity: {sev}\nTime: {_iso(now)}\n\n{title}\n{detail}")

def alert_loop():
    time.sleep(10)
    while True:
        try:
            evaluate()
        except Exception:
            log.exception("alert evaluation failed")
        time.sleep(A_INTERVAL)

@app.get("/api/alerts")
def api_alerts():
    with _alock:
        act = [dict(severity=a["sev"], title=a["title"], detail=a["detail"], since=_iso(a["since"])) for a in _active.values()]
    ch = [c for c, ok in (("email", env("SMTP_HOST") and env("MAIL_TO")), ("teams", env("TEAMS_WEBHOOK_URL"))) if ok]
    return jsonify(enabled=A_ON, channels=ch, active=act, history=list(_history)[:50])

@app.post("/api/alerts/test")
def api_alert_test():
    r = notify(f"[{ENV_NAME}] TEST alert", "This is a test notification from TechM IFS Monitoring Tool.")
    return jsonify(result=r or "no notification channel configured")


if __name__ == "__main__":
    from waitress import serve
    host, port = env("HOST", "0.0.0.0"), int(env("PORT", "8080"))
    threading.Thread(target=host_loop, daemon=True, name="host").start()
    if A_ON:
        threading.Thread(target=alert_loop, daemon=True, name="alerts").start()
        log.info("Alerting on (every %ss)", A_INTERVAL)
    log.info("Serving on http://%s:%s", host, port)
    serve(app, host=host, port=port, threads=8)
