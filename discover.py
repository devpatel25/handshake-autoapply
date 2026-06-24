"""Phase 1 (Handshake): discover jobs via the logged-in browser's JobSearchQuery.

Loads the saved-search URL in an authed context (injected storage_state), scrolls
to trigger the SPA's `JobSearchQuery` GraphQL calls, and intercepts their
responses (`data.jobSearch.edges[].node.job`). No injected fetches — we only read
what the real UI generates. Output: data/jobs_YYYY-MM-DD.json (new only) +
data/seen.json (dedupe). Aborts on a bot challenge.

Usage: python3 discover.py [--once-scrolls N]
"""

import argparse
import json
import random
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

import browser

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
CFG = json.loads((ROOT / "config.json").read_text())["discover"]

state = {"edges": {}, "total": None, "challenge": None, "responses": 0}


def on_response(resp):
    url = resp.url
    if "captcha-delivery.com" in url or "challenges.cloudflare.com" in url:
        state["challenge"] = "captcha"
        return
    if "/hs/graphql" not in url:
        return
    if resp.status == 403:
        state["challenge"] = "403"
        return
    try:
        payload = json.loads(resp.text())
    except Exception:
        return
    js = (payload.get("data") or {}).get("jobSearch")
    if not js:
        return
    state["responses"] += 1
    if js.get("totalCount") is not None:
        state["total"] = js["totalCount"]
    for edge in js.get("edges", []):
        job = (edge.get("node") or {}).get("job") or {}
        jid = str(job.get("id") or "")
        if jid and jid not in state["edges"]:
            state["edges"][jid] = job


def _loc_names(job):
    out = []
    for l in (job.get("locations") or []):
        if isinstance(l, dict):
            out.append(l.get("name") or l.get("city") or "")
        elif isinstance(l, str):
            out.append(l)
    return [x for x in out if x]


def extract(job):
    jid = str(job.get("id"))
    emp = job.get("employer") or {}
    return {
        "job_id": jid,
        "url": f"{browser.BASE_URL}/jobs/{jid}",
        "title": job.get("title"),
        "company": emp.get("name"),
        "type": (job.get("jobType") or {}).get("name"),
        "employmentType": (job.get("employmentType") or {}).get("name"),
        "locations": _loc_names(job),
        "remote": job.get("remote"), "hybrid": job.get("hybrid"), "onSite": job.get("onSite"),
        "workLocationType": job.get("workLocationType"),
        "description": job.get("description"),
        "studentScreen": job.get("studentScreen") or {},
        "remunerations": job.get("remunerations") or [],
        "salaryType": (job.get("salaryType") or {}).get("name"),
        "createdAt": job.get("createdAt"),
        "applyStart": job.get("applyStart"),
        "expirationDate": job.get("expirationDate"),
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
    }


def main():
    DATA.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    url = CFG.get("saved_search_url") or browser.JOB_SEARCH_URL
    lo, hi = CFG.get("scroll_pause_range", [2.0, 4.0])
    deadline = time.time() + CFG.get("max_minutes", 5) * 60
    max_jobs = CFG.get("max_jobs", 150)

    with sync_playwright() as p:
        br, ctx = browser.launch_authed(p)
        page = ctx.new_page()
        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(random.uniform(6, 9))
        # Wait for page 1 results to intercept.
        for _ in range(15):
            if state["edges"]:
                break
            time.sleep(1)
        # Handshake paginates with a "next page" button (NOT infinite scroll), so
        # click through pages, intercepting each page's JobSearchQuery, up to the cap.
        while time.time() < deadline and not state["challenge"]:
            n = len(state["edges"])
            if (state["total"] and n >= state["total"]) or n >= max_jobs:
                break
            nxt = (page.query_selector('button[aria-label="next page"]')
                   or page.query_selector('[aria-label="next page" i]'))
            if not nxt or not nxt.is_visible() or not nxt.is_enabled():
                break
            before = n
            try:
                nxt.scroll_into_view_if_needed(timeout=3000)
                nxt.click()
            except Exception:
                break
            browser.human_pause(lo, hi)
            for _ in range(10):   # wait for the next page's results to load
                if len(state["edges"]) > before:
                    break
                time.sleep(0.7)
            if len(state["edges"]) == before:
                break  # clicking Next produced no new jobs -> end of results
        br.close()

    if state["challenge"]:
        (LOGS / "discover_challenge.flag").write_text(datetime.now(timezone.utc).isoformat())
        print(f"ABORT: bot challenge ({state['challenge']}). No output written.")
        sys.exit(2)

    jobs = {jid: extract(j) for jid, j in state["edges"].items()}
    new = {jid: j for jid, j in jobs.items() if jid not in seen}
    today = date.today().isoformat()
    out = DATA / f"jobs_{today}.json"
    existing = json.loads(out.read_text()) if out.exists() else {}
    existing.update(new)
    out.write_text(json.dumps(existing, indent=1))
    for jid, j in new.items():
        seen[jid] = {"first_seen": today, "title": j["title"], "company": j["company"]}
    seen_path.write_text(json.dumps(seen, indent=1))

    interns = sum(1 for j in jobs.values()
                  if (j.get("type") or "").lower() in ("internship", "co-op", "cooperative education"))
    print(f"responses={state['responses']} total={state['total']} jobs_seen={len(jobs)} "
          f"(intern/co-op={interns}) new={len(new)} -> {out.name} (today total {len(existing)})")


if __name__ == "__main__":
    main()
