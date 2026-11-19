"""IFS Cloud platform checks: HTTP endpoints, Kubernetes pods (kubectl) and SQL-based checks.
Statuses: ok | warn | crit | unknown."""
import json, os, re, ssl, subprocess, time, urllib.error, urllib.parse, urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

CFG_FILE = os.getenv("PLATFORM_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform_checks.json"))
VERIFY = os.getenv("PLATFORM_VERIFY_TLS", "true").lower() == "true"
USERS_SQL = "SELECT COUNT(DISTINCT client_identifier), COUNT(*) FROM v$session WHERE type='USER' AND username IS NOT NULL"
_HIST = {}

def load_cfg():
    if not os.path.isfile(CFG_FILE):
        return None
    with open(CFG_FILE, encoding="utf-8") as f:
        return json.load(f)

def _r(c, status, detail, value=None):
    return {"id": c["id"], "name": c["name"], "status": status, "detail": detail, "value": value}

def _open(c, req, timeout):
    ctx = ssl.create_default_context() if VERIFY else ssl._create_unverified_context()
    h = [urllib.request.HTTPSHandler(context=ctx)] + ([] if c.get("use_proxy") else [urllib.request.ProxyHandler({})])
    return urllib.request.build_opener(*h).open(req, timeout=timeout)

def _grade(c, ms):
    h = _HIST.setdefault(c["id"], deque(maxlen=30)); h.append(ms)
    last = list(h)[-10:]; avg = int(sum(last) / len(last))
    st = "crit" if ms >= c.get("crit_ms", 10**9) else "warn" if ms >= c.get("warn_ms", 10**9) else "ok"
    return st, avg

def _timed(c, req):
    t = time.perf_counter()
    try:
        r = _open(c, req, c.get("timeout", 10)); code = r.status; body = r.read(65536)
    except urllib.error.HTTPError as e:
        code, body = e.code, b""
    except Exception as e:
        return None, None, None, str(getattr(e, "reason", e))[:200]
    return code, int((time.perf_counter() - t) * 1000), body, None

def check_http(c):
    code, ms, _, err = _timed(c, urllib.request.Request(c["url"], headers={"User-Agent": "TechM-IFS-Monitor"}))
    if err:
        return _r(c, "crit", err)
    ok = code in c["ok_codes"] if c.get("ok_codes") else (code < 400 or code in (401, 403))
    if not ok:
        return _r(c, "crit", f"HTTP {code}", f"{ms} ms")
    st, avg = _grade(c, ms)
    return _r(c, st, f"HTTP {code} | avg last 10: {avg} ms", f"{ms} ms")

def check_login(c):
    """Synthetic login: OIDC password-grant token request if LOGIN_TOKEN_URL is set, else a timed GET of c['url']."""
    url = os.getenv("LOGIN_TOKEN_URL")
    if not url:
        return check_http(c)
    f = {"grant_type": "password", "client_id": os.getenv("LOGIN_CLIENT_ID", ""),
         "username": os.getenv("LOGIN_USER", ""), "password": os.getenv("LOGIN_PASS", "")}
    if os.getenv("LOGIN_CLIENT_SECRET"):
        f["client_secret"] = os.getenv("LOGIN_CLIENT_SECRET")
    code, ms, body, err = _timed(c, urllib.request.Request(url, data=urllib.parse.urlencode(f).encode(), method="POST"))
    if err:
        return _r(c, "crit", err)
    if code != 200 or b"access_token" not in (body or b""):
        return _r(c, "crit", f"Token request failed: HTTP {code}", f"{ms} ms")
    st, avg = _grade(c, ms)
    return _r(c, st, f"Synthetic login OK | avg last 10: {avg} ms", f"{ms} ms")

def fetch_pods(cfg):
    cmd = [cfg.get("kubectl", "kubectl")] + (["--context", cfg["context"]] if cfg.get("context") else []) \
          + ["get", "pods", "-n", cfg["namespace"], "-o", "json"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[:200] or "kubectl failed")
    return json.loads(p.stdout)["items"]

def check_pods(c, items):
    rx = re.compile(c["pattern"], re.I)
    pods = [p for p in items if rx.search(p["metadata"]["name"]) and p["status"].get("phase") != "Succeeded"]
    if not pods:
        return _r(c, "crit", f"No pods match '{c['pattern']}'", "0/0")
    bad, restarts = [], 0
    for p in pods:
        cs = p["status"].get("containerStatuses", [])
        restarts += sum(x.get("restartCount", 0) for x in cs)
        if not (p["status"].get("phase") == "Running" and cs and all(x.get("ready") for x in cs)):
            bad.append(f"{p['metadata']['name']} ({p['status'].get('phase', '?')})")
    up = len(pods) - len(bad)
    st = "ok" if not bad else "crit" if up == 0 else "warn"
    if st == "ok" and restarts >= c.get("warn_restarts", 5):
        st = "warn"
    return _r(c, st, f"restarts: {restarts}" + (" | not ready: " + ", ".join(bad[:3]) if bad else ""), f"{up}/{len(pods)} ready")

def check_users(c, db_one):
    row = db_one(c.get("sql", USERS_SQL)) or (0,)
    n = int(row[0] or 0)
    d = f"{row[1]} DB sessions" if len(row) > 1 else ""
    if n == 0 and "sql" not in c:
        d += " | client_identifier empty: set a custom 'sql' for real user counts"
    return _r(c, "warn" if n >= c.get("warn_above", 10**9) else "ok", d, str(n))

def check_ad(c, db_one):
    if not c.get("sql"):
        return _r(c, "unknown", "Set 'sql' (returns LAST_RUN, RESULT) in platform_checks.json")
    row = db_one(c["sql"]) or (None,)
    last, res = row[0], (str(row[1]) if len(row) > 1 else "")
    if last is None:
        return _r(c, "crit", "No sync run found")
    age = (datetime.now(last.tzinfo) - last).total_seconds() / 3600
    bad = re.search(c.get("error_regex", "err|fail"), res, re.I)
    st = "crit" if bad or age > c.get("max_age_hours", 26) else "ok"
    return _r(c, st, f"last run {last:%Y-%m-%d %H:%M} | {res or 'no result text'}", f"{age:.1f} h ago")

def run_all(db_one):
    cfg = load_cfg()
    if cfg is None:
        return {"configured": False, "checks": []}
    items, perr = None, None
    if any(c["type"] == "pods" for c in cfg["checks"]):
        try:
            items = fetch_pods(cfg)
        except FileNotFoundError:
            perr = "kubectl not found in PATH"
        except Exception as e:
            perr = str(e)[:200]
    def run(c):
        try:
            t = c["type"]
            if t == "http": return check_http(c)
            if t == "login": return check_login(c)
            if t == "pods": return _r(c, "unknown", perr) if perr else check_pods(c, items)
            if t == "users": return check_users(c, db_one)
            if t == "ad_sync": return check_ad(c, db_one)
            return _r(c, "unknown", "unknown type " + t)
        except Exception as e:
            return _r(c, "unknown", str(e).split("\n")[0][:200])
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(run, cfg["checks"]))
    return {"configured": True, "checks": res, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
