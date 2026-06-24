"""Capture / refresh the Handshake auth session (one-time bootstrap + re-login).

Microsoft Entra blocks Playwright-driven login, so we log in via a NORMAL Chrome
(the app binary) with a remote-debugging port, then attach over CDP and save the
authenticated session as storage_state (data/handshake_state.json) for the
automation to inject. This is the only step that needs a human (DUO).

  python3 capture_session.py              # launch Chrome, wait for you to log in, then capture
  python3 capture_session.py --capture-only   # capture from an already-open debug Chrome (port 9222)

Exit 0 = captured a logged-in session; 1 = captured but no auth cookie; 2 = could not attach.
"""

import argparse
import os
import subprocess
import sys

from playwright.sync_api import sync_playwright

import browser


def do_capture():
    p = sync_playwright().start()
    try:
        br = browser.connect_cdp(p)
    except Exception as e:
        print(f"ERROR: could not attach to Chrome on port {browser.DEBUG_PORT}: {e}", flush=True)
        os._exit(2)
    ctx = br.contexts[0] if br.contexts else None
    if ctx is None:
        print("ERROR: no browser context on the debug port", flush=True)
        os._exit(2)
    browser.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    st = ctx.storage_state(path=str(browser.STATE_FILE))
    names = [c["name"] for c in st.get("cookies", []) if "joinhandshake" in c.get("domain", "")]
    has_auth = "_trajectory_session" in names
    print(f"saved {browser.STATE_FILE}", flush=True)
    print(f"  {len(st.get('cookies', []))} cookies; _trajectory_session present = {has_auth}", flush=True)
    sys.stdout.flush()
    # Hard-exit so Playwright teardown never closes the (externally launched) Chrome.
    os._exit(0 if has_auth else 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture-only", action="store_true",
                    help="capture from an already-running debug Chrome (skip launching/login)")
    args = ap.parse_args()

    if not args.capture_only:
        if not os.path.exists(browser.CHROME_BIN):
            sys.exit(f"Google Chrome not found at {browser.CHROME_BIN}")
        browser.PROFILE_DIR.mkdir(exist_ok=True)
        print(f"Opening Chrome on the Handshake profile (debug port {browser.DEBUG_PORT}).")
        print("Log in: NEU email -> Continue with email -> Continue to Northeastern SSO -> password ->")
        print("DUO code in Duo Mobile -> click 'Stay signed in? -> Yes', until you SEE job listings.")
        print("Then come back here and press Enter.")
        sys.stdout.flush()
        subprocess.Popen([
            browser.CHROME_BIN,
            f"--user-data-dir={browser.PROFILE_DIR}",
            f"--remote-debugging-port={browser.DEBUG_PORT}",
            "--no-first-run", "--no-default-browser-check",
            browser.JOB_SEARCH_URL,
        ])
        try:
            input("Press Enter once you see job listings... ")
        except EOFError:
            pass
    do_capture()


if __name__ == "__main__":
    main()
