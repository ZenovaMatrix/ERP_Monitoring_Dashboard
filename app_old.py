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

_pool, _pool_lock = None, threading.Lock()
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

if __name__ == "__main__":
    from waitress import serve
    host, port = env("HOST", "0.0.0.0"), int(env("PORT", "8080"))
    log.info("Serving on http://%s:%s", host, port)
    serve(app, host=host, port=port, threads=8)
