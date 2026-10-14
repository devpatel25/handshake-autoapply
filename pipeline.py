#!/usr/bin/env python3
"""Handshake daily auto-apply orchestrator (Step 7).

Flow (mirrors nuworks/wellfound; human-in-the-loop):
  0. singleton lock + once-per-day guard + watchdog
  1. session preflight        (session_check.py; exit 3 -> ping re-login, abort)
  2. discover                 (discover.py -> data/jobs_YYYY-MM-DD.json)
  3. score                    (score.py    -> data/shortlist_YYYY-MM-DD.json)
  4. announce new postings    (stats.py; pings even on a 0-new day)
  5. classify the top shortlist cheaply (apply_type) -> keep in_portal; +re-queue pending
  6. per job (<= max_per_day, min_gap between real applies):
       tailor resume -> DRY stage pass (read screening Qs, screenshot, cancel)
       -> Telegram approval card (resume PDF + proposed answers + flags)
       -> on Approve, auto_apply.apply_to_job(mode="submit") -> verify
  7. mark the day done only if NO system failures (else leave unmarked -> retry live)

NOTHING is submitted to an employer without an explicit Telegram Approve tap.
A required screening question we can't answer (source 'none') BLOCKS the submit.

Usage:
  python3 pipeline.py [--no-scrape] [--dry-apply] [--limit N] [--force] [--skip-approval]
    --no-scrape     reuse today's existing jobs file (skip discover)
    --dry-apply     do everything incl. approval, but never click Submit
    --limit N       cap jobs classified/processed (default = apply.max_per_day)
    --force         run even if already marked done today
    --skip-approval auto-approve (TESTING ONLY; never schedule this)
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import claude_runner
import tailor
import apply_type
import auto_apply
import cover_letter
import screening  # noqa: F401  (kept explicit: answers are computed inside auto_apply)
import fixer
import stats
import supervisor
import telegram_bot as tg

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
PY = sys.executable

CFG = json.loads((ROOT / "config.json").read_text())
APPLY_CFG = CFG.get("apply", {})
MAX_PER_DAY = APPLY_CFG.get("max_per_day", 8)
MIN_GAP_SECONDS = APPLY_CFG.get("min_gap_seconds", 90)
PREF_MIN_SCORE = APPLY_CFG.get("preference_mismatch_min_score", 40)
TAILOR_PARALLELISM = APPLY_CFG.get("tailor_parallelism", 6)
COVER_PARALLELISM = APPLY_CFG.get("cover_parallelism", TAILOR_PARALLELISM)
MAX_RETRY_ATTEMPTS = APPLY_CFG.get("max_retry_attempts", 4)
UNMET_MAX_ATTEMPTS = APPLY_CFG.get("unmet_max_attempts", 1)
SUPERVISOR_CFG = CFG.get("supervisor", {})
MAX_DIAGNOSES_PER_RUN = SUPERVISOR_CFG.get("max_diagnoses_per_run", 8)

APPLIED_FILE = DATA / "applied.json"
PENDING_FILE = DATA / "pending_retry.json"
WONTFIX_FILE = DATA / "wontfix.json"
SKIP_FILE = DATA / "skip_jobs.txt"
RUN_STAMP = LOGS / "last_run_date.txt"
LOCK_FILE = LOGS / "pipeline.lock"

APPROVAL_TIMEOUT = 3600  # 1h per job

# ── Watchdog budgets (a hung step must never run for hours) ──────────────────
SESSION_BUDGET = 180
SCRAPE_BUDGET = 420                     # discover subprocess (auth inject + paginate)
SCORE_BUDGET = 180
CLASSIFY_PER_JOB = 30                   # one page visit per classified job
_TAILOR_ATTEMPTS = tailor.max_total_attempts()
_TAILOR_BACKOFF = claude_runner.DEFAULT_BACKOFF * sum(min(i, 4) for i in range(1, _TAILOR_ATTEMPTS))
TAILOR_BUDGET = _TAILOR_ATTEMPTS * claude_runner.DEFAULT_TIMEOUT + _TAILOR_BACKOFF + 300
APPLY_BUDGET = 600                      # one modal stage/submit pass + perception audit
FIX_BUDGET = 360                        # Phase 5 fixer pass at run start (harvest + up to one coding pass)

_lock_fh = None
_watchdog_deadline = None


class ChallengeAbort(Exception):
    """A bot challenge (captcha/403) hit a browser step — pause the whole run."""


def log(msg: str):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOGS / "pipeline.log", "a") as f:
        f.write(line + "\n")


def _notify(msg: str):
    """Best-effort Telegram for run-control pings — a network/DNS blip must never turn a
    clean abort into a crash (it did: a session-expired ping with no network raised an
    uncaught ConnectionError). Logs on failure; never raises."""
    try:
        tg.send_message(msg)
    except Exception as e:
        log(f"telegram notify failed (continuing): {type(e).__name__}: {e}")


# ── Singleton lock (fcntl; auto-released when the process exits) ─────────────
def acquire_singleton_lock() -> bool:
    global _lock_fh
    import fcntl
    _lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        _lock_fh.close()
        _lock_fh = None
        return False
    _lock_fh.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
    _lock_fh.flush()
    return True


# ── Watchdog ─────────────────────────────────────────────────────────────────
def _start_watchdog(budget: int):
    global _watchdog_deadline
    _watchdog_deadline = time.time() + budget

    def loop():
        while True:
            time.sleep(5)
            if _watchdog_deadline and time.time() > _watchdog_deadline:
                log("⏱️  WATCHDOG: run exceeded its time budget — a step is hung. "
                    "Aborting (today left UNMARKED → retries next run).")
                try:
                    if tg.configured():
                        tg.send_message("⏱️ <b>Handshake watchdog tripped</b>\n\nA step hung past "
                                        "its budget; the run was aborted and the day left "
                                        "<b>unmarked</b> so it retries. Check <code>logs/</code>.")
                except Exception:
                    pass
                os._exit(2)

    threading.Thread(target=loop, daemon=True).start()


def bump_watchdog(extra: int):
    global _watchdog_deadline
    if _watchdog_deadline is not None:
        _watchdog_deadline = max(_watchdog_deadline, time.time() + extra)


# ── Once-per-day guard ───────────────────────────────────────────────────────
def already_ran_today() -> bool:
    return RUN_STAMP.exists() and RUN_STAMP.read_text().strip() == date.today().isoformat()


def mark_ran_today():
    RUN_STAMP.write_text(date.today().isoformat())


# ── applied.json (authoritative don't-reapply; keyed by job_id) ──────────────
def load_applied() -> dict:
    return json.loads(APPLIED_FILE.read_text()) if APPLIED_FILE.exists() else {}


def record_applied(job: dict, detail: str):
    applied = load_applied()
    applied[str(job["job_id"])] = {
        "title": job.get("title", ""), "company": job.get("company", ""),
        "url": job.get("url", ""), "appliedAt": datetime.now().isoformat(timespec="seconds"),
        "detail": detail,
    }
    APPLIED_FILE.write_text(json.dumps(applied, indent=2))


def applied_today_count() -> int:
    today = date.today().isoformat()
    return sum(1 for v in load_applied().values()
               if str(v.get("appliedAt", ""))[:10] == today)


# ── Pending re-queue (jobs whose approval timed out / unverified submit) ─────
def load_pending() -> list:
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text())
    except Exception:
        return []


def save_pending(items: list):
    PENDING_FILE.write_text(json.dumps(items, indent=2))


def add_pending(job: dict, failure_class: str = "transient", detail: str = ""):
    """Upsert a job into the retry queue. First time: record it with _attempts=1 and
    _first_queued. Subsequent times: increment _attempts and refresh the failure class,
    detail and timestamp — the supervisor uses _attempts + _last_class to cap doomed
    loops (a job that keeps failing the same structural way is evicted, not retried)."""
    items = load_pending()
    now = datetime.now().isoformat()
    for p in items:
        if p.get("job_id") == job.get("job_id"):
            p["_attempts"] = int(p.get("_attempts", 0) or 0) + 1
            p["_last_class"] = failure_class
            p["_last_detail"] = (detail or "")[:300]
            p["_queued_at"] = now
            save_pending(items)
            return
    keep = {k: job.get(k) for k in ("job_id", "title", "company", "location", "url",
                                    "type", "description", "score", "studentScreen")}
    keep["_queued_at"] = now
    keep["_first_queued"] = now
    keep["_attempts"] = 1
    keep["_last_class"] = failure_class
    keep["_last_detail"] = (detail or "")[:300]
    items.append(keep)
    save_pending(items)


def clear_pending(job_id: str):
    items = load_pending()
    kept = [p for p in items if p.get("job_id") != job_id]
    if len(kept) != len(items):
        save_pending(kept)


def load_skiplist() -> set:
    if not SKIP_FILE.exists():
        return set()
    out = set()
    for line in SKIP_FILE.read_text().splitlines():
        tok = line.split("#", 1)[0].strip()
        if tok:
            out.add(tok)
    return out


def load_wontfix() -> set:
    """job_ids the supervisor has given up auto-retrying (data/wontfix.json, a dict
    keyed by job_id). Filtered out of eligible()/merge_pending() so they stop looping."""
    if not WONTFIX_FILE.exists():
        return set()
    try:
        return set(json.loads(WONTFIX_FILE.read_text()).keys())
    except Exception:
        return set()


# ── Stage runner (subprocess; exit 2 = bot challenge -> pause the run) ───────
def run_stage(name: str, cmd: list, allow_challenge_abort=True) -> bool:
    log(f"stage: {name} -> {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode == 2 and allow_challenge_abort:
        try:
            tg.send_message(f"🛑 <b>Handshake paused</b>\nBot challenge during <b>{name}</b>. "
                            "Session may need a re-capture (run <code>capture_session.py</code>).")
        except Exception:
            pass
        log(f"{name} hit a challenge (exit 2); aborting run.")
        raise ChallengeAbort(name)
    if r.returncode != 0:
        log(f"{name} failed (exit {r.returncode}).")
        return False
    return True


def _location_str(job: dict) -> str:
    locs = job.get("locations") or []
    return locs[0] if locs else ("Remote" if job.get("remote") else "")


def eligible(shortlist: list, limit: int) -> list:
    """Cheaply classify the top `limit` shortlisted jobs (one page visit each) and
    keep only in_portal, not-already-applied, not-skiplisted ones. Tailoring (which
    is expensive) then runs ONLY on these — never on external/applied jobs."""
    applied = load_applied()
    skiplist = load_skiplist()
    wontfix = load_wontfix()
    top = [j for j in shortlist
           if str(j.get("job_id")) not in applied and str(j.get("job_id")) not in wontfix][:limit]
    if not top:
        return []
    bump_watchdog(CLASSIFY_PER_JOB * len(top) + 60)
    cls = {c["job_id"]: c for c in apply_type.classify_jobs(
        [{"job_id": j["job_id"], "url": j.get("url"), "title": j.get("title")} for j in top])}
    keep = []
    for j in top:
        jid = str(j["job_id"])
        c = cls.get(jid, {})
        j["_apply_type"] = c.get("type")
        j["_required_docs"] = c.get("required_docs", [])
        j["location"] = _location_str(j)
        if jid in skiplist:
            log(f"  skip [skiplist] {str(j.get('title',''))[:50]}")
        elif c.get("type") == "in_portal":
            keep.append(j)
        else:
            log(f"  skip [{c.get('type')}] {str(j.get('title',''))[:50]}")
    return keep


def merge_pending(todo: list, limit: int) -> list:
    """Re-offer jobs whose approval timed out before, but only if the portal still
    shows them in_portal and not applied (re-classify; drop applied/closed)."""
    pend = load_pending()
    if not pend:
        return todo
    have = {str(j["job_id"]) for j in todo}
    applied = load_applied()
    wontfix = load_wontfix()
    extra = [p for p in pend if p.get("job_id") and str(p["job_id"]) not in have
             and str(p["job_id"]) not in applied and str(p["job_id"]) not in wontfix][:limit]
    if not extra:
        return todo
    skiplist = load_skiplist()
    cls = {c["job_id"]: c for c in apply_type.classify_jobs(
        [{"job_id": p["job_id"], "url": p.get("url"), "title": p.get("title")} for p in extra])}
    out = list(todo)
    for p in extra:
        jid = str(p["job_id"])
        c = cls.get(jid, {})
        if jid in skiplist:
            clear_pending(jid)
        elif c.get("type") == "in_portal":
            p["_apply_type"] = "in_portal"
            p["_required_docs"] = c.get("required_docs", [])
            p["location"] = _location_str(p)
            out.append(p)
            log(f"  re-queued (prior timeout): {str(p.get('title',''))[:50]}")
        else:
            clear_pending(jid)  # applied/closed/external now — no retry
            log(f"  pending dropped [{c.get('type')}]: {str(p.get('title',''))[:50]}")
    return out


def _tailor_one(job: dict) -> dict:
    """Tailor ONE job's resume (reusing a 1-page PDF if already present). Pure worker
    for the thread pool — returns a result dict; does no logging (the main thread logs
    as futures complete) and mutates nothing shared (tailor uses a per-job workdir)."""
    existing = tailor.latest_pdf(job)
    if existing:
        return {"pdf": existing, "reused": True}
    res = tailor.tailor_with_claude(job)
    if res["ok"]:
        return {"pdf": res["pdf"], "reused": False, "attempts": res.get("attempts"),
                "model": res.get("model"), "escalated": res.get("escalated")}
    return {"pdf": None, "fail": {"stage": "résumé tailoring", "code": res.get("fail_code"),
            "reason": res.get("fail_reason"), "log": res.get("log"),
            "attempts": res.get("attempts")}}


def tailor_all_parallel(todo: list, workers: int):
    """Tailor every shortlisted job's resume CONCURRENTLY up front (the slow claude -p
    calls), instead of one-at-a-time inside the apply loop. Each job's result is cached
    on the job dict as job["_resume_pdf"] (None on failure) so get_resume() just reuses
    it. Tailors are independent (per-job workdirs), so threads are safe. This collapses
    serial ~N×(minutes-per-resume) into roughly one wave."""
    if not todo:
        return
    workers = max(1, min(workers, len(todo)))
    log(f"tailoring {len(todo)} resume(s) in parallel (workers={workers})...")
    bump_watchdog(TAILOR_BUDGET + 600)
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_tailor_one, j): j for j in todo}
        for fut in as_completed(futs):
            job = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"pdf": None, "fail": {"stage": "résumé tailoring", "code": "crash",
                     "reason": f"{type(e).__name__}: {e}"}}
            job["_resume_pdf"] = r["pdf"]
            done += 1
            t = (job.get("title", "") or "")[:40]
            if r["pdf"]:
                how = "reused" if r.get("reused") else (
                    "tailored" + (" (Opus)" if r.get("escalated") else ""))
                log(f"  [{done}/{len(todo)}] ✓ {how}: {t} -> {Path(r['pdf']).name}")
            else:
                job["_fail"] = r.get("fail")
                log(f"  [{done}/{len(todo)}] ✗ tailor FAILED: {t} "
                    f"[{(r.get('fail') or {}).get('code')}]")
            bump_watchdog(TAILOR_BUDGET)  # keep the watchdog alive while others finish
    log(f"parallel tailoring finished in {int(time.time() - t0)}s "
        f"({sum(1 for j in todo if j.get('_resume_pdf'))}/{len(todo)} ready)")


def _cover_one(job: dict) -> dict:
    """Generate ONE job's cover letter (claude -p). Pure thread-pool worker — per-job
    workdir, no shared mutation, no logging (the main thread logs as futures complete)."""
    pdf = job.get("_resume_pdf")
    rtex = Path(pdf).with_suffix(".tex") if pdf else None
    src = str(rtex if (rtex and rtex.exists()) else (pdf or ""))
    res = cover_letter.generate_with_claude(job, src)
    if res.get("ok"):
        return {"pdf": res["pdf"]}
    return {"pdf": None, "fail": {"code": res.get("fail_code"), "reason": res.get("fail_reason")}}


def cover_all_parallel(cl_jobs: list, workers: int):
    """Generate cover letters CONCURRENTLY — but ONLY for the jobs passed in (those whose
    apply modal actually has a cover-letter section). Same idea as tailor_all_parallel;
    caches job["_cover_pdf"] (None on failure). Skips jobs that already have one cached
    (lets tests pre-seed). Not every job gets a letter — only the ones that need one."""
    todo = [j for j in cl_jobs if not j.get("_cover_pdf")]
    if not todo:
        return
    workers = max(1, min(workers, len(todo)))
    log(f"generating {len(todo)} cover letter(s) in parallel (workers={workers})...")
    bump_watchdog(TAILOR_BUDGET + 600)
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_cover_one, j): j for j in todo}
        for fut in as_completed(futs):
            job = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"pdf": None, "fail": {"code": "crash", "reason": f"{type(e).__name__}: {e}"}}
            job["_cover_pdf"] = r["pdf"]
            done += 1
            t = (job.get("title", "") or "")[:40]
            if r["pdf"]:
                log(f"  [{done}/{len(todo)}] ✓ cover letter: {t} -> {Path(r['pdf']).name}")
            else:
                log(f"  [{done}/{len(todo)}] ✗ cover letter FAILED: {t} "
                    f"[{(r.get('fail') or {}).get('code')}]")
            bump_watchdog(TAILOR_BUDGET)
    log(f"parallel cover letters finished in {int(time.time() - t0)}s "
        f"({sum(1 for j in todo if j.get('_cover_pdf'))}/{len(todo)} ready)")


def get_resume(job: dict):
    """Return the tailored PDF. Prefer the result of the parallel tailoring stage
    (job["_resume_pdf"], possibly None on failure); otherwise reuse/tailor inline."""
    if "_resume_pdf" in job:
        return job["_resume_pdf"]
    existing = tailor.latest_pdf(job)
    if existing:
        log(f"  using existing tailored resume: {Path(existing).name}")
        return existing
    log("  tailoring resume (ResumeBot2.1 via claude -p)...")
    res = tailor.tailor_with_claude(job)
    if res["ok"]:
        esc = " — escalated to Opus" if res.get("escalated") else ""
        log(f"  tailored: {Path(res['pdf']).name} (attempt {res.get('attempts')}, "
            f"{res.get('model','?')}{esc})")
        return res["pdf"]
    job["_fail"] = {"stage": "résumé tailoring", "code": res.get("fail_code"),
                    "reason": res.get("fail_reason"), "log": res.get("log"),
                    "attempts": res.get("attempts")}
    log(f"  ⚠ tailoring failed [{res.get('fail_code')}]: {res.get('fail_reason')}")
    return None


def _alert_failure(job: dict, fail: dict):
    title = (job.get("title", "") or "")[:60]
    msg = ("🛑 <b>Handshake auto-apply failed — needs a look</b>\n\n"
           f"<b>{title}</b>\n{job.get('company','')}\n\n"
           f"<b>Stage:</b> {fail.get('stage')}  (<code>{fail.get('code')}</code>)\n"
           f"<b>Likely problem:</b> {fail.get('reason')}\n\n"
           f"Tried {fail.get('attempts','?')}×. Day left <b>unmarked</b> → it retries next run.")
    if fail.get("log"):
        msg += f"\n\n📄 Attempt log: <code>{fail['log']}</code>"
    try:
        if tg.configured():
            tg.send_message(msg)
    except Exception:
        pass


def process(job: dict, dry_apply: bool) -> dict:
    """LEGACY per-job flow (serial: tailor→stage→cover→card→wait→submit, one job at a
    time). Superseded by apply_batch(); kept for reference, not called by main()."""
    title = (job.get("title", "") or "")[:55]
    jid = str(job["job_id"])
    log(f"JOB: {title} ({job.get('company','')})")
    bump_watchdog(TAILOR_BUDGET + APPLY_BUDGET)

    pdf = get_resume(job)
    if not pdf:
        _alert_failure(job, job.get("_fail", {"stage": "résumé tailoring"}))
        return {"job": title, "result": "tailor_failed", "system_failure": True}

    # DRY stage pass: open the modal, upload the resume, read screening questions,
    # screenshot, cancel. Never submits. Builds the data the approval card shows.
    bump_watchdog(APPLY_BUDGET)
    staged = auto_apply.apply_to_job(job, pdf, mode="dry")
    st = staged.get("status")
    log(f"  staged: {st} — {staged.get('detail','')}")

    if st == "challenge_abort":
        raise ChallengeAbort("apply(dry)")
    if st == "already_applied":
        record_applied(job, "already applied (portal showed Withdraw/Applied)")
        clear_pending(jid)
        return {"job": title, "result": "already_applied"}
    if st in ("no_inportal_apply", "external"):
        return {"job": title, "result": "no_inportal_apply", "detail": staged.get("detail", "")}
    if st == "blocked_missing_documents":
        add_pending(job)
        tg.send_message(f"📎 Needs a cover letter / extra doc (not yet automated): <b>{title}</b>\n"
                        "Re-queued; finish manually for now.")
        return {"job": title, "result": "blocked_missing_documents"}
    if st == "blocked_unanswered_question":
        add_pending(job)
        qs = staged.get("detail", "")
        tg.send_message(f"🔴 Can't auto-answer a required question for <b>{title}</b>:\n{qs}\n\n"
                        "I will NOT auto-submit. Finish this one manually; re-queued.")
        return {"job": title, "result": "blocked_unanswered_question"}
    if st == "error":
        tg.send_message(f"⚠️ Error staging <b>{title}</b>: {staged.get('detail','')[:160]}")
        return {"job": title, "result": "error", "detail": staged.get("detail"), "system_failure": True}
    if st != "staged":
        return {"job": title, "result": st, "detail": staged.get("detail")}

    answers = staged.get("answers") or []
    pref_mismatch = bool(staged.get("preference_mismatch"))
    # Preference mismatch + a weak score -> skip (PLAN P1.9: apply-anyway only if strong).
    if pref_mismatch and (job.get("score") or 0) < PREF_MIN_SCORE:
        tg.send_message(f"⏭️ Skipped <b>{title}</b> — you don't meet employer preferences and the "
                        f"score ({job.get('score')}) is below the apply-anyway bar ({PREF_MIN_SCORE}).")
        return {"job": title, "result": "pref_mismatch_skip"}

    # Cover letter: if the apply modal has a cover-letter section, generate a tailored
    # letter (claude -p) from the JD + resume so it's attached + shown on the card.
    cover_pdf = None
    if staged.get("has_cover_section"):
        bump_watchdog(TAILOR_BUDGET)
        rtex = Path(pdf).with_suffix(".tex")
        log("  cover-letter section present — generating tailored cover letter...")
        cl = cover_letter.generate_with_claude(job, str(rtex if rtex.exists() else pdf))
        if cl.get("ok"):
            cover_pdf = cl["pdf"]
            log(f"  cover letter: {Path(cover_pdf).name}")
        else:
            log(f"  ⚠ cover-letter generation failed [{cl.get('fail_code')}]: {cl.get('fail_reason')}")

    # Approval card: resume PDF (+ cover letter) + the exact screening answers we'd submit.
    summary = f"Score {job.get('score','?')} · visa:{job.get('visa','?')} · {job.get('location','')}"
    nonce = uuid.uuid4().hex[:8]
    log("  sending approval card to Telegram...")
    try:
        tg.send_application_for_approval(pdf, job, answers=answers, summary=summary,
                                         nonce=nonce, preference_mismatch=pref_mismatch,
                                         cover_pdf=cover_pdf)
    except Exception as e:
        log(f"  ⚠ failed to send approval card: {e}")
        return {"job": title, "result": "telegram_send_failed", "system_failure": True}
    bump_watchdog(APPROVAL_TIMEOUT + APPLY_BUDGET + 120)
    decision = tg.wait_for_decision(jid, nonce=nonce, timeout=APPROVAL_TIMEOUT)
    log(f"  decision: {decision}")

    if decision == "timeout":
        add_pending(job)
        tg.send_message(f"⏳ No response in time: {title}\nRe-queued — I'll offer it again next run.")
        return {"job": title, "result": "timeout"}
    if decision != "approve":
        clear_pending(jid)
        tg.send_message(f"⏭️ Skipped: {title}")
        return {"job": title, "result": "reject"}

    if dry_apply:
        tg.send_message(f"🧪 Approved (dry — NOT submitted): {title}\n"
                        f"Screening answers staged: {len(answers)}.")
        return {"job": title, "result": "staged_approved"}

    # REAL submit.
    bump_watchdog(APPLY_BUDGET)
    log("  submitting (mode=submit)...")
    res = auto_apply.apply_to_job(job, pdf, answers=answers, mode="submit", approved=True, cover_pdf=cover_pdf)
    log(f"  apply result: {res['status']} — {res['detail']}")
    if res["status"] == "challenge_abort":
        raise ChallengeAbort("apply(submit)")
    if res["status"] == "applied":
        record_applied(job, res["detail"])
        clear_pending(jid)
        tg.send_message(f"✅ Applied: <b>{title}</b>\n{res['detail']}")
        return {"job": title, "result": "applied", "detail": res["detail"]}
    if res["status"] == "submitted_unverified":
        add_pending(job)
        tg.send_message(f"⚠️ <b>Unverified submit: {title}</b>\n{res['detail']}\n\n"
                        "I'll re-check next run: the double-apply guard skips it if it went "
                        "through, else it re-applies. No action needed.")
        return {"job": title, "result": "submitted_unverified", "detail": res["detail"],
                "system_failure": True}
    # blocked_* / error / anything else
    tg.send_message(f"⚠️ {res['status']}: {title}\n{res['detail'][:160]}")
    return {"job": title, "result": res["status"], "detail": res["detail"],
            "system_failure": res["status"] == "error"}


def _result(job: dict, status: str, detail: str = "", system_failure: bool = False,
            extra: dict = None) -> dict:
    """Uniform apply-batch result. Always carries job_id (the supervisor keys incidents
    by it); for non-success outcomes, pass `extra` (the staged/res dict) to attach the
    brain-relevant fields the supervisor diagnoses from."""
    r = {"job": (job.get("title", "") or "")[:55], "job_id": str(job.get("job_id", "") or ""),
         "result": status, "detail": detail or ""}
    if system_failure:
        r["system_failure"] = True
    if extra:
        for k in ("screenshot", "attached_resume", "questions", "preference_mismatch",
                  "has_cover_section", "has_transcript_section"):
            v = extra.get(k)
            if v is not None:
                r[k] = v
    return r


def apply_batch(todo: list, dry_apply: bool) -> list:
    """Batched flow (replaces the per-job process() loop so generation happens up front
    and all approval cards go out together):
      A. dry-stage every job (serial; headed Chrome + shared resume slot) — detect which
         need a cover letter, read screening answers, verify the resume attaches.
      B. generate cover letters in parallel — ONLY for jobs whose modal has a CL section.
      C. send EVERY approval card at once (résumé + cover letter + answers).
      D. collect approvals for all cards and submit each the moment it's approved
         (serial, min-gap), re-queue timeouts. Nothing submits without a tap."""
    results = []
    ready = []   # {job, pdf, answers, pref, title, jid, has_cover, needs_cl}

    # ── A. stage all ────────────────────────────────────────────────────────────
    for j in todo:
        title = (j.get("title", "") or "")[:55]
        jid = str(j["job_id"])
        if not dry_apply and applied_today_count() + len(ready) >= MAX_PER_DAY:
            log(f"daily cap ({MAX_PER_DAY}) reached — not staging more.")
            break
        pdf = get_resume(j)
        if not pdf:
            _alert_failure(j, j.get("_fail", {"stage": "résumé tailoring"}))
            results.append(_result(j, "tailor_failed", "résumé tailoring failed", system_failure=True))
            continue
        log(f"STAGE: {title} ({j.get('company','')})")
        bump_watchdog(APPLY_BUDGET)
        try:
            staged = auto_apply.apply_to_job(j, pdf, mode="dry")
        except Exception as e:
            import traceback
            log(f"  ✗ stage crashed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            results.append(_result(j, "crashed", f"{type(e).__name__}: {e}", system_failure=True))
            continue
        st = staged.get("status")
        log(f"  staged: {st} — {staged.get('detail', '')[:120]}")
        if st == "challenge_abort":
            raise ChallengeAbort("apply(dry)")
        if st == "already_applied":
            record_applied(j, "already applied (portal showed Withdraw/Applied)")
            clear_pending(jid)
            results.append(_result(j, "already_applied", "portal showed Withdraw/Applied"))
            continue
        if st in ("no_inportal_apply", "external"):
            results.append(_result(j, "no_inportal_apply", staged.get("detail", "")))
            continue
        if st == "blocked_unanswered_question":
            add_pending(j, "data_gap", staged.get("detail", ""))
            results.append(_result(j, "blocked_unanswered_question", staged.get("detail", ""), extra=staged))
            continue
        if st == "error":
            tg.send_message(f"⚠️ Error staging <b>{title}</b>: {staged.get('detail', '')[:160]}")
            results.append(_result(j, "error", staged.get("detail", ""), system_failure=True, extra=staged))
            continue
        has_cover = bool(staged.get("has_cover_section"))
        # A CL-required job dry-stages as blocked_missing_documents (Submit disabled w/o a
        # letter); generating + attaching the CL should enable Submit, so treat it as "needs CL".
        needs_cl = (st == "blocked_missing_documents" and has_cover)
        if st == "blocked_missing_documents" and not needs_cl:
            add_pending(j, "data_gap", staged.get("detail", ""))
            results.append(_result(j, "blocked_missing_documents", staged.get("detail", ""), extra=staged))
            continue
        if st != "staged" and not needs_cl:
            results.append(_result(j, st, staged.get("detail", ""), extra=staged))
            continue
        answers = staged.get("answers") or []
        pref = bool(staged.get("preference_mismatch"))
        if pref and (j.get("score") or 0) < PREF_MIN_SCORE:
            tg.send_message(f"⏭️ Skipped <b>{title}</b> — below the apply-anyway bar "
                            f"(score {j.get('score')} &lt; {PREF_MIN_SCORE}).")
            results.append(_result(j, "pref_mismatch_skip", "below apply-anyway score bar"))
            continue
        ready.append({"job": j, "pdf": pdf, "answers": answers, "pref": pref, "title": title,
                      "jid": jid, "has_cover": has_cover, "needs_cl": needs_cl,
                      "has_transcript": bool(staged.get("has_transcript_section"))})

    if not ready:
        log("no jobs ready for approval.")
        return results

    # ── B. cover letters in parallel — ONLY jobs whose modal has a CL section ─────
    cover_all_parallel([r["job"] for r in ready if r["has_cover"]], COVER_PARALLELISM)

    # ── C. send every approval card at once ──────────────────────────────────────
    pending = {}
    for r in ready:
        cover_pdf = r["job"].get("_cover_pdf")
        if r["needs_cl"] and not cover_pdf:
            add_pending(r["job"], "data_gap", "cover letter required but could not be generated")
            results.append(_result(r["job"], "blocked_missing_documents", "cover letter required but could not be generated"))
            continue
        nonce = uuid.uuid4().hex[:8]
        summary = (f"Score {r['job'].get('score', '?')} · visa:{r['job'].get('visa', '?')} · "
                   f"{r['job'].get('location', '')}")
        try:
            tg.send_application_for_approval(r["pdf"], r["job"], answers=r["answers"], summary=summary,
                                             nonce=nonce, preference_mismatch=r["pref"], cover_pdf=cover_pdf,
                                             has_transcript=r.get("has_transcript", False))
        except Exception as e:
            log(f"  ⚠ failed to send card for {r['title']}: {e}")
            results.append(_result(r["job"], "telegram_send_failed", "failed to send approval card", system_failure=True))
            continue
        r["nonce"] = nonce
        r["cover_pdf"] = cover_pdf
        pending[r["jid"]] = nonce

    if not pending:
        return results
    tg.send_message(f"📨 <b>{len(pending)} application(s) ready for your approval.</b>\n"
                    "Tap Approve/Reject on each card — I'll submit the approved ones "
                    "(nothing goes out without your tap).")

    # ── D. collect approvals + submit each as it's approved (serial, min-gap) ─────
    last_submit = [0.0]

    def on_decision(jid, action):
        r = next((x for x in ready if x["jid"] == jid), None)
        if not r:
            return
        title = r["title"]
        try:
            if action != "approve":
                clear_pending(jid)
                tg.send_message(f"⏭️ Skipped: {title}")
                results.append(_result(r["job"], "reject"))
                return
            if dry_apply:
                tg.send_message(f"🧪 Approved (dry — NOT submitted): {title}")
                results.append(_result(r["job"], "staged_approved"))
                return
            if applied_today_count() >= MAX_PER_DAY:
                add_pending(r["job"], "transient", "daily cap reached")
                results.append(_result(r["job"], "cap_reached", "daily cap reached"))
                return
            gap = MIN_GAP_SECONDS - (time.time() - last_submit[0])
            if last_submit[0] and gap > 0:
                bump_watchdog(int(gap) + APPLY_BUDGET)
                time.sleep(gap)
            bump_watchdog(APPLY_BUDGET)
            log(f"  submitting (approved): {title}")
            res = auto_apply.apply_to_job(r["job"], r["pdf"], answers=r["answers"], mode="submit",
                                          approved=True, cover_pdf=r.get("cover_pdf"))
            last_submit[0] = time.time()
            log(f"  apply result: {res['status']} — {res['detail'][:120]}")
            if res["status"] == "applied":
                record_applied(r["job"], res["detail"])
                clear_pending(jid)
                tg.send_message(f"✅ Applied: <b>{title}</b>\n{res['detail']}")
                results.append(_result(r["job"], "applied", res["detail"]))
            elif res["status"] == "submitted_unverified":
                # Not a system failure: the submit may well have gone through, so we no
                # longer leave the day unmarked (which re-ran the whole pipeline every
                # slot). Re-queue once as verify_gap; the supervisor reports it and the
                # double-apply guard resolves it next run.
                add_pending(r["job"], "verify_gap", res["detail"])
                results.append(_result(r["job"], "submitted_unverified", res["detail"], extra=res))
            elif res["status"] == "challenge_abort":
                tg.send_message(f"🛑 Bot challenge submitting {title}; the session may need re-capture.")
                results.append(_result(r["job"], "challenge_abort", system_failure=True))
            else:
                tg.send_message(f"⚠️ {res['status']}: {title}\n{res['detail'][:160]}")
                results.append(_result(r["job"], res["status"], res["detail"],
                                       system_failure=(res["status"] == "error"), extra=res))
        except Exception as e:
            import traceback
            log(f"  ✗ submit crashed on {title}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            try:
                tg.send_message(f"🛑 Crashed submitting <b>{title}</b>: "
                                f"<code>{type(e).__name__}: {str(e)[:160]}</code>")
            except Exception:
                pass
            results.append(_result(r["job"], "crashed", f"{type(e).__name__}: {e}", system_failure=True))

    bump_watchdog(APPROVAL_TIMEOUT + APPLY_BUDGET + 120)
    timed_out = tg.collect_decisions(pending, timeout=APPROVAL_TIMEOUT, on_decision=on_decision)
    for jid in timed_out:
        r = next((x for x in ready if x["jid"] == jid), None)
        if not r:
            continue
        add_pending(r["job"], "transient", "approval timed out")
        results.append(_result(r["job"], "timeout", "approval timed out"))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scrape", action="store_true")
    ap.add_argument("--dry-apply", action="store_true")
    ap.add_argument("--limit", type=int, default=MAX_PER_DAY)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-approval", action="store_true")
    args = ap.parse_args()

    log("=" * 55)
    log("Handshake auto-apply pipeline starting")
    log(f"telegram: {tg.configured()} | dry_apply: {args.dry_apply} | force: {args.force} "
        f"| limit: {args.limit}")

    if not acquire_singleton_lock():
        log("Another pipeline run is active (lock held) — exiting.")
        return
    if not tg.configured():
        log("Telegram not configured — aborting (the approval gate is mandatory). "
            "Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to .env.")
        return

    _start_watchdog(FIX_BUDGET)

    # Phase 5: harvest + apply approved code fixes FIRST — before any other Telegram poll
    # (so the fix-tap poll can't race the app-approval poll, the 409) AND ahead of the
    # once-per-day apply guard, so an approved fix isn't stalled a whole day. Self-bounded
    # (claude_runner + harvest timeouts) under FIX_BUDGET.
    try:
        fixer.harvest_and_run()
    except Exception as e:
        log(f"fixer pass failed (continuing): {type(e).__name__}: {e}")

    if already_ran_today() and not args.force:
        log("Already ran today — apply run skipped (approved fixes were harvested above).")
        return

    bump_watchdog(SESSION_BUDGET + SCRAPE_BUDGET)

    try:
        today = date.today().isoformat()

        # 1. session preflight
        bump_watchdog(SESSION_BUDGET)
        chk = subprocess.run([PY, "session_check.py"], cwd=str(ROOT))
        if chk.returncode == 3:
            _notify("🔑 <b>Handshake session expired.</b>\nRun <code>python3 "
                            "capture_session.py</code> on the Mac, log in (SSO+DUO), then re-run. "
                            "No jobs were processed.")
            log("session expired; aborting (unmarked).")
            return
        if chk.returncode == 2:
            _notify("🛑 Bot challenge on session check; aborting this run.")
            log("challenge on session check; aborting.")
            return

        # 2. discover
        discover_failed = False
        if not args.no_scrape:
            bump_watchdog(SCRAPE_BUDGET)
            if not run_stage("discover", [PY, "discover.py"]):
                # A failed discover is almost always a transient network/site outage
                # (e.g. net::ERR_INTERNET_DISCONNECTED). It must NOT let the run mark the
                # day done off stale/empty data — that silently burns every later slot
                # (the 2026-06-25 incident: net dropped mid-day, discover failed, 0 jobs,
                # "clean" run marked the day, 16:30/18:30/20:30 all skipped). We still
                # attempt a fallback if today's jobs file already exists, but the day is
                # left UNMARKED below so a later slot re-scrapes once the net is back.
                discover_failed = True
                jobs_today = DATA / f"jobs_{today}.json"
                if jobs_today.exists():
                    _notify("⚠️ Discovery failed (network/site issue). Falling back to "
                            "today's existing jobs file; day left UNMARKED so a later "
                            "slot re-scrapes.")
                else:
                    _notify("⚠️ Discovery failed and there's no jobs file for today — "
                            "nothing to process. Day left UNMARKED so a later slot retries.")
                    log("discover failed and no fresh jobs file; aborting (unmarked).")
                    return

        # 3. score
        bump_watchdog(SCORE_BUDGET)
        if not run_stage("score", [PY, "score.py", "--date", today], allow_challenge_abort=False):
            _notify("⚠️ Scoring failed; aborting run.")
            return

        shortlist_path = DATA / f"shortlist_{today}.json"
        shortlist = json.loads(shortlist_path.read_text()) if shortlist_path.exists() else []

        # 4. announce (pings even on a 0-new day); keep the count so the supervisor can
        #    flag a "scraped N but applied 0" gap instead of staying silent.
        new_count = stats.announce_new_postings(shortlist)

        # 5. classify + re-queue pending
        todo = eligible(shortlist, args.limit)
        todo = merge_pending(todo, args.limit)
        log(f"{len(todo)} in-portal job(s) to process")

        # 5b. tailor ALL resumes concurrently up front (the slow claude -p calls).
        #     Each job's PDF is cached on job["_resume_pdf"]; process()/get_resume()
        #     just reuse it. Collapses serial N×(minutes-per-resume) into ~one wave.
        tailor_all_parallel(todo, TAILOR_PARALLELISM)

        # 6. stage all -> parallel cover letters -> send all cards together -> collect
        #    approvals + submit each as approved. apply_batch handles cap + min-gap.
        try:
            results = apply_batch(todo, args.dry_apply)
        except ChallengeAbort as e:
            _notify(f"🛑 Bot challenge during apply ({e}); pausing run. "
                            "Re-capture the session if it persists.")
            log(f"challenge abort: {e}; stopping (day unmarked).")
            results = [{"job": "—", "result": "challenge_abort", "system_failure": True}]
        except Exception as e:
            import traceback
            log(f"  ✗ UNHANDLED in apply_batch: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            try:
                tg.send_message(f"🛑 Crashed during apply batch\n"
                                f"<code>{type(e).__name__}: {str(e)[:200]}</code>\n\n"
                                "Day left unmarked → retries next run.")
            except Exception:
                pass
            results = [{"job": "batch", "result": "crashed", "system_failure": True}]

        log("=" * 55)
        log("SUMMARY:")
        for r in results:
            log(f"  {r['result']:<24} {r['job']}")

        failures = [r for r in results if r.get("system_failure")]
        # A degraded discover (stale/empty data) is treated like a system failure for the
        # mark/retry decision: never mark a day done when we couldn't actually scrape.
        marked = not (failures or discover_failed)
        if not marked:
            reasons = []
            if failures:
                reasons.append(f"{len(failures)} system failure(s)")
            if discover_failed:
                reasons.append("discovery failed (degraded/stale data)")
            log(f"{' + '.join(reasons)} — leaving today UNMARKED so it retries.")
        else:
            mark_ran_today()
            log("Run clean — marked as done for today.")

        stats.record_run(shortlist, results, marked=marked)

        # Supervisor: classify outcomes, prune the doomed retry queue into wontfix.json,
        # and send ONE consolidated report (replaces the old per-result chatter + the
        # summary blob). Runs AFTER the mark/unmark decision so it can never affect
        # whether the day is marked; it is internally best-effort and never raises.
        # Give the supervisor room for its (capped, hard-timed-out) brain diagnoses so a
        # slow audit can't trip the watchdog mid-report.
        bump_watchdog(MAX_DIAGNOSES_PER_RUN * 140 + 120)
        try:
            supervisor.run_supervisor(results, shortlist, marked=marked,
                                      new_postings=new_count, discover_failed=discover_failed)
        except Exception as e:
            log(f"supervisor failed (run still complete): {type(e).__name__}: {e}")
    finally:
        pass  # lock auto-releases when the process exits (fd closes)


if __name__ == "__main__":
    main()
