"""
Case Clicker recon script.
Probes all likely API / WebSocket endpoints to discover the protocol.
Run:  python recon.py
"""

import json, time
from curl_cffi import requests

BASE = "https://case-clicker.com"

# Headers that mimic a real logged-in Chrome browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": BASE,
    "Referer": BASE + "/",
}

session = requests.Session(impersonate="chrome131")

# ── 1. Hit the main page to get Cloudflare clearance cookies ─────────────────
print("[*] Fetching main page to seed Cloudflare cookies...")
r = session.get(BASE + "/", headers=HEADERS, timeout=30)
print(f"    status={r.status_code}  cookies={dict(session.cookies)}")

# ── 2. Probe likely login endpoints ──────────────────────────────────────────
LOGIN_PATHS = [
    "/api/login",
    "/api/auth/login",
    "/api/user/login",
    "/api/v1/login",
    "/api/v1/auth/login",
    "/auth/login",
    "/login",
]

print("\n[*] Probing login endpoints (POST)...")
for path in LOGIN_PATHS:
    try:
        r = session.post(
            BASE + path,
            json={"username": "_probe_", "password": "_probe_"},
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=10,
        )
        print(f"    {path:35s}  status={r.status_code}  body_preview={r.text[:120]!r}")
    except Exception as e:
        print(f"    {path:35s}  ERROR: {e}")

# ── 3. Probe Socket.IO / WebSocket polling endpoints to find the WS server ───
# Socket.IO handshake starts over HTTP polling before upgrading to WS
SOCKET_PATHS = [
    "/socket.io/?EIO=4&transport=polling",
    "/api/socket.io/?EIO=4&transport=polling",
    "/ws",
    "/api/ws",
]

print("\n[*] Probing Socket.IO / WebSocket HTTP-upgrade endpoints (GET)...")
for path in SOCKET_PATHS:
    try:
        r = session.get(
            BASE + path,
            headers=HEADERS,
            timeout=10,
        )
        print(f"    {path:50s}  status={r.status_code}  body_preview={r.text[:200]!r}")
    except Exception as e:
        print(f"    {path:50s}  ERROR: {e}")

# ── 4. Try alternate domains ──────────────────────────────────────────────────
ALT_BASES = [
    "https://api.case-clicker.com",
    "https://socket.case-clicker.com",
    "https://ws.case-clicker.com",
]
print("\n[*] Probing alternate sub-domains...")
for base in ALT_BASES:
    try:
        r = session.get(base + "/", headers=HEADERS, timeout=8)
        print(f"    {base:45s}  status={r.status_code}  body_preview={r.text[:120]!r}")
    except Exception as e:
        print(f"    {base:45s}  ERROR: {e}")

print("\n[done]")
