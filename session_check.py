"""Preflight: does the captured Handshake session still authenticate?

Launches headed Chrome with the captured storage_state and loads the job-search
page, then decides by CONTENT (the login form = logged out; a URL-only check
false-positives because the SPA keeps the /job-search URL while rendering login).

Exit 0 = logged in. Exit 2 = bot challenge. Exit 3 = logged out / expired / no session.
pipeline.py runs this first; on exit 3 it pings Dev to re-run capture_session.py.

Usage:
  python3 session_check.py
"""

import sys
import time

from playwright.sync_api import sync_playwright

import browser

# Substrings that only appear on the logged-out Handshake login wall.
LOGGED_OUT_MARKERS = ("Hello, job seeker", "Enter your Northeastern University email")


def main():
    if not browser.STATE_FILE.exists():
        print("logged_out (no session file — run capture_session.py)")
        sys.exit(3)
    state, on_response = browser.make_challenge_listener()
    p = sync_playwright().start()
    br, ctx = browser.launch_authed(p)
    page = ctx.new_page()
    page.on("response", on_response)
    try:
        page.goto(browser.JOB_SEARCH_URL, wait_until="domcontentloaded")
        time.sleep(7)
        url = page.url
        body = page.inner_text("body")[:5000]
    except Exception as e:
        print(f"error: {e}")
        br.close()
        sys.exit(3)
    br.close()

    if state["hit"]:
        print(f"challenge ({state['detail']})")
        sys.exit(2)
    logged_out = (browser.LOGIN_HOST in url) or any(m in body for m in LOGGED_OUT_MARKERS)
    if logged_out:
        print(f"logged_out (url={url})")
        sys.exit(3)
    print(f"logged_in (url={url})")
    sys.exit(0)


if __name__ == "__main__":
    main()
