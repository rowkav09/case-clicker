"""
Phase 3 – mine all JS chunks for auth endpoints, socket events, and click logic.
"""
import re, json, sys
from curl_cffi import requests

sys.stdout.reconfigure(encoding='utf-8')

BASE    = "https://case-clicker.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

session = requests.Session(impersonate="chrome131")
session.get(BASE + "/", headers=HEADERS, timeout=20)

# ── 1. Collect ALL JS chunk URLs from the login page ────────────────────────
r_login = session.get(BASE + "/auth/login", headers=HEADERS, timeout=15)
html = r_login.text

# grab all /_next/static JS paths mentioned in the HTML
chunks = list(dict.fromkeys(re.findall(r'/_next/static/[^"\'> ]+\.js', html)))
print(f"[*] Found {len(chunks)} chunks in HTML: {chunks}\n")

# also try to get the build manifest to find all chunks
manifest_r = session.get(BASE + "/_next/static/chunks/pages/_buildManifest.js", headers=HEADERS, timeout=10)
more_chunks = re.findall(r'/_next/static/[^"\']+\.js', manifest_r.text)
all_chunks = list(dict.fromkeys(chunks + more_chunks))
print(f"[*] Total after manifest: {len(all_chunks)} chunks\n")

# ── 2. Pull and analyse each chunk ───────────────────────────────────────────
INTERESTING_KEYS = [
    "socket", "Socket", "SOCKET",
    "websocket", "WebSocket",
    "/api/", "emit(", ".on(",
    "login", "signin", "auth", "token", "session",
    "click", "Click", "CLICK",
    "case", "upgrade",
    "password", "username", "credentials",
]

findings = {}

for chunk_path in all_chunks:
    url = BASE + chunk_path
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        js = r.text
    except Exception as e:
        print(f"  ERROR fetching {chunk_path}: {e}")
        continue

    hits = {}

    # WS URLs
    ws_urls = re.findall(r'wss?://[^\s"\'`\\)]+', js)
    if ws_urls:
        hits["ws_urls"] = list(set(ws_urls))

    # /api/ paths
    api_paths = re.findall(r'["\`](/api/[a-zA-Z0-9/_\-]+)', js)
    if api_paths:
        hits["api_paths"] = list(set(api_paths))

    # socket.io emit events  e.g. socket.emit("click", ...) or emit("case_click"
    emits = re.findall(r'emit\(["\`]([^"\'`]+)["\`]', js)
    if emits:
        hits["emit_events"] = list(set(emits))

    # socket.on events
    ons = re.findall(r'\.on\(["\`]([^"\'`]+)["\`]', js)
    if ons:
        hits["on_events"] = list(set(ons))

    # fetch() / axios calls
    fetches = re.findall(r'fetch\(["\`]([^"\'`]+)["\`]', js)
    if fetches:
        hits["fetch_calls"] = list(set(fetches))

    # any line with "login" or "auth" and a URL
    auth_lines = [line.strip() for line in js.split('\n')
                  if re.search(r'login|auth|token|session|credentials', line, re.I)
                  and re.search(r'/api/|fetch|axios|socket', line, re.I)]
    if auth_lines:
        hits["auth_lines"] = auth_lines[:10]

    if hits:
        name = chunk_path.split("/")[-1]
        findings[name] = hits
        print(f"[!] {name}  ({len(js)} bytes)")
        for k, v in hits.items():
            print(f"      {k}: {v}")
        print()

print(f"\n[done] {len(findings)} interesting chunks found")
