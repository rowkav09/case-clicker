"""
Phase 2 recon – NextAuth.js endpoints + ws.case-clicker.com + JS bundle mining.
"""
import re, json
from curl_cffi import requests

BASE    = "https://case-clicker.com"
WS_BASE = "https://ws.case-clicker.com"

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

# ── 0. Seed cookies ───────────────────────────────────────────────────────────
session.get(BASE + "/", headers=HEADERS, timeout=20)

# ── 1. NextAuth.js standard routes ───────────────────────────────────────────
NEXTAUTH = [
    "/api/auth/csrf",
    "/api/auth/session",
    "/api/auth/providers",
    "/api/auth/signin",
    "/api/auth/signin/credentials",
    "/api/auth/callback/credentials",
]
print("[*] NextAuth routes:")
for path in NEXTAUTH:
    r = session.get(BASE + path, headers=HEADERS, timeout=10)
    print(f"    GET  {path:45s}  {r.status_code}  {r.text[:150]!r}")

# ── 2. CSRF + try credentials login ──────────────────────────────────────────
print("\n[*] Getting CSRF token...")
csrf_r = session.get(BASE + "/api/auth/csrf", headers=HEADERS, timeout=10)
print(f"    {csrf_r.status_code}  {csrf_r.text[:200]}")
csrf_token = ""
try:
    csrf_token = csrf_r.json().get("csrfToken", "")
    print(f"    csrfToken = {csrf_token!r}")
except Exception:
    pass

if csrf_token:
    print("\n[*] Attempting credentials login with CSRF token...")
    login_r = session.post(
        BASE + "/api/auth/callback/credentials",
        data={
            "csrfToken": csrf_token,
            "username": "_probe_",
            "password": "_probe_",
            "redirect": "false",
            "callbackUrl": BASE,
            "json": "true",
        },
        headers={**HEADERS,
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": BASE + "/auth/login"},
        timeout=15,
    )
    print(f"    status={login_r.status_code}")
    print(f"    headers={dict(login_r.headers)}")
    print(f"    body={login_r.text[:400]}")

# ── 3. Probe ws.case-clicker.com ──────────────────────────────────────────────
print("\n[*] Probing ws.case-clicker.com...")
WS_PATHS = [
    "/",
    "/socket.io/?EIO=4&transport=polling",
    "/socket.io/?EIO=3&transport=polling",
]
for path in WS_PATHS:
    try:
        r = session.get(WS_BASE + path, headers=HEADERS, timeout=8)
        print(f"    GET  {path:50s}  {r.status_code}  {r.text[:200]!r}")
    except Exception as e:
        print(f"    GET  {path:50s}  ERROR: {e}")

# ── 4. Fetch the login page and look for API hints in the HTML ────────────────
print("\n[*] Scraping /auth/login for API clues...")
r = session.get(BASE + "/auth/login", headers=HEADERS, timeout=15)
html = r.text

# look for _next/static JS chunks
chunks = re.findall(r'/_next/static/[^"\']+\.js', html)
print(f"    Found {len(chunks)} JS chunk references")
for c in chunks[:6]:
    print(f"      {c}")

# look for any fetch/axios calls or API paths in html
api_hints = re.findall(r'["\']/(api/[^"\'?]+)', html)
if api_hints:
    print(f"    API hints in HTML: {list(set(api_hints))[:20]}")

# ── 5. Pull a small JS chunk and search for socket/ws/auth strings ────────────
if chunks:
    # prefer a chunk that looks like it contains app logic (larger filenames)
    target = chunks[0]
    print(f"\n[*] Fetching JS chunk: {target}")
    js_r = session.get(BASE + target, headers=HEADERS, timeout=20)
    js = js_r.text
    print(f"    size={len(js)} bytes")

    # look for socket/ws endpoints
    ws_matches = re.findall(r'wss?://[^\s"\'`]+', js)
    if ws_matches:
        print(f"    WS URLs found: {list(set(ws_matches))}")
    else:
        print("    No wss:// URLs found in this chunk")

    # look for api route strings
    api_matches = re.findall(r'["\`]/api/[a-zA-Z0-9/_-]+', js)
    if api_matches:
        print(f"    /api/ paths: {list(set(api_matches))[:30]}")

    # look for socket.io references
    sio = re.findall(r'socket\.io|socketio|SOCKET|websocket|WebSocket', js, re.I)
    print(f"    Socket references: {list(set(sio))[:10]}")

print("\n[done]")
