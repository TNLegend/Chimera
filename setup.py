"""
Converts a Cookie-Editor JSON export into auth_state.json for the proxy.

Steps:
  1. Install "Cookie-Editor" extension in Chrome
  2. Go to gemini.google.com
  3. Click Cookie-Editor → Export → "Export as JSON" (copies to clipboard)
  4. Paste clipboard into a file called:  cookies_raw.json
  5. Run:  python setup_auth.py
"""
import json
import sys
import os

RAW_FILE       = "cookies_raw.json"
AUTH_STATE_FILE = "auth_state.json"

TARGET_COOKIES = {
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-1PSIDCC",
    "SAPISID", "APISID", "SSID", "SID", "HSID",
    "NID", "SIDCC",
}

def main():
    if not os.path.exists(RAW_FILE):
        print(f"[-] {RAW_FILE} not found.")
        print("    1. Install 'Cookie-Editor' extension in Chrome")
        print("    2. Go to gemini.google.com")
        print("    3. Click Cookie-Editor → Export → 'Export as JSON'")
        print(f"    4. Paste the clipboard content into a file named: {RAW_FILE}")
        print("    5. Re-run this script")
        sys.exit(1)

    with open(RAW_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    cookies_out = []
    for c in raw:
        name = c.get("name", "")
        value = c.get("value", "")
        domain = c.get("domain", ".google.com")
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")

        cookies_out.append({
            "name":     name,
            "value":    value,
            "domain":   domain,
            "path":     c.get("path", "/"),
            "secure":   c.get("secure", True),
            "httpOnly": c.get("httpOnly", True),
            "sameSite": c.get("sameSite", "None"),
        })

    key_cookies = [c for c in cookies_out if c["name"] in TARGET_COOKIES]
    if not key_cookies:
        print(f"[-] None of the expected cookies found in {RAW_FILE}.")
        print("    Make sure you exported from gemini.google.com while logged in.")
        sys.exit(1)

    state = {"cookies": cookies_out, "origins": []}
    with open(AUTH_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    found = [c["name"] for c in key_cookies]
    print(f"[+] Converted {len(cookies_out)} cookies ({len(key_cookies)} key cookies: {found})")
    print(f"[+] Saved to {AUTH_STATE_FILE}")
    print("[+] Run chimera_proxy.py — cookies will auto-refresh via Playwright every 30 min.")

if __name__ == "__main__":
    main()