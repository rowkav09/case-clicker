import sys; sys.stdout.reconfigure(encoding='utf-8')
from curl_cffi import requests as cffi_requests
import re

s = cffi_requests.Session(impersonate='chrome131')

# Search app chunk for the Turnstile sitekey (starts with 0x4)
chunks = [
    "/_next/static/chunks/pages/_app-cfb133e40287f8b8.js",
    "/_next/static/chunks/6376-5a5d2b46eed801a3.js",
    "/_next/static/chunks/pages/auth/login-042ea928dc0b403d.js",
]

for c in chunks:
    r = s.get("https://case-clicker.com" + c, timeout=20)
    js = r.text
    keys = re.findall(r'0x4[A-Za-z0-9_-]{20,}', js)
    if keys:
        print(f"{c}: {list(set(keys))}")
    # also look for siteKey / site_key string
    sk = re.findall(r'sitekey["\s:=]+["\']([^"\']+)["\']', js, re.I)
    if sk:
        print(f"  sitekey: {sk}")
    ts = re.findall(r'turnstile[^;]{0,200}', js, re.I)
    if ts:
        print(f"  turnstile refs: {ts[:3]}")
