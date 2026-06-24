#!/usr/bin/env python3
"""Visibility helpers for the Handshake pipeline (mirrors nuworks/wellfound stats).

1. announce_new_postings(jobs) — after discovery+scoring, Telegram the shortlisted
   postings we haven't announced before (title — company — type/score). Deduped via
   announced_jobs.json so a day's retry slots don't re-spam. On a day with NOTHING
   new it still sends a one-line "📭 0 new postings" notice (once/day) so a quiet day
   is confirmed, never silent. Returns the count of new postings.
2. record_run(jobs, results, marked) — append this run's outcomes to a per-day stats
   file (data/daily/YYYY-MM-DD.json) that the nightly digest reads.

ALL file I/O is best-effort: these never raise into the pipeline.
"""
import json
import html
from pathlib import Path
from datetime import date, datetime

import telegram_bot as tg

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ANNOUNCED = DATA / "announced_jobs.json"      # {job_id: {date,title,company,type}}
DAILY_DIR = DATA / "daily"                    # one YYYY-MM-DD.json per day


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save(p: Path, obj):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2))
    except Exception:
        pass


def today_path(d: date = None) -> Path:
    return DAILY_DIR / f"{(d or date.today()).isoformat()}.json"


def load_today() -> dict:
    return _load(today_path(), {"date": date.today().isoformat(), "runs": [],
                                "scraped_ids": [], "applied": [], "skipped": [],
                                "errors": []})


def _send(msg: str):
    """Best-effort Telegram send — never raises into the pipeline."""
    try:
        if tg.configured():
            tg.send_message(msg)
    except Exception:
        pass


def announce_new_postings(jobs: list[dict]) -> int:
    """Telegram a heads-up about the shortlist, and record the ids for the digest.
    Returns the count of NEW (not-previously-announced) postings.

    Sends EVERY day so silence never looks like a missed run:
      • new postings -> "🆕 N new Handshake postings:" + list
      • nothing new  -> "📭 0 new Handshake postings today ..." (once/day)
    """
    jobs = jobs or []
    announced = _load(ANNOUNCED, {})
    today = date.today().isoformat()

    t = load_today()
    for j in jobs:
        jid = str(j.get("job_id") or "")
        if jid and jid not in t["scraped_ids"]:
            t["scraped_ids"].append(jid)

    new = [j for j in jobs if str(j.get("job_id") or "") and str(j["job_id"]) not in announced]

    if not new:
        first_notice = not t.get("postings_notice_sent")
        t["postings_notice_sent"] = True
        _save(today_path(), t)
        if first_notice:
            total = len(jobs)
            if total == 0:
                _send("📭 <b>Handshake daily check</b>\n\n0 new job postings — nothing matched "
                      "your filters today. Nothing to apply to.")
            else:
                _send("📭 <b>Handshake daily check</b>\n\n0 new job postings today. "
                      f"{total} prior shortlisted listing(s) still open — nothing new to apply to.")
        return 0

    t["postings_notice_sent"] = True
    _save(today_path(), t)
    lines = []
    for j in new:
        jid = str(j["job_id"])
        title = html.escape(str(j.get("title", ""))[:70])
        company = html.escape(str(j.get("company", "")))
        typ = html.escape(str(j.get("type", "") or "?"))
        score = j.get("score")
        tag = f"{typ}, score {score}" if score is not None else typ
        lines.append(f"• {title} — {company} [{tag}]")
        announced[jid] = {"date": today, "title": j.get("title", ""),
                          "company": j.get("company", ""), "type": j.get("type")}
    _save(ANNOUNCED, announced)
    head = f"🆕 {len(new)} new Handshake posting{'s' if len(new) != 1 else ''} found:\n\n"
    _send(head + "\n".join(lines))
    return len(new)


def record_run(jobs: list[dict], results: list[dict], marked: bool):
    """Accumulate this run's outcomes into today's stats file (deduped by job)."""
    t = load_today()
    t["runs"].append(datetime.now().strftime("%H:%M"))

    def _add(bucket: list, item: dict, key="job"):
        if not any(x.get(key) == item.get(key) for x in bucket):
            bucket.append(item)

    for r in results:
        res = r.get("result")
        job = r.get("job", "")
        if res == "applied":
            _add(t["applied"], {"job": job})
        elif r.get("system_failure"):
            _add(t["errors"], {"job": job, "reason": res})
        elif res in ("reject", "timeout", "blocked_unanswered_question",
                     "blocked_missing_documents", "no_inportal_apply", "external"):
            _add(t["skipped"], {"job": job, "reason": res})
    _save(today_path(), t)
