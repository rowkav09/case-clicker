"""
app.py  —  Case Clicker Bot  |  Web Dashboard
==============================================
Replaces bot.py's terminal output with a live browser dashboard.

Run:
    python app.py
Then open:
    http://localhost:5000
"""

import json, time, os, sys, threading, datetime
from collections import deque

try:
    from flask import Flask, render_template, jsonify, request
except ImportError:
    sys.exit("[!] Flask not installed.  Run:  pip install flask")

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    sys.exit("[!] curl_cffi not installed.  Run:  pip install curl_cffi")

# ── Config ────────────────────────────────────────────────────────────────────
BASE             = "https://case-clicker.com"
SESSIONS_FILE    = os.path.join(os.path.dirname(__file__), "sessions.json")
CLICK_BATCH      = 100      # clicks sent per request
CLICK_DELAY      = 0.4      # seconds between requests
RATE_LIMIT_SLEEP = 60       # sleep on HTTP 429
ME_INTERVAL      = 30       # seconds between /api/me fetches

HEADERS = {
    "Origin":       BASE,
    "Referer":      BASE + "/",
    "Content-Type": "application/json",
    "Accept":       "application/json, */*;q=0.9",
}

# ── Shared state ──────────────────────────────────────────────────────────────
stats_lock  = threading.Lock()
stats: dict = {}          # email  → stat dict

stop_flags: dict = {}     # email  → threading.Event
threads:    dict = {}     # email  → Thread

log_lock = threading.Lock()
event_log: deque = deque(maxlen=150)   # most-recent first

app = Flask(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_sessions() -> dict:
    if not os.path.exists(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def default_stat(email: str) -> dict:
    return {
        "email":       email,
        "username":    email.split("@")[0],
        "status":      "stopped",
        "running":     False,
        "clicks":      0,
        "cases_won":   0,
        "progress":    0.0,
        "balance":     None,
        "level":       None,
        "cps":         0.0,
        "last_case":   None,
        "last_case_name": None,
        "last_err":    "",
        "start_time":  None,
        "uptime_secs": 0,
        "total_cases_opened": None,
        "xp":          None,
    }


def log(email: str, msg: str, level: str = "info"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with log_lock:
        event_log.appendleft({"ts": ts, "email": email, "msg": msg, "level": level})


def make_session(acc_data: dict) -> cffi_requests.Session:
    sess = cffi_requests.Session(impersonate="chrome131")
    for c in acc_data.get("cookies", []):
        sess.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", "case-clicker.com"),
            path=c.get("path", "/"),
        )
    return sess


def fetch_me(sess: cffi_requests.Session, email: str):
    """Pull /api/me and refresh balance / level / username in stats."""
    try:
        r = sess.get(f"{BASE}/api/me", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            d = r.json()
            balance = (d.get("balance") or d.get("coins")
                       or d.get("money") or d.get("cash") or 0)
            level   = d.get("level") or d.get("rank") or 0
            xp      = d.get("xp") or d.get("experience") or 0
            username = (d.get("username") or d.get("name")
                        or d.get("displayName") or email.split("@")[0])
            total_cases = (d.get("totalCasesOpened") or d.get("casesOpened")
                           or d.get("totalCases") or 0)
            with stats_lock:
                stats[email].update({
                    "balance":     balance,
                    "level":       level,
                    "xp":          xp,
                    "username":    username,
                    "total_cases_opened": total_cases,
                })
    except Exception:
        pass   # non-fatal

# ── Worker ────────────────────────────────────────────────────────────────────

def account_worker(email: str, acc_data: dict, stop_event: threading.Event):
    with stats_lock:
        stats[email].update({
            "status":    "starting",
            "running":   True,
            "start_time": time.time(),
            "last_err":  "",
        })

    log(email, "Started clicking")
    sess = make_session(acc_data)
    fetch_me(sess, email)

    last_me   = time.time()
    last_tick = time.time()
    consecutive_errors = 0

    while not stop_event.is_set():
        try:
            r = sess.post(
                f"{BASE}/api/caseClick",
                json={"clicks": CLICK_BATCH},
                headers=HEADERS,
                timeout=15,
            )

            now     = time.time()
            elapsed = now - last_tick
            cps     = CLICK_BATCH / elapsed if elapsed > 0 else 0
            last_tick = now

            # ── 200 OK ────────────────────────────────────────────────────────
            if r.status_code == 200:
                consecutive_errors = 0
                try:
                    data     = r.json()
                    progress = float(data.get("caseProgress", data.get("progress", 0)))
                    balance  = (data.get("balance") or data.get("coins")
                                or data.get("money"))
                    with stats_lock:
                        stats[email]["clicks"]  += CLICK_BATCH
                        stats[email]["progress"] = progress
                        stats[email]["status"]   = "clicking"
                        stats[email]["last_err"] = ""
                        stats[email]["cps"]      = round(cps, 1)
                        if balance is not None:
                            stats[email]["balance"] = balance
                        if progress >= 99.9 or data.get("case"):
                            stats[email]["cases_won"] += 1
                            stats[email]["last_case"]  = datetime.datetime.now().strftime("%H:%M:%S")
                            case_info = data.get("case") or {}
                            case_name = case_info.get("name", "")
                            stats[email]["last_case_name"] = case_name or None
                            log(email,
                                f"Case won!{' — ' + case_name if case_name else ''}",
                                "success")
                except Exception:
                    pass

            # ── 401 / 403 — session dead ──────────────────────────────────────
            elif r.status_code in (401, 403):
                consecutive_errors += 1
                with stats_lock:
                    stats[email].update({
                        "status":   "session expired",
                        "last_err": f"HTTP {r.status_code}",
                    })
                log(email, f"Session expired (HTTP {r.status_code})", "error")
                stop_event.wait(30)
                if consecutive_errors >= 3:
                    with stats_lock:
                        stats[email].update({
                            "status":  "re-login needed",
                            "running": False,
                        })
                    log(email, "Stopped — re-login needed", "error")
                    break

            # ── 429 — rate limited ────────────────────────────────────────────
            elif r.status_code == 429:
                with stats_lock:
                    stats[email].update({
                        "status":   "rate-limited",
                        "last_err": "429",
                    })
                log(email, f"Rate-limited — pausing {RATE_LIMIT_SLEEP}s", "warn")
                stop_event.wait(RATE_LIMIT_SLEEP)

            # ── 5xx — server error ────────────────────────────────────────────
            elif r.status_code >= 500:
                consecutive_errors += 1
                with stats_lock:
                    stats[email].update({
                        "status":   "server error",
                        "last_err": str(r.status_code),
                    })
                stop_event.wait(15)

            # ── other ─────────────────────────────────────────────────────────
            else:
                consecutive_errors += 1
                snippet = r.text[:80] if r.text else str(r.status_code)
                with stats_lock:
                    stats[email].update({
                        "status":   f"error {r.status_code}",
                        "last_err": snippet,
                    })
                stop_event.wait(10)

            if consecutive_errors >= 10:
                with stats_lock:
                    stats[email].update({"status": "stopped (errors)", "running": False})
                log(email, "Stopped after too many errors", "error")
                break

            # Periodic /api/me refresh
            if time.time() - last_me > ME_INTERVAL:
                fetch_me(sess, email)
                last_me = time.time()

            stop_event.wait(CLICK_DELAY)

        except Exception as exc:
            consecutive_errors += 1
            with stats_lock:
                stats[email].update({
                    "status":   "network error",
                    "last_err": str(exc)[:80],
                })
            stop_event.wait(10)
            if consecutive_errors >= 10:
                with stats_lock:
                    stats[email].update({"status": "stopped (network)", "running": False})
                log(email, "Stopped after network errors", "error")
                break

    # Mark stopped
    with stats_lock:
        final = stats[email]["status"]
        if final not in ("re-login needed", "stopped (errors)", "stopped (network)"):
            stats[email]["status"] = "stopped"
        stats[email]["running"]    = False
        stats[email]["start_time"] = None
        stats[email]["cps"]        = 0.0

    log(email, f"Stopped  [{stats[email]['status']}]")


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    sessions_data = load_sessions()
    # Ensure every known account is in the stats dict
    for email in sessions_data:
        with stats_lock:
            if email not in stats:
                stats[email] = default_stat(email)

    with stats_lock:
        rows = []
        for email, s in stats.items():
            row = dict(s)
            row["uptime_secs"] = (
                int(time.time() - s["start_time"]) if s.get("start_time") else 0
            )
            rows.append(row)
    return jsonify(rows)


@app.route("/api/log")
def api_log():
    with log_lock:
        return jsonify(list(event_log))


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    email = data.get("email")

    sessions_data = load_sessions()
    if not sessions_data:
        return jsonify({"error": "No sessions — run login_setup.py first."}), 400

    targets = ([email] if email and email in sessions_data
               else list(sessions_data.keys()))

    started = []
    for em in targets:
        # Don't double-start
        if em in threads and threads[em].is_alive():
            continue
        with stats_lock:
            if em not in stats:
                stats[em] = default_stat(em)
        stop_flags[em] = threading.Event()
        t = threading.Thread(
            target=account_worker,
            args=(em, sessions_data[em], stop_flags[em]),
            daemon=True,
        )
        threads[em] = t
        t.start()
        started.append(em)
        time.sleep(0.05)   # slight stagger

    return jsonify({"ok": True, "started": started})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json(silent=True) or {}
    email = data.get("email")

    targets = ([email] if email else list(stop_flags.keys()))
    for em in targets:
        if em in stop_flags:
            stop_flags[em].set()

    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    sessions_data = load_sessions()
    return jsonify({
        "has_sessions":  bool(sessions_data),
        "account_count": len(sessions_data),
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 52)
    print("  Case Clicker Bot  |  Web Dashboard")
    print("  http://localhost:5000")
    print("=" * 52)

    sessions_data = load_sessions()
    if not sessions_data:
        print("[!] No sessions.json found.")
        print("    Run  python login_setup.py  first, then restart this.")
    else:
        for em in sessions_data:
            stats[em] = default_stat(em)
        print(f"[*] Loaded {len(sessions_data)} account(s) from sessions.json")
        print("[*] Open http://localhost:5000 in your browser to start farming\n")

    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
