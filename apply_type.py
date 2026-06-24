"""Classify a Handshake job's apply method from the job-detail DOM.

  in_portal       : an in-app "Apply" button -> the auto-submit target
  external        : "Apply Externally" -> hand off, never auto-submit in Handshake
  already_applied : the page shows Applied / a Withdraw control
  unknown         : none of the above (flag for manual handling)

Returns {job_id, title, type, target, required_docs}. Mirrors the
nuworks_autoapply/apply_type.py contract so the pipeline's eligible()/merge logic
ports unchanged.
"""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import browser

HERE = Path(__file__).resolve().parent


def _visible_labels(page):
    """Lowercased text of only the VISIBLE buttons/links. The apply modal is
    pre-rendered hidden in the DOM (its Submit Application / Withdraw buttons
    exist on every job), so we must filter by visibility, not query the DOM."""
    out = []
    for e in page.query_selector_all("button, a"):
        try:
            if e.is_visible():
                t = (e.inner_text() or "").strip().lower()
                if t:
                    out.append(t)
        except Exception:
            pass
    return out


def classify_page(page):
    low = _visible_labels(page)
    if any("apply externally" in t for t in low):
        return "external", None
    if any(t == "apply" for t in low):           # visible primary "Apply" = in-portal, not yet applied
        return "in_portal", None
    if any("withdraw" in t for t in low) or any(t == "applied" for t in low):
        return "already_applied", None           # authoritative guard still re-checks at apply time
    return "unknown", None


def classify_jobs(jobs):
    """jobs: [{job_id, url, title}] -> [{job_id, title, type, target, required_docs}]."""
    out = []
    with sync_playwright() as p:
        br, ctx = browser.launch_authed(p)
        page = ctx.new_page()
        for j in jobs:
            url = j.get("url") or f"{browser.BASE_URL}/jobs/{j['job_id']}"
            try:
                page.goto(url, wait_until="domcontentloaded")
                time.sleep(4)
                typ, target = classify_page(page)
            except Exception as e:
                typ, target = "error", str(e)[:80]
            out.append({"job_id": str(j.get("job_id")), "title": j.get("title"),
                        "type": typ, "target": target, "required_docs": []})
        br.close()
    return out


def main():
    files = sorted((HERE / "data").glob("shortlist_*.json"))
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    jobs = json.loads(files[-1].read_text())[:n] if files else []
    if not jobs:
        print("No shortlist found — run discover.py + score.py first.")
        return
    for r in classify_jobs(jobs):
        print(f"{r['type']:<16}{(r['title'] or '')[:50]:<50} {r['job_id']}")


if __name__ == "__main__":
    main()
