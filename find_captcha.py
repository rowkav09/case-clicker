import sys; sys.stdout.reconfigure(encoding='utf-8')
from curl_cffi import requests as cffi_requests
import re

s = cffi_requests.Session(impersonate='chrome131')
r = s.get('https://case-clicker.com/auth/login', timeout=20)
html = r.text

for kw in ['captcha','hcaptcha','recaptcha','turnstile','sitekey','0x4','challenges']:
    hits = [l.strip()[:200] for l in html.split('\n') if kw.lower() in l.lower()]
    if hits:
        print(f'{kw}: {hits[:3]}')

scripts = re.findall(r'src="([^"]+)"', html)
print('scripts:', scripts[:20])
