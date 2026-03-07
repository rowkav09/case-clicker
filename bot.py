"""
bot.py  -  multi-account case-clicker bot
==========================================
Loads saved sessions from sessions.json (created by login_setup.py)
and runs one thread per account doing pure HTTP POST requests.
No browser, no captcha, no paid service.

Usage:
    python bot.py              # run all accounts
    python bot.py 5            # cap at 5 threads
"""

import json, time, sys, os, threading, datetime
from curl_cffi import requests as cffi_requests

SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")
BASE          = "https://case-clicker.com"

CLICK_BATCH      = 100   # clicks per request
CLICK_DELAY      = 0.4   # seconds between requests
RATE_LIMIT_SLEEP = 60    # wait on 429

HEADERS_BASE = {
    "Origin":       BASE,
    "Referer":      BASE + "/",
    "Content-Type": "application/json",
    "Accept":       "application/json, */*;q=0.9",
}

# ── shared stats ─────────────────────────────────────────────────────────────
stats_lock = threading.Lock()
stats: dict = {}

def update_stat(email, **kwargs):
    with stats_lock:
        if email not in stats:
            stats[email] = {"clicks": 0, "cases": 0, "progress": 0.0,
                            "status": "init", "last_err": ""}
        stats[email].update(kwargs)


# ── status printer ────────────────────────────────────────────────────────────
def printer_thread():
    while True:
        time.sleep(5)
        with stats_lock:
            if not stats:
                continue
            os.system("cls" if os.name == "nt" else "clear")
            now = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"  case-clicker bot  [{now}]")
            print(f"  {'email':<35}  {'clicks':>8}  {'cases':>6}  {'prog%':>6}  status")
            print("  " + "-" * 72)
            for email, s in sorted(stats.items()):
                short = email[:34]
                err   = f"  [{s['last_err']}]" if s["last_err"] else ""
                print(f"  {short:<35}  {s['clicks']:>8,}  {s['cases']:>6}  "
                      f"{s['progress']:>5.1f}%  {s['status']}{err}")


# ── per-account worker ────────────────────────────────────────────────────────
def account_worker(email: str, acc_data: dict):
    update_stat(email, status="starting")
    sess = cffi_requests.Session(impersonate="chrome131")

    for c in acc_data["cookies"]:
        sess.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", "case-clicker.com"),
            path=c.get("path", "/"),
        )

    consecutive_errors = 0

    while True:
        try:
            r = sess.post(
                f"{BASE}/api/caseClick",
                json={"clicks": CLICK_BATCH},
                headers=HEADERS_BASE,
                timeout=15,
            )

            if r.status_code == 200:
                consecutive_errors = 0
                try:
                    data     = r.json()
                    progress = float(data.get("caseProgress", data.get("progress", 0)))
                    with stats_lock:
                        stats[email]["clicks"]  += CLICK_BATCH
                        stats[email]["progress"] = progress
                        stats[email]["status"]   = "clicking"
                        stats[email]["last_err"] = ""
                        if progress >= 99.9 or data.get("case"):
                            stats[email]["cases"] += 1
                except (ValueError, KeyError):
                    pass

            elif r.status_code in (401, 403):
                consecutive_errors += 1
                update_stat(email, status="session expired", last_err=str(r.status_code))
                time.sleep(30)
                if consecutive_errors >= 3:
                    update_stat(email, status="STOPPED - re-run login_setup.py")
                    break

            elif r.status_code == 429:
                update_stat(email, status=f"rate-limited {RATE_LIMIT_SLEEP}s", last_err="429")
                time.sleep(RATE_LIMIT_SLEEP)

            elif r.status_code >= 500:
                consecutive_errors += 1
                update_stat(email, status="server error", last_err=str(r.status_code))
                time.sleep(15)

            else:
                consecutive_errors += 1
                update_stat(email, status=f"unexpected {r.status_code}",
                            last_err=r.text[:60])
                time.sleep(10)

            if consecutive_errors >= 10:
                update_stat(email, status="STOPPED (too many errors)")
                break

            time.sleep(CLICK_DELAY)

        except Exception as exc:
            consecutive_errors += 1
            update_stat(email, status="network error", last_err=str(exc)[:60])
            time.sleep(10)
            if consecutive_errors >= 10:
                update_stat(email, status="STOPPED (network errors)")
                break


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    if not os.path.exists(SESSIONS_FILE):
        print("[!] sessions.json not found.")
        print("    Run  python login_setup.py  first to log in all accounts.")
        sys.exit(1)

    with open(SESSIONS_FILE, encoding="utf-8") as f:
        sessions_data = json.load(f)

    if not sessions_data:
        print("[!] sessions.json is empty.  Run  python login_setup.py  first.")
        sys.exit(1)

    max_threads = int(sys.argv[1]) if len(sys.argv) > 1 else len(sessions_data)
    accounts    = list(sessions_data.items())[:max_threads]

    print(f"[*] Starting bot with {len(accounts)} account(s) ...")

    t = threading.Thread(target=printer_thread, daemon=True)
    t.start()

    threads = []
    for email, acc_data in accounts:
        t = threading.Thread(target=account_worker, args=(email, acc_data), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.1)

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[*] Stopped.")


if __name__ == "__main__":
    main()
