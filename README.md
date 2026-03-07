# case-clicker

> ⚠️ **Still under development — likely to fail or break at any time. Use at your own risk.**

An automated farming bot for [case-clicker.com](https://case-clicker.com) with a local web dashboard.

## Features

- Multi-account support (unlimited accounts via `accounts.json`)
- WebSocket-based clicking loop (matches the real browser behaviour)
- Automatic Turnstile captcha solving via a real Chromium window (Patchright)
- Skill map presets — sell all skills and rebuy an optimised build in one click
- Flask web dashboard at `http://localhost:5000`

## Requirements

- Python 3.11+
- Internet connection

All Python packages are auto-installed on first run.

## Setup

1. Clone the repo
2. Add your accounts to `accounts.json`:
   ```json
   [
     { "email": "you@example.com", "password": "yourpassword" },
     { "email": "alt@example.com", "password": "altpassword" }
   ]
   ```
3. Run:
   ```bash
   python main.py
   ```
4. Open `http://localhost:5000` in your browser
5. Click **Login Setup** to log in all accounts (a Chromium window opens per account)
6. Once sessions are saved, click **Start All** to begin farming

## Notes

- Sessions are stored in `sessions.json` — re-run Login Setup if they expire
- The bot is **under active development** and may stop working without notice if the site updates its API or WebSocket protocol

## License

MIT — see [LICENSE](LICENSE)
