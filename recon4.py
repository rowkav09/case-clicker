"""
Phase 4 – deep dive into the login chunk and larger app chunks.
Fetch each directly and pretty-print the auth/socket relevant lines.
"""
import re, sys
from curl_cffi import requests

sys.stdout.reconfigure(encoding='utf-8')

BASE = "https://case-clicker.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": BASE + "/",
}

session = requests.Session(impersonate="chrome131")
session.get(BASE + "/", headers=HEADERS, timeout=20)

# ── Target chunks ─────────────────────────────────────────────────────────────
TARGETS = [
    "/_next/static/chunks/pages/auth/login-042ea928dc0b403d.js",
    "/_next/static/chunks/1922-f4a9b05dc8cfcd21.js",
    "/_next/static/chunks/7598-b0202a743ad9811f.js",
    "/_next/static/chunks/9755-0bd50713e338be77.js",
    "/_next/static/chunks/6209-e8ba545c9bb477b3.js",
    "/_next/static/chunks/6376-5a5d2b46eed801a3.js",
    "/_next/static/chunks/pages/_app-cfb133e40287f8b8.js",
]

# Also try to get the home page chunk ("/")
for target in TARGETS:
    url = BASE + target
    r = session.get(url, headers=HEADERS, timeout=20)
    js = r.text
    name = target.split("/")[-1]
    print(f"\n{'='*70}")
    print(f"CHUNK: {name}  ({len(js):,} bytes)")
    print('='*70)

    # 1. WS / socket URLs
    ws_urls = re.findall(r'wss?://[^\s"\'`\\)]+', js)
    if ws_urls:
        print(f"  WS URLs: {list(set(ws_urls))}")

    # 2. socket.io / io( calls
    io_calls = re.findall(r'io\(["\`]([^"\'`]+)["\`]', js)
    if io_calls:
        print(f"  io() calls: {io_calls}")

    # 3. socket.io options object patterns  e.g. {path:"/socket.io"}
    paths = re.findall(r'path\s*:["\s]*["\`](/[^"\'` ]+)["\`]', js)
    if paths:
        print(f"  path options: {paths}")

    # 4. emit events
    emits = re.findall(r'emit\(["\`]([^"\'`]+)["\`]', js)
    if emits:
        print(f"  emit events: {list(set(emits))}")

    # 5. socket on events
    ons = re.findall(r'\.on\(["\`]([^"\'`]+)["\`]', js)
    if ons:
        print(f"  on events: {list(set(ons))}")

    # 6. /api/ paths
    api = re.findall(r'["\`](/api/[a-zA-Z0-9/_\-]+)["\`]', js)
    if api:
        print(f"  /api/ paths: {list(set(api))}")

    # 7. fetch / axios patterns
    fetches = re.findall(r'(?:fetch|axios)\(["\`]([^"\'`]+)["\`]', js)
    if fetches:
        print(f"  fetch/axios: {fetches}")

    # 8. Print every line that mentions login/auth/socket/ws (max 20 lines)
    interesting = []
    for line in js.split(';'):
        line = line.strip()
        if re.search(r'login|signin|password|socket|emit|\.on\(|auth|token|session|click|clickValue', line, re.I):
            if 20 < len(line) < 400:
                interesting.append(line)
    if interesting:
        print(f"\n  Interesting snippets ({len(interesting)} total, showing first 25):")
        for s in interesting[:25]:
            print(f"    >> {s[:300]}")

print("\n[done]")
