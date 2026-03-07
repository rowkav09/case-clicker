"""
login_setup.py  –  one-time login for all accounts
====================================================
Opens ONE real browser window, logs into every account in accounts.json,
and saves the session cookies to sessions.json.

Run this ONCE (or whenever your sessions expire):
    python login_setup.py

Then run the bot (pure HTTP, no browser):
    python bot.py
"""

import json, time, sys, os
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "accounts.json")
SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")
BASE          = "https://case-clicker.com"

def main():
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        accounts = json.load(f)

    print(f"[*] Logging into {len(accounts)} accounts via browser (one-time setup).")
    print(f"[*] A browser window will open — do NOT close it until done.\n")

    saved = {}
    failed = []

    with sync_playwright() as pw:
        # Launch a real Chromium window (not headless — needed for Turnstile)
        browser = pw.chromium.launch(
            headless=False,
            args=["--window-size=900,700"],
        )
        context = browser.new_context(
            viewport={"width": 900, "height": 700},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        for i, acc in enumerate(accounts):
            email    = acc["email"]
            password = acc["password"]
            tag      = f"[{i+1}/{len(accounts)}] {email}"

            print(f"{tag}  …  logging in")

            try:
                # Navigate to login page
                page.goto(f"{BASE}/auth/login", wait_until="domcontentloaded", timeout=30000)

                # Fill email
                page.fill('input[type="email"], input[name="email"], input[placeholder*="mail" i]',
                          email, timeout=10000)

                # Fill password
                page.fill('input[type="password"]', password, timeout=10000)

                # Wait a moment for Turnstile widget to become interactive
                time.sleep(2)

                # Click the submit / sign-in button
                page.click(
                    'button[type="submit"], button:has-text("Sign"), button:has-text("Login"), button:has-text("Log in")',
                    timeout=10000,
                )

                # Wait for redirect away from any auth page (up to 45 s for Turnstile)
                try:
                    page.wait_for_url(
                        lambda url: "/auth/" not in url and "login" not in url,
                        timeout=45000,
                    )
                except PWTimeout:
                    # Maybe already on home, or the URL pattern differs — check anyway
                    pass

                # Grab cookies from the context
                cookies = context.cookies()
                if not cookies:
                    raise RuntimeError("No cookies after login")

                # Check we actually got a session (not just CF cookies)
                session_cookies = [
                    c for c in cookies
                    if any(k in c["name"].lower() for k in ("session", "token", "auth"))
                ]
                if not session_cookies:
                    # Try waiting a bit more
                    time.sleep(3)
                    cookies = context.cookies()
                    session_cookies = [
                        c for c in cookies
                        if any(k in c["name"].lower() for k in ("session", "token", "auth"))
                    ]

                saved[email] = {
                    "password": password,
                    "cookies":  [
                        {k: v for k, v in c.items()
                         if k in ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite")}
                        for c in cookies
                    ],
                }
                print(f"  ✓  saved {len(cookies)} cookies ({len(session_cookies)} session cookies)")

                # Clear cookies for the next account
                context.clear_cookies()
                time.sleep(1)

            except Exception as exc:
                print(f"  ✗  FAILED: {exc}")
                failed.append(email)
                context.clear_cookies()
                # Navigate away to reset any stuck Turnstile / form state
                try:
                    page.goto("about:blank", timeout=5000)
                except Exception:
                    pass
                time.sleep(1)

        browser.close()

    # Save sessions
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(saved, f, indent=2)

    print(f"\n[*] Done.  {len(saved)} sessions saved to sessions.json")
    if failed:
        print(f"[!] Failed accounts ({len(failed)}):")
        for e in failed:
            print(f"    {e}")
    print("\nNow run:  python bot.py")


if __name__ == "__main__":
    main()
