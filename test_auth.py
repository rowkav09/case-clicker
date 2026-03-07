"""Quick test: login 1 account and try one case click."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from curl_cffi import requests as cffi_requests

BASE  = "https://case-clicker.com"
EMAIL = "rkav007@icloud.com"
PASS  = "LemonT77"

session = cffi_requests.Session(impersonate="chrome131")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": BASE,
    "Referer": BASE + "/auth/login",
}

print("[1] Seeding CF cookies...")
session.get(BASE + "/", headers=HEADERS, timeout=30)
print(f"    cookies: {dict(session.cookies)}")

print("[2] Logging in...")
r = session.post(
    BASE + "/api/auth/sign-in/email",
    json={"email": EMAIL, "password": PASS, "callbackURL": "/"},
    headers={**HEADERS, "Content-Type": "application/json", "x-captcha-response": ""},
    timeout=20,
)
print(f"    status={r.status_code}")
print(f"    body={r.text[:400]}")
print(f"    cookies after login: {dict(session.cookies)}")

if r.status_code in (200, 201):
    print("[3] Sending 5 case clicks...")
    r2 = session.post(
        BASE + "/api/caseClick",
        json={"clicks": 5},
        headers={**HEADERS, "Content-Type": "application/json"},
        timeout=15,
    )
    print(f"    status={r2.status_code}  body={r2.text[:300]}")
else:
    print("[!] Login failed, skipping click test")
