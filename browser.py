"""Canonical browser setup for the Handshake auto-apply automation.

Auth model (validated live — see PLAN.md / memory): Microsoft Entra blocks
Playwright-driven login, so we never drive the SSO. `capture_session.py` logs in
via a NORMAL Chrome (the app binary, `--remote-debugging-port`) and saves the
authenticated session as storage_state (`data/handshake_state.json`). Every
automation module then injects that storage_state via `launch_authed()` — headed
real Chrome (headless trips bot detection), no SSO needed. Handshake's auth
cookie `_trajectory_session` is memory-only, which is why a disk-profile handoff
fails and storage_state capture is the persistence mechanism. Re-capture (via
`capture_session.py`) only when the server-side session expires.
"""

import random
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path.home() / ".handshake_profile"      # used only by the human login Chrome
STATE_FILE = ROOT / "data" / "handshake_state.json"   # captured session the automation injects
DEBUG_PORT = 9222
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Northeastern Handshake entry points.
SCHOOL = "northeastern"
BASE_URL = f"https://{SCHOOL}.joinhandshake.com"
JOB_SEARCH_URL = f"{BASE_URL}/job-search"
GRAPHQL_URL = f"{BASE_URL}/hs/graphql"
LOGIN_HOST = "login.microsoftonline.com"   # NEU Microsoft Entra SSO
ACCESS_PATH = "/access"                    # Handshake unified login wall

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def launch_authed(p, headless=False):
    """Launch headed real Chrome and open a context with the captured session.

    Returns (browser, context). Injects storage_state when present; otherwise an
    unauthenticated context (callers/session_check then see logged_out).
    headless stays False on purpose: headless Chrome trips Handshake's bot detection.
    """
    br = p.chromium.launch(channel="chrome", headless=headless, args=LAUNCH_ARGS)
    if STATE_FILE.exists():
        ctx = br.new_context(storage_state=str(STATE_FILE))
    else:
        ctx = br.new_context()
    return br, ctx


def connect_cdp(p, port=DEBUG_PORT):
    """Attach to an already-running Chrome via its remote-debugging port.

    Used by capture_session.py to read the live session from the human-login
    Chrome. Caller must NOT browser.close() this (it's not ours) — hard-exit
    instead so the user's Chrome stays open.
    """
    return p.chromium.connect_over_cdp(f"http://localhost:{port}")


def launch_persistent(p, headless=False):
    """Persistent-profile context (used only by the Step-0 recon scripts)."""
    PROFILE_DIR.mkdir(exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR), channel="chrome", headless=headless,
        no_viewport=True, args=LAUNCH_ARGS)


# Back-compat alias used by the Step-0 recon scripts.
launch_context = launch_persistent


def make_challenge_listener():
    """Return (state, handler) for bot-challenge detection.

    Attach with page.on("response", handler); check state["hit"] afterward so a
    caller can abort instead of scraping a captcha/Cloudflare wall.
    """
    state = {"hit": False, "detail": None}

    def on_response(resp):
        url = resp.url
        if "captcha-delivery.com" in url or "challenges.cloudflare.com" in url:
            state["hit"] = True
            state["detail"] = f"captcha:{url[:120]}"
        elif "joinhandshake.com" in url and resp.status == 403:
            state["hit"] = True
            state["detail"] = f"403:{url[:120]}"

    return state, on_response


def human_pause(lo=1.5, hi=4.0):
    """Randomized pause to keep interaction human-paced (anti-bot)."""
    time.sleep(random.uniform(lo, hi))


def shot(page, shots_dir, name):
    """Timestamped screenshot into shots_dir; returns the filename."""
    shots_dir = Path(shots_dir)
    shots_dir.mkdir(parents=True, exist_ok=True)
    path = shots_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}.png"
    page.screenshot(path=str(path))
    return path.name
