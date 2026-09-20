#!/usr/bin/env python
"""
main.py  —  Case Clicker Bot  (all-in-one)
==========================================
Single entry point for login setup and the web dashboard.
Install dependencies before running it.

Install and run:
    python -m pip install -r requirements.txt
    python -m patchright install chromium
    python main.py

Then open:
    http://localhost:5000
"""

import json, time, threading, datetime, concurrent.futures, math, re, queue, os, sys
from collections import deque

from flask import Flask, render_template_string, jsonify, request
from curl_cffi import requests as cffi_requests
from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout
import websocket

# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════
BASE             = "https://case-clicker.com"
DIR              = os.path.dirname(os.path.abspath(__file__))
SESSIONS_FILE    = os.path.join(DIR, "sessions.json")
ACCOUNTS_FILE    = os.path.join(DIR, "accounts.json")

CLICK_BATCH      = 3    # fallback; overridden per-account using casesPerClick
CLICK_DELAY      = 0.36  # Socket.IO click interval (360ms like the browser script)
RATE_LIMIT_SLEEP = 90   # initial back-off on 429
ME_INTERVAL      = 30
VAULT_EVERY_N_CLICKS = 100   # collect vault every N case-clicks
SKILLMAP_API_DELAY   = 0.25  # delay between skill buy/sell requests

HEADERS = {
    "Origin":       BASE,
    "Referer":      BASE + "/",
    "Content-Type": "application/json",
    "Accept":       "application/json, */*;q=0.9",
}

# ══════════════════════════════════════════════════════════════════════════════
# Shared state
# ══════════════════════════════════════════════════════════════════════════════
stats_lock  = threading.Lock()
stats: dict = {}

stop_flags: dict = {}
threads:    dict = {}

log_lock  = threading.Lock()
event_log: deque = deque(maxlen=200)

setup_lock   = threading.Lock()
setup_status = {"running": False, "done": 0, "total": 0, "log": []}

# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_sessions() -> dict:
    if not os.path.exists(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sessions(data: dict):
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_accounts() -> list:
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def default_stat(email: str) -> dict:
    return {
        "email":          email,
        "username":       email.split("@")[0],
        "status":         "stopped",
        "running":        False,
        "clicks":         0,
        "cases_won":      0,
        "progress":       0.0,
        "balance":        None,
        "level":          None,
        "cps":            0.0,
        "last_case":      None,
        "last_case_name": None,
        "last_err":       "",
        "start_time":     None,
        "uptime_secs":    0,
        "total_cases_opened": None,
        "xp":             None,
        "click_batch":    CLICK_BATCH,
    }


def log(email: str, msg: str, level: str = "info"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with log_lock:
        event_log.appendleft({"ts": ts, "email": email, "msg": msg, "level": level})


def make_session(acc_data: dict) -> cffi_requests.Session:
    sess = cffi_requests.Session(impersonate="chrome131")
    # Inject all cookies as simple name→value pairs (most compatible across curl_cffi versions)
    cookie_dict = {c["name"]: c["value"] for c in acc_data.get("cookies", [])}
    sess.cookies.update(cookie_dict)
    return sess


def fetch_me(sess: cffi_requests.Session, email: str):
    try:
        r = sess.get(f"{BASE}/api/me", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            d = r.json()
            balance  = d.get("money") or d.get("balance") or d.get("coins") or 0
            rank_obj = d.get("rank") or {}
            level    = rank_obj.get("name") or rank_obj.get("id") or d.get("level") or 0
            xp       = d.get("xp") or d.get("experience") or 0
            username = (d.get("username") or d.get("name") or
                        d.get("displayName") or email.split("@")[0])
            total_cases = (d.get("openedCases") or d.get("totalCasesOpened") or
                           d.get("casesOpened") or 0)
            cases_per_click = float(d.get("casesPerClick") or 0)
            # clicks needed to open exactly 1 case
            batch = max(1, math.ceil(100.0 / cases_per_click)) if cases_per_click > 0 else CLICK_BATCH
            with stats_lock:
                if email in stats:
                    stats[email].update({
                        "balance":            balance,
                        "level":              level,
                        "xp":                 xp,
                        "username":           username,
                        "total_cases_opened": total_cases,
                        "click_batch":        batch,
                    })
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# Login setup (Playwright)
# ══════════════════════════════════════════════════════════════════════════════

def run_login_setup(accounts: list) -> tuple[dict, list]:
    """
    Logs into every account 2 at a time, each in its own browser window.
    Windows sit side by side: slot 0 on the left, slot 1 on the right.
    """
    saved      = {}
    failed     = []
    saved_lock = threading.Lock()

    with setup_lock:
        setup_status["running"] = True
        setup_status["done"]    = 0
        setup_status["total"]   = len(accounts)
        setup_status["log"]     = []

    def slog(msg: str):
        setup_status["log"].append(msg)
        print(msg, flush=True)

    # Window positions for up to 6 simultaneous browsers (3 columns × 2 rows, 640×500 each)
    SLOTS = [
        (0,   0), (640,   0), (1280,   0),
        (0, 500), (640, 500), (1280, 500),
    ]

    def login_one(acc: dict, index: int, slot: int):
        email    = acc["email"]
        password = acc["password"]
        sx, sy   = SLOTS[slot]
        slog(f"[{index+1}/{len(accounts)}] Logging in {email} …")

        from patchright.sync_api import sync_playwright as _sp, TimeoutError as _TO
        with _sp() as pw:
            browser = pw.chromium.launch(
                headless=False,
                args=[f"--window-size=640,500", f"--window-position={sx},{sy}"],
            )
            context = browser.new_context(viewport={"width": 640, "height": 500})
            page    = context.new_page()
            try:
                page.goto(f"{BASE}/auth/login", wait_until="load", timeout=60000)
                page.bring_to_front()
                time.sleep(1.5)

                # Dismiss cookie consent banner — "AGREE" at (802, 695)
                try:
                    if page.locator('button:has-text("AGREE")').is_visible(timeout=2000):
                        page.mouse.click(802, 695)
                        slog(f"  [{email}] dismissed cookie banner")
                        time.sleep(0.8)
                except Exception:
                    pass

                # Click email (202,227) and type
                page.mouse.click(202, 227)
                time.sleep(0.2)
                page.keyboard.type(email, delay=40)

                # Click password (202,270) and type
                page.mouse.click(202, 270)
                time.sleep(0.2)
                page.keyboard.type(password, delay=40)
                slog(f"  [{email}] ✓ typed credentials")

                # Wait for Turnstile token
                try:
                    page.wait_for_function(
                        """() => {
                            const el = document.querySelector('input[name="cf-turnstile-response"]');
                            return el && el.value && el.value.length > 20;
                        }""",
                        timeout=45000, polling=300,
                    )
                    slog(f"  [{email}] Turnstile solved ✓")
                except _TO:
                    slog(f"  [{email}] Turnstile timeout — submitting anyway")

                # Click Login button at (202, 426)
                page.mouse.click(202, 426)
                slog(f"  [{email}] clicked Login")

                try:
                    page.wait_for_url(
                        lambda url: "/auth/" not in url and "login" not in url.lower(),
                        timeout=30000,
                    )
                except _TO:
                    pass

                cookies = context.cookies()
                session_cookies = [c for c in cookies if any(k in c["name"].lower() for k in ("session","token","auth","better"))]
                if not session_cookies:
                    time.sleep(3)
                    cookies = context.cookies()
                    session_cookies = [c for c in cookies if any(k in c["name"].lower() for k in ("session","token","auth","better"))]

                slog(f"  [{email}] cookies: {[c['name'] for c in cookies]}")

                cur = page.url
                if "login" in cur.lower() or "/auth/" in cur:
                    try:
                        err = page.locator('[role="alert"],.error,[class*="error"]').first.inner_text(timeout=2000)
                    except Exception:
                        err = "still on login page"
                    raise RuntimeError(f"Login did not redirect: {err}")
                if not session_cookies:
                    raise RuntimeError(f"No session cookies — got: {[c['name'] for c in cookies]}")

                with saved_lock:
                    saved[email] = {
                        "password": password,
                        "cookies": [{k: v for k, v in c.items()
                                     if k in ("name","value","domain","path","secure","httpOnly","sameSite")}
                                    for c in cookies],
                    }
                slog(f"  [{email}] ✓ saved {len(cookies)} cookies")

            except Exception as exc:
                slog(f"  [{email}] ✗ FAILED: {exc}")
                with saved_lock:
                    failed.append(email)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

        with setup_lock:
            setup_status["done"] = setup_status["done"] + 1

    # Rolling window: up to 6 browsers open at once.
    # As soon as one browser closes the next account starts immediately.
    MAX_CONCURRENT = 6
    slot_q = queue.Queue()
    for _i in range(MAX_CONCURRENT):
        slot_q.put(_i)

    def _login_with_slot(acc, idx):
        slot = slot_q.get()   # blocks until a browser slot is free
        try:
            login_one(acc, idx, slot)
        finally:
            slot_q.put(slot)  # release immediately so next account can start

    _threads = []
    for idx, acc in enumerate(accounts):
        t = threading.Thread(target=_login_with_slot, args=(acc, idx), daemon=True)
        _threads.append(t)
        t.start()
        time.sleep(0.3)  # slight stagger so browsers don't all open simultaneously

    for t in _threads:
        t.join()

    # Retry failed accounts up to 2 more times
    for attempt in range(1, 3):
        with saved_lock:
            if not failed:
                break
            retry_list = list(failed)
            failed.clear()
        slog(f"--- Retry attempt {attempt} for {len(retry_list)} failed account(s) ---")
        acc_map = {a["email"]: a for a in accounts}
        _retry_threads = []
        for idx, email_r in enumerate(retry_list):
            acc = acc_map.get(email_r)
            if not acc:
                continue
            t = threading.Thread(target=_login_with_slot, args=(acc, idx), daemon=True)
            _retry_threads.append(t)
            t.start()
            time.sleep(0.3)
        for t in _retry_threads:
            t.join()

    setup_status["running"] = False
    setup_status["done"]    = len(accounts)
    return saved, failed

# ══════════════════════════════════════════════════════════════════════════════
# Bot worker
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# Skill map data (from Auto Skill Pro Tampermonkey script)
# ══════════════════════════════════════════════════════════════════════════════

ALL_SKILLS_BY_MAP = {
    "Dust II": [
        {"id":"a1","requiredPrevSkillId":None,"cost":1},
        {"id":"a2","requiredPrevSkillId":"a1","cost":5},
        {"id":"a3","requiredPrevSkillId":"a2","cost":10},
        {"id":"a4","requiredPrevSkillId":"a3","cost":10},
        {"id":"a5","requiredPrevSkillId":"a4","cost":50},
        {"id":"a6","requiredPrevSkillId":"a2","cost":10},
        {"id":"a7","requiredPrevSkillId":"a6","cost":10},
        {"id":"a8","requiredPrevSkillId":"a7","cost":10},
        {"id":"a9","requiredPrevSkillId":"a8","cost":10},
        {"id":"a10","requiredPrevSkillId":"a1","cost":10},
        {"id":"a11","requiredPrevSkillId":"a10","cost":25},
        {"id":"a12","requiredPrevSkillId":"a11","cost":50},
        {"id":"a13","requiredPrevSkillId":"a10","cost":10},
        {"id":"a14","requiredPrevSkillId":"a13","cost":10},
        {"id":"a15","requiredPrevSkillId":"a14","cost":10},
        {"id":"a16","requiredPrevSkillId":None,"cost":1},
        {"id":"a17","requiredPrevSkillId":"a16","cost":20},
        {"id":"a18","requiredPrevSkillId":"a17","cost":20},
        {"id":"a19","requiredPrevSkillId":"a18","cost":20},
        {"id":"a20","requiredPrevSkillId":"a19","cost":50},
        {"id":"a21","requiredPrevSkillId":"a18","cost":10},
        {"id":"a22","requiredPrevSkillId":"a21","cost":10},
        {"id":"a23","requiredPrevSkillId":"a22","cost":25},
        {"id":"a24","requiredPrevSkillId":"a16","cost":10},
        {"id":"a25","requiredPrevSkillId":"a24","cost":10},
        {"id":"a26","requiredPrevSkillId":"a16","cost":10},
        {"id":"a27","requiredPrevSkillId":"a26","cost":10},
        {"id":"a28","requiredPrevSkillId":"a27","cost":20},
        {"id":"a29","requiredPrevSkillId":"a28","cost":50},
    ],
    "Mirage": [
        {"id":"b1","requiredPrevSkillId":None,"cost":10},
        {"id":"b2","requiredPrevSkillId":"b1","cost":30},
        {"id":"b3","requiredPrevSkillId":"b2","cost":30},
        {"id":"b4","requiredPrevSkillId":"b3","cost":40},
        {"id":"b5","requiredPrevSkillId":"b4","cost":100},
        {"id":"b6","requiredPrevSkillId":"b2","cost":30},
        {"id":"b7","requiredPrevSkillId":"b6","cost":50},
        {"id":"b8","requiredPrevSkillId":"b1","cost":20},
        {"id":"b9","requiredPrevSkillId":"b8","cost":30},
        {"id":"b10","requiredPrevSkillId":"b8","cost":30},
        {"id":"b11","requiredPrevSkillId":"b1","cost":50},
        {"id":"b12","requiredPrevSkillId":"b11","cost":30},
        {"id":"b13","requiredPrevSkillId":"b12","cost":30},
        {"id":"b14","requiredPrevSkillId":"b13","cost":100},
        {"id":"b15","requiredPrevSkillId":"b11","cost":30},
        {"id":"b16","requiredPrevSkillId":"b15","cost":30},
        {"id":"b17","requiredPrevSkillId":"b16","cost":300},
        {"id":"b18","requiredPrevSkillId":None,"cost":10},
        {"id":"b19","requiredPrevSkillId":"b18","cost":30},
        {"id":"b20","requiredPrevSkillId":"b19","cost":30},
        {"id":"b21","requiredPrevSkillId":"b20","cost":50},
        {"id":"b22","requiredPrevSkillId":"b21","cost":300},
        {"id":"b23","requiredPrevSkillId":"b18","cost":30},
        {"id":"b24","requiredPrevSkillId":"b23","cost":30},
        {"id":"b25","requiredPrevSkillId":"b18","cost":30},
        {"id":"b26","requiredPrevSkillId":"b25","cost":30},
        {"id":"b27","requiredPrevSkillId":"b26","cost":30},
        {"id":"b28","requiredPrevSkillId":"b27","cost":200},
        {"id":"b29","requiredPrevSkillId":"b25","cost":30},
        {"id":"b30","requiredPrevSkillId":"b29","cost":100},
        {"id":"b31","requiredPrevSkillId":"b18","cost":30},
        {"id":"b32","requiredPrevSkillId":"b31","cost":30},
        {"id":"b33","requiredPrevSkillId":"b32","cost":30},
        {"id":"b34","requiredPrevSkillId":"b32","cost":30},
    ],
    "Inferno": [
        {"id":"c1","requiredPrevSkillId":None,"cost":50},
        {"id":"c2","requiredPrevSkillId":"c1","cost":100},
        {"id":"c3","requiredPrevSkillId":"c2","cost":100},
        {"id":"c4","requiredPrevSkillId":"c2","cost":100},
        {"id":"c5","requiredPrevSkillId":"c2","cost":100},
        {"id":"c6","requiredPrevSkillId":"c1","cost":100},
        {"id":"c7","requiredPrevSkillId":"c6","cost":100},
        {"id":"c8","requiredPrevSkillId":"c7","cost":100},
        {"id":"c9","requiredPrevSkillId":"c8","cost":100},
        {"id":"c10","requiredPrevSkillId":"c9","cost":200},
        {"id":"c11","requiredPrevSkillId":"c10","cost":200},
        {"id":"c12","requiredPrevSkillId":"c11","cost":1000},
        {"id":"c13","requiredPrevSkillId":None,"cost":50},
        {"id":"c14","requiredPrevSkillId":"c13","cost":100},
        {"id":"c15","requiredPrevSkillId":"c14","cost":300},
        {"id":"c16","requiredPrevSkillId":"c15","cost":200},
        {"id":"c17","requiredPrevSkillId":"c16","cost":200},
        {"id":"c18","requiredPrevSkillId":"c15","cost":100},
        {"id":"c19","requiredPrevSkillId":"c18","cost":500},
        {"id":"c20","requiredPrevSkillId":"c15","cost":100},
        {"id":"c21","requiredPrevSkillId":"c20","cost":300},
        {"id":"c22","requiredPrevSkillId":"c21","cost":500},
        {"id":"c23","requiredPrevSkillId":"c22","cost":2000},
        {"id":"c24","requiredPrevSkillId":"c13","cost":100},
        {"id":"c25","requiredPrevSkillId":"c24","cost":100},
        {"id":"c26","requiredPrevSkillId":"c25","cost":100},
        {"id":"c27","requiredPrevSkillId":"c26","cost":200},
        {"id":"c28","requiredPrevSkillId":"c27","cost":300},
        {"id":"c29","requiredPrevSkillId":"c28","cost":1000},
        {"id":"c30","requiredPrevSkillId":"c13","cost":100},
        {"id":"c31","requiredPrevSkillId":"c30","cost":100},
        {"id":"c32","requiredPrevSkillId":"c31","cost":100},
        {"id":"c33","requiredPrevSkillId":"c31","cost":100},
        {"id":"c34","requiredPrevSkillId":"c31","cost":200},
        {"id":"c35","requiredPrevSkillId":"c34","cost":200},
    ],
    "Ancient": [
        {"id":"d1","requiredPrevSkillId":None,"cost":500},
        {"id":"d2","requiredPrevSkillId":"d1","cost":1000},
        {"id":"d3","requiredPrevSkillId":"d2","cost":1000},
        {"id":"d4","requiredPrevSkillId":"d3","cost":1000},
        {"id":"d5","requiredPrevSkillId":"d3","cost":1000},
        {"id":"d6","requiredPrevSkillId":"d3","cost":1000},
        {"id":"d7","requiredPrevSkillId":"d1","cost":5000},
        {"id":"d8","requiredPrevSkillId":"d1","cost":1000},
        {"id":"d9","requiredPrevSkillId":"d8","cost":1000},
        {"id":"d10","requiredPrevSkillId":"d9","cost":1000},
        {"id":"d11","requiredPrevSkillId":"d9","cost":2500},
        {"id":"d12","requiredPrevSkillId":"d8","cost":1000},
        {"id":"d13","requiredPrevSkillId":None,"cost":500},
        {"id":"d14","requiredPrevSkillId":"d13","cost":1000},
        {"id":"d15","requiredPrevSkillId":"d14","cost":1000},
        {"id":"d16","requiredPrevSkillId":"d15","cost":1000},
        {"id":"d17","requiredPrevSkillId":"d16","cost":10000},
        {"id":"d18","requiredPrevSkillId":"d13","cost":1000},
        {"id":"d19","requiredPrevSkillId":"d18","cost":1000},
        {"id":"d20","requiredPrevSkillId":"d19","cost":1000},
        {"id":"d21","requiredPrevSkillId":"d18","cost":1000},
        {"id":"d22","requiredPrevSkillId":"d21","cost":1000},
        {"id":"d23","requiredPrevSkillId":"d19","cost":10000},
        {"id":"d24","requiredPrevSkillId":"d13","cost":1000},
        {"id":"d25","requiredPrevSkillId":"d24","cost":1000},
    ],
    "Anubis": [
        {"id":"e1","requiredPrevSkillId":None,"cost":5000},
        {"id":"e2","requiredPrevSkillId":"e1","cost":10000},
        {"id":"e3","requiredPrevSkillId":"e2","cost":10000},
        {"id":"e4","requiredPrevSkillId":"e3","cost":10000},
        {"id":"e5","requiredPrevSkillId":"e4","cost":10000},
        {"id":"e6","requiredPrevSkillId":"e1","cost":10000},
        {"id":"e7","requiredPrevSkillId":"e6","cost":10000},
        {"id":"e8","requiredPrevSkillId":"e7","cost":10000},
        {"id":"e9","requiredPrevSkillId":"e8","cost":10000},
        {"id":"e10","requiredPrevSkillId":"e6","cost":10000},
        {"id":"e11","requiredPrevSkillId":"e10","cost":10000},
        {"id":"e12","requiredPrevSkillId":"e11","cost":10000},
        {"id":"e13","requiredPrevSkillId":"e12","cost":10000},
        {"id":"e14","requiredPrevSkillId":"e13","cost":10000},
        {"id":"e15","requiredPrevSkillId":"e10","cost":10000},
        {"id":"e16","requiredPrevSkillId":"e15","cost":10000},
        {"id":"e17","requiredPrevSkillId":"e16","cost":10000},
        {"id":"e18","requiredPrevSkillId":"e17","cost":50000},
        {"id":"e19","requiredPrevSkillId":None,"cost":5000},
        {"id":"e20","requiredPrevSkillId":"e19","cost":20000},
        {"id":"e21","requiredPrevSkillId":"e20","cost":20000},
        {"id":"e22","requiredPrevSkillId":"e21","cost":20000},
        {"id":"e23","requiredPrevSkillId":"e22","cost":20000},
        {"id":"e24","requiredPrevSkillId":"e19","cost":10000},
        {"id":"e25","requiredPrevSkillId":"e24","cost":10000},
        {"id":"e26","requiredPrevSkillId":"e25","cost":25000},
        {"id":"e27","requiredPrevSkillId":"e26","cost":10000},
        {"id":"e28","requiredPrevSkillId":"e27","cost":10000},
        {"id":"e29","requiredPrevSkillId":"e28","cost":10000},
        {"id":"e30","requiredPrevSkillId":"e29","cost":25000},
        {"id":"e31","requiredPrevSkillId":"e26","cost":500000},
        {"id":"e32","requiredPrevSkillId":"e26","cost":500000},
        {"id":"e33","requiredPrevSkillId":"e24","cost":10000},
        {"id":"e34","requiredPrevSkillId":"e33","cost":10000},
        {"id":"e35","requiredPrevSkillId":"e34","cost":10000},
        {"id":"e36","requiredPrevSkillId":"e35","cost":25000},
        {"id":"e37","requiredPrevSkillId":"e19","cost":30000},
        {"id":"e38","requiredPrevSkillId":"e19","cost":10000},
        {"id":"e39","requiredPrevSkillId":"e38","cost":10000},
        {"id":"e40","requiredPrevSkillId":"e39","cost":25000},
        {"id":"e41","requiredPrevSkillId":"e40","cost":50000},
    ],
    "Cobblestone": [
        {"id":"f1","requiredPrevSkillId":None,"cost":10000},
        {"id":"f2","requiredPrevSkillId":"f1","cost":20000},
        {"id":"f3","requiredPrevSkillId":"f1","cost":20000},
        {"id":"f4","requiredPrevSkillId":"f3","cost":20000},
        {"id":"f5","requiredPrevSkillId":"f4","cost":40000},
        {"id":"f6","requiredPrevSkillId":"f5","cost":40000},
        {"id":"f7","requiredPrevSkillId":"f6","cost":80000},
        {"id":"f8","requiredPrevSkillId":"f7","cost":150000},
        {"id":"f9","requiredPrevSkillId":"f3","cost":20000},
        {"id":"f10","requiredPrevSkillId":"f9","cost":20000},
        {"id":"f11","requiredPrevSkillId":"f10","cost":50000},
        {"id":"f12","requiredPrevSkillId":"f11","cost":20000},
        {"id":"f13","requiredPrevSkillId":"f12","cost":100000},
        {"id":"f14","requiredPrevSkillId":"f11","cost":20000},
        {"id":"f15","requiredPrevSkillId":"f14","cost":100000},
        {"id":"f16","requiredPrevSkillId":None,"cost":50000},
        {"id":"f17","requiredPrevSkillId":"f16","cost":20000},
        {"id":"f18","requiredPrevSkillId":"f17","cost":20000},
        {"id":"f19","requiredPrevSkillId":"f16","cost":20000},
        {"id":"f20","requiredPrevSkillId":"f19","cost":500000},
        {"id":"f21","requiredPrevSkillId":"f16","cost":20000},
        {"id":"f22","requiredPrevSkillId":"f21","cost":20000},
        {"id":"f23","requiredPrevSkillId":"f22","cost":20000},
        {"id":"f24","requiredPrevSkillId":"f23","cost":50000},
        {"id":"f25","requiredPrevSkillId":"f24","cost":20000},
        {"id":"f26","requiredPrevSkillId":"f25","cost":20000},
        {"id":"f27","requiredPrevSkillId":"f26","cost":1000000},
        {"id":"f28","requiredPrevSkillId":"f24","cost":20000},
        {"id":"f29","requiredPrevSkillId":"f28","cost":20000},
        {"id":"f30","requiredPrevSkillId":"f29","cost":25000},
        {"id":"f31","requiredPrevSkillId":"f30","cost":3000000},
    ],
}

# Build flat lookup: skillId -> mapName
_skill_to_map = {}
for _mn, _skills in ALL_SKILLS_BY_MAP.items():
    for _sk in _skills:
        _skill_to_map[_sk["id"]] = _mn

# Calculate depths for correct buy/sell ordering
def _calc_depths():
    depths = {}
    def depth(sid):
        if sid in depths:
            return depths[sid]
        # find parent
        par = None
        for skills in ALL_SKILLS_BY_MAP.values():
            for sk in skills:
                if sk["id"] == sid:
                    par = sk["requiredPrevSkillId"]
                    break
            if par is not None or (par is None and any(sk["id"] == sid for sk in skills)):
                break
        if not par:
            depths[sid] = 0
            return 0
        depths[sid] = 1 + depth(par)
        return depths[sid]
    for _map_skills in ALL_SKILLS_BY_MAP.values():
        for _sk in _map_skills:
            depth(_sk["id"])
    return depths

SKILL_DEPTHS = _calc_depths()
ALL_SKILL_IDS = list(_skill_to_map.keys())

# Preset skill-id lists (from the script)
SKILL_PRESETS = {
    "f27Build": {
        "name": "5K case skillmap",
        "description": "$25 cases, 33.3%+ case %, rest to vault gen/min",
        "skillIds": [
            'f16','f21','f22','f23','f24','f25','f26','f27','f19',
            'e19','e24','e33','e34','e35','e36',
            'e1','e6','e10','e11','e15','e16','e17','e18',
            'd13','d14','d15','d16','d17','d24','d25','d18','d21',
            'd1','d2','d3','d5','d8','d9','d11',
            'c13','c14','c15','c16','c17','c24',
            'c30','c31','c34','c35',
            'b1','b11','b12','b13','b14',
        ],
    },
    "optimizedBuild": {
        "name": "Global Elite Optimized",
        "description": "33.3%+ cases, rest to vault gen/min",
        "skillIds": [
            'b1','b11','b15','b16','b17','b12','b13','b14','b18','b25','b26','b27','b28',
            'c13','c14','c15','c16','c17','c30','c31','c34','c35','c24',
            'd13','d24','d25','d14','d15','d16','d17','d18','d21','d1','d2','d3','d5','d8','d9','d11','d19',
            'e1','e6','e10','e15','e16','e17','e18','e11','e19','e24','e33','e34','e35','e36',
        ],
    },
    "imageBuild": {
        "name": "ZSBs 5k case map",
        "description": "ZSBs case clicking map from Discord, $25 cases, 2x multi",
        "skillIds": [
            'f16','f21','f22','f23','f24','f25','f26','f27',
            'f28','f29','f30','f31',
            'f19','f20',
            'e1','e6','e10','e15','e16','e17','e18','e11',
            'e19','e24','e33','e34','e35','e36',
            'd13','d14','d15','d16','d17',
            'd24','d25',
            'd18','d21',
            'd1','d2','d3','d5',
            'd8','d9','d11',
            'c13','c14','c15','c16','c17',
            'c24',
            'c30','c31','c34','c35',
        ],
    },
    "maxClickBuild": {
        "name": "Max Click Money ($1.00+/click)",
        "description": "Prioritizes Click Money above all else",
        "skillIds": [
            'f1','f3','f4','f5','f6','f7','f8','f9','f10','f11','f12','f13','f14','f15',
            'f16','f21','f22','f23','f24','f28','f19',
            'e19','e38','e39','e40','e41','e1','e2','e3','e4','e5','e24','e33','e34','e35','e36',
            'd13','d14','d15','d16','d17','d1','d2','d3','d4',
            'c13','c14','c15','c20','c21','c22','c23','c18','c19',
        ],
    },
}


def apply_skillmap(sess: cffi_requests.Session, email: str, preset_id: str) -> dict:
    """Sell all current skills then buy the target preset. Returns summary dict."""
    preset = SKILL_PRESETS.get(preset_id)
    if not preset:
        return {"error": f"Unknown preset: {preset_id}"}

    log(email, f"Skillmap: selling all skills…", "info")

    # Step 1: sell all in depth-descending order (children first)
    to_sell = sorted(ALL_SKILL_IDS, key=lambda sid: SKILL_DEPTHS.get(sid, 0), reverse=True)
    sold = 0
    for sid in to_sell:
        map_name = _skill_to_map.get(sid)
        if not map_name:
            continue
        try:
            r = sess.request(
                "DELETE",
                f"{BASE}/api/skill",
                json={"mapName": map_name, "skillId": sid},
                headers=HEADERS,
                timeout=10,
            )
            if r.status_code == 200:
                sold += 1
            elif r.status_code == 400:
                pass  # not owned, fine
            elif r.status_code == 429:
                time.sleep(2)
                sess.request("DELETE", f"{BASE}/api/skill",
                             json={"mapName": map_name, "skillId": sid},
                             headers=HEADERS, timeout=10)
        except Exception:
            pass
        time.sleep(SKILLMAP_API_DELAY)

    log(email, f"Skillmap: sold {sold} skills — now buying preset '{preset['name']}'…", "info")

    # Step 2: buy preset skills in depth-ascending order (roots first)
    unique_ids = list(dict.fromkeys(preset["skillIds"]))  # deduplicate preserving order
    to_buy = sorted(unique_ids, key=lambda sid: SKILL_DEPTHS.get(sid, 0))
    bought = 0
    errors = 0
    for sid in to_buy:
        map_name = _skill_to_map.get(sid)
        if not map_name:
            continue
        try:
            r = sess.post(
                f"{BASE}/api/skill",
                json={"mapName": map_name, "skillId": sid},
                headers=HEADERS,
                timeout=10,
            )
            if r.status_code == 200:
                bought += 1
            elif r.status_code == 429:
                time.sleep(2)
                r2 = sess.post(f"{BASE}/api/skill",
                               json={"mapName": map_name, "skillId": sid},
                               headers=HEADERS, timeout=10)
                if r2.status_code == 200:
                    bought += 1
                else:
                    errors += 1
            else:
                errors += 1
        except Exception:
            errors += 1
        time.sleep(SKILLMAP_API_DELAY)

    msg = f"Skillmap applied: {bought}/{len(to_buy)} skills bought"
    if errors:
        msg += f", {errors} errors"
    log(email, msg, "success" if errors == 0 else "warn")
    return {"ok": True, "sold": sold, "bought": bought, "errors": errors}


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket-based account worker (Socket.IO)
# ══════════════════════════════════════════════════════════════════════════════

def _build_cookie_header(acc_data: dict) -> str:
    """Build a Cookie: header string from saved cookie list."""
    return "; ".join(f"{c['name']}={c['value']}" for c in acc_data.get("cookies", []))


def _socketio_handshake(acc_data: dict) -> str | None:
    """
    Perform the Socket.IO HTTP polling handshake to get the socket `sid`.
    Returns the sid string or None on failure.
    """
    sess = make_session(acc_data)
    try:
        r = sess.get(
            f"{BASE}/socket.io/?EIO=4&transport=polling",
            headers={**HEADERS, "Accept": "*/*"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        # Response is like: 97{"sid":"...", ...}
        m = re.search(r'\{.*\}', r.text)
        if not m:
            return None
        data = json.loads(m.group())
        return data.get("sid")
    except Exception:
        return None


def account_worker(email: str, acc_data: dict, stop_event: threading.Event):
    with stats_lock:
        stats[email].update({
            "status":     "starting",
            "running":    True,
            "start_time": time.time(),
            "last_err":   "",
        })

    log(email, "Started (WebSocket)")

    # Use HTTP session for /api/me, vault, etc.
    http_sess = make_session(acc_data)
    fetch_me(http_sess, email)

    last_me  = time.time()
    cookie_hdr = _build_cookie_header(acc_data)

    def _run_ws_loop():
        """Returns False if we should stop entirely, True to reconnect."""
        nonlocal last_me
        click_count = 0
        ws_errors   = 0
        connected   = threading.Event()
        ws_ref      = [None]
        tick_count  = [0]  # clicks sent this connection

        with stats_lock:
            batch = stats[email].get("click_batch", CLICK_BATCH)

        # Get SIO sid via HTTP polling handshake
        sid = _socketio_handshake(acc_data)
        if not sid:
            with stats_lock:
                stats[email].update({"status": "ws handshake failed", "last_err": "no sid"})
            log(email, "Socket.IO handshake failed — retrying", "warn")
            return True  # reconnect

        ws_url = (
            f"wss://case-clicker.com/socket.io/?EIO=4&transport=websocket&sid={sid}"
        )

        def on_open(ws):
            ws_ref[0] = ws
            # Socket.IO upgrade probe
            ws.send("2probe")

        def on_message(ws, message):
            nonlocal click_count
            if message == "3probe":
                # Upgrade confirmed — send upgrade frame then connect to namespace
                ws.send("5")
                ws.send("40")  # Socket.IO namespace connect
                return         # wait for server "40" before clicking
            if message.startswith("40"):
                # Namespace connected — safe to start clicking
                connected.set()
                with stats_lock:
                    stats[email].update({"status": "clicking", "last_err": ""})
                log(email, "WebSocket connected", "success")
                return
            if message.startswith("2"):  # ping
                ws.send("3")  # pong
                return
            # Parse Socket.IO event messages (42[...])
            if message.startswith("42"):
                try:
                    payload = json.loads(message[2:])
                    evt = payload[0] if payload else ""
                    data = payload[1] if len(payload) > 1 else {}
                    if evt == "caseclick":
                        progress = float(data.get("caseProgress", data.get("progress", 0)))
                        balance  = data.get("balance") or data.get("money")
                        now      = time.time()
                        with stats_lock:
                            stats[email]["clicks"]  += batch
                            stats[email]["progress"] = progress
                            stats[email]["status"]   = "clicking"
                            stats[email]["last_err"] = ""
                            if balance is not None:
                                stats[email]["balance"] = balance
                            if progress >= 99.9 or data.get("case"):
                                stats[email]["cases_won"] += 1
                                stats[email]["last_case"]  = datetime.datetime.now().strftime("%H:%M:%S")
                                case_name = (data.get("case") or {}).get("name", "")
                                stats[email]["last_case_name"] = case_name or None
                                log(email,
                                    f"Case won!{' — ' + case_name if case_name else ''}",
                                    "success")
                except Exception:
                    pass

        def on_error(ws, err):
            with stats_lock:
                stats[email].update({"status": "ws error", "last_err": str(err)[:80]})

        def on_close(ws, code, msg):
            connected.set()  # unblock clicker thread if waiting
            with stats_lock:
                if not stop_event.is_set():
                    stats[email].update({"status": "reconnecting…", "last_err": ""})

        ws = websocket.WebSocketApp(
            ws_url,
            header={"Cookie": cookie_hdr, "Origin": BASE, "User-Agent": "Mozilla/5.0"},
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        # Run WebSocket in background thread
        ws_thread = threading.Thread(
            target=ws.run_forever,
            kwargs={"ping_interval": 0},  # we handle pings manually
            daemon=True,
        )
        ws_thread.start()

        # Wait for connection upgrade (up to 15s)
        if not connected.wait(timeout=15):
            ws.close()
            with stats_lock:
                stats[email].update({"status": "ws timeout", "last_err": "no upgrade"})
            return True  # reconnect

        if stop_event.is_set():
            ws.close()
            return False

        # ── Clicking loop ────────────────────────────────────────────────────
        while not stop_event.is_set() and ws_thread.is_alive():
            try:
                if ws_ref[0] and ws_ref[0].sock and ws_ref[0].sock.connected:
                    with stats_lock:
                        batch = stats[email].get("click_batch", CLICK_BATCH)
                    ws_ref[0].send("42" + json.dumps(["caseclick", batch]))
                    click_count += batch

                    # Vault every N clicks
                    if click_count % (VAULT_EVERY_N_CLICKS * batch) < batch:
                        try:
                            ws_ref[0].send('42' + json.dumps(["collectVault"]))
                        except Exception:
                            pass

                    # Update CPS in stats periodically
                    tick_count[0] += 1
                    if tick_count[0] % 10 == 0:
                        with stats_lock:
                            stats[email]["cps"] = round(batch / CLICK_DELAY, 1)

                    # HTTP /api/me refresh
                    if time.time() - last_me > ME_INTERVAL:
                        try:
                            fetch_me(http_sess, email)
                        except Exception:
                            pass
                        last_me = time.time()

                else:
                    # Socket lost
                    break

            except websocket.WebSocketConnectionClosedException:
                break
            except Exception as exc:
                ws_errors += 1
                with stats_lock:
                    stats[email].update({"last_err": str(exc)[:80]})
                if ws_errors >= 5:
                    break

            stop_event.wait(CLICK_DELAY)

        ws.close()
        ws_thread.join(timeout=3)
        return not stop_event.is_set()  # reconnect if not intentionally stopped

    # Outer reconnect loop
    while not stop_event.is_set():
        should_reconnect = _run_ws_loop()
        if not should_reconnect or stop_event.is_set():
            break
        log(email, "Reconnecting in 5s…", "warn")
        stop_event.wait(5)

    with stats_lock:
        final = stats[email].get("status", "")
        if final not in ("re-login needed",):
            stats[email]["status"] = "stopped"
        stats[email].update({"running": False, "start_time": None, "cps": 0.0})

    log(email, f"Stopped [{stats[email]['status']}]")

# ══════════════════════════════════════════════════════════════════════════════
# Flask app
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

# ── Dashboard HTML (inline, no external files needed) ────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CaseBot Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:#09090f; --bg2:#10101a; --bg3:#18182a; --bg4:#21213a;
      --border:#2a2a48; --border2:#3a3a5c;
      --text:#d8d8f0; --text2:#5858a0; --text3:#8080b8;
      --green:#00e676; --yellow:#ffc107; --red:#ff4560;
      --blue:#4da8ff; --purple:#a566ff; --cyan:#00d4ff; --orange:#ff7043;
      --r:8px; --shadow:0 4px 24px rgba(0,0,0,.6);
    }
    body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif; font-size:13px; line-height:1.5; min-height:100vh; }
    ::-webkit-scrollbar{width:6px;height:6px} ::-webkit-scrollbar-track{background:var(--bg)} ::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}

    /* Nav */
    #nav { position:sticky;top:0;z-index:200;background:var(--bg2);border-bottom:1px solid var(--border);padding:0 18px;height:50px;display:flex;align-items:center;gap:10px; }
    .nav-brand { font-size:16px;font-weight:700;color:var(--cyan);letter-spacing:.4px;margin-right:auto;display:flex;align-items:center;gap:8px; }
    .pulse-dot { width:8px;height:8px;border-radius:50%;background:var(--green);flex-shrink:0;box-shadow:0 0 0 0 rgba(0,230,118,.6);animation:ripple 2s infinite; }
    @keyframes ripple { 0%{box-shadow:0 0 0 0 rgba(0,230,118,.5)} 70%{box-shadow:0 0 0 8px rgba(0,230,118,0)} 100%{box-shadow:0 0 0 0 rgba(0,230,118,0)} }

    /* Buttons */
    .btn { display:inline-flex;align-items:center;gap:5px;padding:5px 13px;border:1px solid var(--border2);border-radius:6px;background:var(--bg3);color:var(--text);cursor:pointer;font-size:12px;font-weight:600;transition:all .15s;white-space:nowrap;user-select:none; }
    .btn:hover { background:var(--bg4);border-color:var(--text3); }
    .btn:disabled { opacity:.4;cursor:default; }
    .btn.go   { border-color:var(--green);color:var(--green); } .btn.go:hover   { background:rgba(0,230,118,.1); }
    .btn.halt { border-color:var(--red);color:var(--red); }     .btn.halt:hover { background:rgba(255,69,96,.1); }
    .btn.warn { border-color:var(--yellow);color:var(--yellow); } .btn.warn:hover { background:rgba(255,193,7,.12); }
    .btn.sm { padding:3px 9px;font-size:11px; }

    /* KPIs */
    #summary { display:grid;grid-template-columns:repeat(auto-fill,minmax(135px,1fr));gap:8px;padding:12px 18px;border-bottom:1px solid var(--border); }
    .kpi { background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:10px 13px; }
    .kpi .lbl { font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--text2);margin-bottom:4px; }
    .kpi .val { font-size:20px;font-weight:700;font-variant-numeric:tabular-nums; }
    .kpi .sub { font-size:9px;color:var(--text2);margin-top:1px; }
    .kpi.g .val{color:var(--green)} .kpi.c .val{color:var(--cyan)} .kpi.p .val{color:var(--purple)} .kpi.y .val{color:var(--yellow)} .kpi.b .val{color:var(--blue)} .kpi.o .val{color:var(--orange)}

    /* Main layout */
    #main { display:flex; }
    #panel-table { flex:1;overflow:auto;border-right:1px solid var(--border);min-height:calc(100vh - 50px - 100px); }
    #toolbar { display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--border);background:var(--bg2);position:sticky;top:0;z-index:10; }
    #toolbar h2 { font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--text2);margin-right:auto; }
    #refresh-txt { font-size:9px;color:var(--text2); }

    table { width:100%;border-collapse:collapse; }
    thead th { position:sticky;top:38px;z-index:5;background:var(--bg2);padding:7px 11px;text-align:left;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--text2);border-bottom:1px solid var(--border);white-space:nowrap; }
    tbody tr { border-bottom:1px solid rgba(255,255,255,.04);transition:background .1s; }
    tbody tr:last-child { border-bottom:none; }
    tbody tr:hover { background:rgba(255,255,255,.025); }
    td { padding:8px 11px;vertical-align:middle;white-space:nowrap; }

    .acct .name { font-weight:600;font-size:13px; }
    .acct .mail { font-size:10px;color:var(--text2); }

    .badge { display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:20px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap; }
    .bd { width:5px;height:5px;border-radius:50%;flex-shrink:0; }
    .bs-clicking  { background:rgba(0,230,118,.12);color:var(--green);border:1px solid rgba(0,230,118,.25); } .bs-clicking .bd  { background:var(--green);animation:blink .9s infinite; }
    .bs-stopped   { background:rgba(255,255,255,.05);color:var(--text2);border:1px solid var(--border); }    .bs-stopped .bd   { background:var(--text2); }
    .bs-rate      { background:rgba(255,193,7,.12);color:var(--yellow);border:1px solid rgba(255,193,7,.25); } .bs-rate .bd  { background:var(--yellow);animation:blink 1.5s infinite; }
    .bs-error     { background:rgba(255,69,96,.12);color:var(--red);border:1px solid rgba(255,69,96,.25); }    .bs-error .bd { background:var(--red); }
    .bs-starting  { background:rgba(77,168,255,.12);color:var(--blue);border:1px solid rgba(77,168,255,.25); } .bs-starting .bd { background:var(--blue);animation:blink .5s infinite; }
    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }

    .prog-wrap { display:flex;align-items:center;gap:7px;min-width:120px; }
    .prog-track { flex:1;height:5px;background:var(--bg4);border-radius:3px;overflow:hidden; }
    .prog-fill { height:100%;border-radius:3px;background:linear-gradient(90deg,var(--purple),var(--cyan));transition:width .5s ease; }
    .prog-pct { font-size:10px;color:var(--text2);min-width:35px;text-align:right;font-variant-numeric:tabular-nums; }

    .num { font-variant-numeric:tabular-nums; }
    .dim { color:var(--text2); }
    .g { color:var(--green); } .y { color:var(--yellow); } .p { color:var(--purple); }
    .c { color:var(--cyan); }  .r { color:var(--red); }

    /* Log panel */
    #panel-log { width:300px;flex-shrink:0;display:flex;flex-direction:column;max-height:calc(100vh - 50px - 100px);overflow:hidden; }
    #panel-log .ph { padding:8px 13px;border-bottom:1px solid var(--border);background:var(--bg2);font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--text2); }
    #log-scroll { flex:1;overflow-y:auto; }
    .log-row { display:flex;flex-direction:column;gap:1px;padding:6px 11px;border-bottom:1px solid rgba(255,255,255,.04);font-size:11px; }
    .log-header { display:flex;gap:7px;align-items:baseline; }
    .log-ts { font-family:monospace;color:var(--text2);font-size:9px;flex-shrink:0; }
    .log-em { color:var(--cyan);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1; }
    .log-msg { color:var(--text);padding-left:2px; }
    .log-row.success .log-msg{color:var(--green)} .log-row.error .log-msg{color:var(--red)} .log-row.warn .log-msg{color:var(--yellow)}

    /* Setup modal */
    #setup-modal { display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:500;align-items:center;justify-content:center; }
    #setup-modal.open { display:flex; }
    #setup-box { background:var(--bg2);border:1px solid var(--border2);border-radius:12px;padding:28px 32px;max-width:480px;width:90%;box-shadow:var(--shadow); }
    #setup-box h2 { font-size:18px;margin-bottom:8px; }
    #setup-box p { color:var(--text2);font-size:12px;margin-bottom:16px; }
    #setup-log { background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px 12px;height:180px;overflow-y:auto;font-family:monospace;font-size:11px;color:var(--text3);white-space:pre-wrap; }
    #setup-progress { margin-top:10px;height:5px;background:var(--bg4);border-radius:3px;overflow:hidden; }
    #setup-progress-fill { height:100%;background:linear-gradient(90deg,var(--purple),var(--cyan));width:0%;transition:width .4s; }

    #toast { position:fixed;bottom:18px;right:18px;background:var(--bg3);border:1px solid var(--border2);border-radius:var(--r);padding:9px 16px;font-size:13px;box-shadow:var(--shadow);opacity:0;pointer-events:none;transition:opacity .25s;z-index:999; }
    #toast.show { opacity:1; }

    /* Skillmap modal */
    #skillmap-modal { display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:500;align-items:center;justify-content:center; }
    #skillmap-modal.open { display:flex; }
    #skillmap-box { background:var(--bg2);border:1px solid var(--border2);border-radius:12px;padding:28px 32px;max-width:520px;width:95%;box-shadow:var(--shadow);max-height:90vh;overflow-y:auto; }
    #skillmap-box h2 { font-size:18px;margin-bottom:8px; }
    #skillmap-box p { color:var(--text2);font-size:12px;margin-bottom:14px; }
    .preset-row { display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;margin-bottom:8px; }
    .preset-info { flex:1; }
    .preset-info .pname { font-weight:600;font-size:13px; }
    .preset-info .pdesc { font-size:11px;color:var(--text2);margin-top:2px; }
    .preset-info .pbadge { font-size:10px;color:var(--purple); }
    #skillmap-account-select { width:100%;padding:6px 10px;background:var(--bg);border:1px solid var(--border2);border-radius:6px;color:var(--text);font-size:12px;margin-bottom:14px; }
    #skillmap-status { margin-top:10px;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;font-size:12px;color:var(--text3);min-height:32px; }

    @media (max-width:860px) { #panel-log { display:none; } }
  </style>
</head>
<body>

<nav id="nav">
  <div class="nav-brand">
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none">
      <path d="M10 2L12.5 7.5H18L13.5 11L15.5 17L10 13.5L4.5 17L6.5 11L2 7.5H7.5L10 2Z" fill="var(--cyan)" opacity=".85"/>
    </svg>
    CaseBot <span style="color:var(--text2);font-weight:400;font-size:11px;margin-left:2px">Dashboard</span>
  </div>
  <span class="pulse-dot" title="Auto-refreshing every 2s"></span>
  <button class="btn warn" onclick="openSetup()">&#9881; Login Setup</button>
  <button class="btn" style="border-color:var(--purple);color:var(--purple)" onclick="openSkillmap()">&#9733; Skillmap</button>
  <button class="btn go"   onclick="startAll()">&#9654; Start All</button>
  <button class="btn halt" onclick="stopAll()">&#9632; Stop All</button>
  <button class="btn halt" style="background:#3a1010;border-color:#8b0000;color:#ff6b6b" onclick="removeAll()">&#128465; Remove All</button>
</nav>

<div id="summary">
  <div class="kpi g"><div class="lbl">Running</div><div class="val" id="k-running">—</div><div class="sub" id="k-running-sub"></div></div>
  <div class="kpi c"><div class="lbl">Total Clicks</div><div class="val" id="k-clicks">—</div><div class="sub">this session</div></div>
  <div class="kpi p"><div class="lbl">Cases Won</div><div class="val" id="k-cases">—</div><div class="sub">this session</div></div>
  <div class="kpi y"><div class="lbl">Total Balance</div><div class="val" id="k-balance">—</div></div>
  <div class="kpi b"><div class="lbl">Avg CPS</div><div class="val" id="k-cps">—</div><div class="sub">clicks / sec</div></div>
  <div class="kpi o"><div class="lbl">Avg Level</div><div class="val" id="k-level">—</div></div>
</div>

<div id="main">
  <div id="panel-table">
    <div id="toolbar">
      <h2>Accounts</h2>
      <span id="refresh-txt"></span>
    </div>
    <table>
      <thead><tr>
        <th>#</th><th>Account</th><th>Status</th><th>Balance</th>
        <th>Lvl</th><th>Cases</th><th>Case Progress</th>
        <th>CPS</th><th>Clicks</th><th>Last Case</th><th>Uptime</th><th>Action</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

  <div id="panel-log">
    <div class="ph">&#128221; Event Log</div>
    <div id="log-scroll"></div>
  </div>
</div>

<!-- Skillmap modal -->
<div id="skillmap-modal">
  <div id="skillmap-box">
    <h2>&#9733; Apply Skillmap</h2>
    <p>Sells all current skills then buys the chosen preset. Pick an account and a preset below.</p>
    <label style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--text2)">Account</label>
    <select id="skillmap-account-select"></select>
    <label style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--text2)">Preset</label>
    <div id="presets-list" style="margin-top:8px;margin-bottom:4px"></div>
    <div id="skillmap-status">Ready.</div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn" style="border-color:var(--purple);color:var(--purple)" id="skillmap-apply-btn" onclick="applySkillmap()">&#9733; Apply</button>
      <button class="btn" onclick="closeSkillmap()">Close</button>
    </div>
  </div>
</div>

<!-- Setup modal -->
<div id="setup-modal">
  <div id="setup-box">
    <h2>&#128273; Account Login Setup</h2>
    <p>Opens a browser window to log into all accounts from <code>accounts.json</code>.
       Turnstile captcha is solved automatically in a real browser.
       Sessions are saved to <code>sessions.json</code> and used for pure-HTTP clicking.</p>
    <div id="setup-log">Ready. Click "Start Login" to begin.</div>
    <div id="setup-progress"><div id="setup-progress-fill"></div></div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn go" id="setup-start-btn" onclick="startSetup()">&#9654; Start Login</button>
      <button class="btn"    onclick="closeSetup()">Close</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
"use strict";
function esc(s){if(s==null)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function $(id){return document.getElementById(id);}
function fmtNum(n,d=0){if(n==null||isNaN(n))return'—';return Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});}
function fmtBig(n){if(n==null||isNaN(n))return'—';if(n>=1e12)return(n/1e12).toFixed(2)+'T';if(n>=1e9)return(n/1e9).toFixed(2)+'B';if(n>=1e6)return(n/1e6).toFixed(2)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return fmtNum(n);}
function fmtUptime(s){if(!s||s<=0)return'—';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;if(h>0)return`${h}h ${m}m`;if(m>0)return`${m}m ${ss}s`;return`${ss}s`;}
function statusClass(st){if(!st)return'stopped';const s=st.toLowerCase();if(s.includes('clicking'))return'clicking';if(s.includes('rate'))return'rate';if(s.includes('start'))return'starting';if(s.includes('expired')||s.includes('re-login')||s.includes('error')||s.includes('network'))return'error';return'stopped';}
function capFirst(s){return s?s.charAt(0).toUpperCase()+s.slice(1):'';}
function showToast(msg,ms=2500){const t=$('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),ms);}

async function post(url,body={}){try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return await r.json();}catch(e){return{error:String(e)};}}

async function startAll(){const res=await post('/api/start');if(res.error){showToast('⚠ '+res.error);return;}showToast(`▶ Starting ${res.started?res.started.length:0} account(s)…`);}
async function stopAll(){await post('/api/stop');showToast('■ Stopping all…');}
async function removeAll(){if(!confirm('Stop all bots and clear ALL sessions?'))return;await post('/api/remove');showToast('🗑 All bots removed');}
async function toggleAccount(email,running){await post(running?'/api/stop':'/api/start',{email});showToast(running?'Stopping '+email:'Starting '+email);}

function renderSummary(accs){
  const run=accs.filter(a=>a.running);
  const tc=accs.reduce((s,a)=>s+(a.clicks||0),0);
  const tw=accs.reduce((s,a)=>s+(a.cases_won||0),0);
  const bals=accs.filter(a=>a.balance!=null).map(a=>a.balance);
  const tb=bals.length?bals.reduce((s,b)=>s+b,0):null;
  const rc=run.filter(a=>a.cps>0);
  const ac=rc.length?(rc.reduce((s,a)=>s+a.cps,0)/rc.length).toFixed(1):null;
  const lvls=accs.filter(a=>a.level!=null).map(a=>a.level);
  const al=lvls.length?Math.round(lvls.reduce((s,l)=>s+l,0)/lvls.length):null;
  $('k-running').textContent=run.length; $('k-running-sub').textContent=`of ${accs.length}`;
  $('k-clicks').textContent=fmtBig(tc); $('k-cases').textContent=fmtNum(tw);
  $('k-balance').textContent=fmtBig(tb); $('k-cps').textContent=ac||'—'; $('k-level').textContent=al||'—';
}

function renderTable(accs){
  accs.sort((a,b)=>a.running!==b.running?a.running?-1:1:a.email.localeCompare(b.email));
  let h='';
  accs.forEach((a,i)=>{
    const sc=statusClass(a.status),prog=Math.min(100,Math.max(0,a.progress||0));
    const bl=a.running?'&#9632; Stop':'&#9654; Start',bc=a.running?'btn sm halt':'btn sm go';
    const err=a.last_err?`<div style="font-size:9px;color:var(--red);margin-top:1px;max-width:140px;overflow:hidden;text-overflow:ellipsis">${esc(a.last_err.slice(0,50))}</div>`:'';
    const lc=a.last_case?`<span style="font-size:10px;font-family:monospace">${esc(a.last_case)}</span>`+(a.last_case_name?`<div style="font-size:9px;color:var(--text2)">${esc(a.last_case_name)}</div>`:''): '<span class="dim">—</span>';
    h+=`<tr>
      <td class="dim num" style="font-size:10px">${i+1}</td>
      <td><div class="acct"><span class="name">${esc(a.username||a.email.split('@')[0])}</span><span class="mail">${esc(a.email)}</span></div></td>
      <td><div class="badge bs-${esc(sc)}"><span class="bd"></span>${esc(capFirst(a.status||'stopped'))}</div>${err}</td>
      <td class="y num">${fmtBig(a.balance)}</td>
      <td class="dim num">${a.level!=null?a.level:'—'}</td>
      <td class="p num">${fmtNum(a.cases_won)}</td>
      <td><div class="prog-wrap"><div class="prog-track"><div class="prog-fill" style="width:${prog.toFixed(1)}%"></div></div><span class="prog-pct">${prog.toFixed(1)}%</span></div></td>
      <td class="c num">${a.cps>0?a.cps:'—'}</td>
      <td class="num">${fmtBig(a.clicks)}</td>
      <td>${lc}</td>
      <td class="dim">${fmtUptime(a.uptime_secs)}</td>
      <td><button class="${bc}" onclick="toggleAccount('${esc(a.email)}',${a.running})">${bl}</button></td>
    </tr>`;
  });
  $('tbody').innerHTML=h;
}

function renderLog(evts){if(!evts||!evts.length)return;let h='';evts.forEach(e=>{h+=`<div class="log-row ${esc(e.level||'info')}"><div class="log-header"><span class="log-ts">${esc(e.ts)}</span><span class="log-em">${esc(e.email)}</span></div><div class="log-msg">${esc(e.msg)}</div></div>`;});$('log-scroll').innerHTML=h;}

async function refresh(){try{const[sr,lr]=await Promise.all([fetch('/api/stats'),fetch('/api/log')]);const accs=await sr.json(),evts=await lr.json();if(Array.isArray(accs)&&accs.length){renderSummary(accs);renderTable(accs);}if(Array.isArray(evts))renderLog(evts);$('refresh-txt').textContent='Updated '+new Date().toLocaleTimeString();}catch(e){console.error(e);}}

// Setup modal
function openSetup(){$('setup-modal').classList.add('open');}
function closeSetup(){if(!$('setup-start-btn').disabled)$('setup-modal').classList.remove('open');}

let setupPolling=null;
async function startSetup(){
  const btn=$('setup-start-btn');
  btn.disabled=true; btn.textContent='Running…';
  $('setup-log').textContent='Starting login browser…\n';
  const res=await post('/api/setup/start');
  if(res.error){$('setup-log').textContent+='ERROR: '+res.error;btn.disabled=false;btn.textContent='▶ Start Login';return;}
  setupPolling=setInterval(pollSetup,1000);
}

async function pollSetup(){
  try{
    const r=await fetch('/api/setup/status');
    const d=await r.json();
    $('setup-log').textContent=d.log.join('\n')||'Working…';
    $('setup-log').scrollTop=$('setup-log').scrollHeight;
    const pct=d.total>0?Math.round(d.done/d.total*100):0;
    $('setup-progress-fill').style.width=pct+'%';
    if(!d.running){
      clearInterval(setupPolling);
      $('setup-start-btn').disabled=false;
      $('setup-start-btn').textContent='▶ Start Login';
      showToast('Login setup complete!');
      refresh();
    }
  }catch(e){}
}

refresh();
setInterval(refresh,2000);

// Skillmap
let skillmapPresets=[];
let selectedPreset=null;
let skillmapPolling=null;

async function openSkillmap(){
  $('skillmap-modal').classList.add('open');
  // populate account list
  const sel=$('skillmap-account-select');
  sel.innerHTML='';
  try{
    const r=await fetch('/api/stats');const accs=await r.json();
    accs.filter(a=>a).forEach(a=>{
      const opt=document.createElement('option');
      opt.value=a.email;opt.textContent=(a.username||a.email.split('@')[0])+' — '+a.email;
      sel.appendChild(opt);
    });
  }catch(e){}
  // load presets
  try{
    const r=await fetch('/api/skillmap/presets');
    skillmapPresets=await r.json();
    renderPresets();
  }catch(e){}
}

function renderPresets(){
  const cont=$('presets-list');
  cont.innerHTML='';
  skillmapPresets.forEach(p=>{
    const div=document.createElement('div');
    div.className='preset-row';
    div.style.cursor='pointer';
    div.innerHTML=`<div class="preset-info"><div class="pname">${esc(p.name)}</div><div class="pdesc">${esc(p.description)}</div><div class="pbadge">${p.skillCount} skills</div></div><span id="pcheck-${esc(p.id)}" style="font-size:18px;color:var(--purple)">&#9675;</span>`;
    div.addEventListener('click',()=>{
      selectedPreset=p.id;
      document.querySelectorAll('[id^="pcheck-"]').forEach(el=>el.innerHTML='&#9675;');
      $('pcheck-'+p.id).innerHTML='&#9679;';
    });
    cont.appendChild(div);
  });
}

function closeSkillmap(){
  if($('skillmap-apply-btn').disabled)return;
  $('skillmap-modal').classList.remove('open');
  selectedPreset=null;
  if(skillmapPolling){clearInterval(skillmapPolling);skillmapPolling=null;}
}

async function applySkillmap(){
  const email=$('skillmap-account-select').value;
  if(!email){showToast('Select an account first.');return;}
  if(!selectedPreset){showToast('Select a preset first.');return;}
  if(!confirm(`Apply '${selectedPreset}' to ${email}?\nThis will sell ALL current skills first.`))return;

  $('skillmap-apply-btn').disabled=true;
  $('skillmap-status').textContent='Starting\u2026';

  const res=await post('/api/skillmap/apply',{email,preset:selectedPreset});
  if(res.error){
    $('skillmap-status').textContent='ERROR: '+res.error;
    $('skillmap-apply-btn').disabled=false;
    return;
  }
  $('skillmap-status').textContent='Working\u2026 (check the event log for progress)';
  // Poll log for completion
  let pollCount=0;
  skillmapPolling=setInterval(async()=>{
    pollCount++;
    try{
      const lr=await fetch('/api/log');const evts=await lr.json();
      const recent=evts.find(e=>e.email===email&&(e.msg.includes('Skillmap applied')||e.msg.includes('Skillmap:')));
      if(recent&&recent.msg.includes('applied')){
        clearInterval(skillmapPolling);skillmapPolling=null;
        $('skillmap-status').textContent='\u2713 '+recent.msg;
        $('skillmap-apply-btn').disabled=false;
        showToast('\u2713 Skillmap applied!');
      } else if(pollCount>300){
        clearInterval(skillmapPolling);skillmapPolling=null;
        $('skillmap-status').textContent='Timed out. Check event log.';
        $('skillmap-apply-btn').disabled=false;
      }
    }catch(e){}
  },1000);
}
</script>
</body>
</html>"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/stats")
def api_stats():
    sessions_data = load_sessions()
    with stats_lock:
        for email in sessions_data:
            if email not in stats:
                stats[email] = default_stat(email)
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
        return jsonify({"error": "No sessions — run Login Setup first."}), 400

    targets = ([email] if email and email in sessions_data
               else list(sessions_data.keys()))

    started = []
    for em in targets:
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
        time.sleep(0.05)

    return jsonify({"ok": True, "started": started})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    targets = [email] if email else list(stop_flags.keys())
    for em in targets:
        if em in stop_flags:
            stop_flags[em].set()
    return jsonify({"ok": True})


@app.route("/api/remove", methods=["POST"])
def api_remove():
    """Stop all bots and wipe sessions.json + in-memory stats."""
    for ev in stop_flags.values():
        ev.set()
    # give threads a moment to notice the stop signal
    time.sleep(0.5)
    stop_flags.clear()
    threads.clear()
    with stats_lock:
        stats.clear()
    try:
        save_sessions({})
    except Exception:
        pass
    log("system", "All bots removed and sessions cleared", "warn")
    return jsonify({"ok": True})


@app.route("/api/setup/start", methods=["POST"])
def api_setup_start():
    if setup_status["running"]:
        return jsonify({"error": "Setup already running."}), 400
    accounts = load_accounts()
    if not accounts:
        return jsonify({"error": "accounts.json not found or empty."}), 400

    def run():
        saved, failed = run_login_setup(accounts)
        if saved:
            existing = load_sessions()
            existing.update(saved)
            save_sessions(existing)
            log("system", f"Login setup complete — {len(saved)} accounts saved", "success")
            # seed stats for new accounts
            with stats_lock:
                for em in saved:
                    if em not in stats:
                        stats[em] = default_stat(em)
        if failed:
            log("system", f"Login setup — {len(failed)} failed: {', '.join(failed)}", "error")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/setup/status")
def api_setup_status():
    with setup_lock:
        return jsonify(dict(setup_status))


@app.route("/api/skillmap/presets")
def api_skillmap_presets():
    return jsonify([
        {"id": k, "name": v["name"], "description": v["description"],
         "skillCount": len(set(v["skillIds"]))}
        for k, v in SKILL_PRESETS.items()
    ])


# Track ongoing skillmap jobs: email -> {"running": bool, "log": [...]}
skillmap_jobs: dict = {}
skillmap_jobs_lock = threading.Lock()


@app.route("/api/skillmap/apply", methods=["POST"])
def api_skillmap_apply():
    data = request.get_json(silent=True) or {}
    email     = data.get("email", "")
    preset_id = data.get("preset", "")
    sessions_data = load_sessions()

    if not email or email not in sessions_data:
        return jsonify({"error": "Account not found in sessions."}), 400
    if preset_id not in SKILL_PRESETS:
        return jsonify({"error": f"Unknown preset: {preset_id}"}), 400

    with skillmap_jobs_lock:
        if skillmap_jobs.get(email, {}).get("running"):
            return jsonify({"error": "Skillmap already running for this account."}), 400
        skillmap_jobs[email] = {"running": True, "log": [], "result": None}

    def run():
        result = apply_skillmap(make_session(sessions_data[email]), email, preset_id)
        with skillmap_jobs_lock:
            skillmap_jobs[email]["running"] = False
            skillmap_jobs[email]["result"]  = result

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    sessions_data = load_sessions()

    print("=" * 54)
    print("  CaseBot  |  Web Dashboard")
    print("  http://localhost:5000")
    print("=" * 54)

    if not sessions_data:
        accounts = load_accounts()
        if accounts:
            print(f"\n[*] Found {len(accounts)} account(s) in accounts.json")
            print("[*] No sessions yet — open http://localhost:5000 and click Login Setup")
        else:
            print("\n[!] accounts.json not found. Add your accounts first.")
    else:
        print(f"[*] Loaded {len(sessions_data)} session(s)")
        for em in sessions_data:
            stats[em] = default_stat(em)

    print("\n[*] Starting web server … open http://localhost:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
