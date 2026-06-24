#!/usr/bin/env python3
"""Autonomous auto-fix engine (Phase 5).

The supervisor's brain diagnoses a code-break and proposes a fix; this module owns the
human-in-the-loop autonomy around applying it: a Telegram one-tap APPROVAL CARD, a
signature-keyed LEDGER (data/fixes_ledger.json) so the same bug is never auto-fixed
twice, a validated CODING PASS, and git commit/revert so every applied fix is auditable
and one-command revertible.

Autonomy tiers (decided mechanically from the files the coding pass changed):
  • Tier 1 — NEVER auto-edit (fixer/supervisor/claude_runner/tests/config/plists/.gitignore/.env):
    rejected + reverted + escalated.
  • Tier 2 — submit-critical (auto_apply/perception/telegram_bot/screening/session_check/
    browser/pipeline): validated, then reverted and a DIFF sent for a 2nd one-tap review;
    only on that approval is the patch re-applied + committed.
  • Tier 3 — everything else: validated → committed → job re-queued.

`apply_enabled` (config.fixer) is the master switch. While false (Phase 5c), the engine
runs the coding pass + every gate but ALWAYS reverts and only REPORTS what it would do —
nothing is ever committed. Decoupled like supervisor.py (writes JSON state directly,
manipulates git via subprocess); never raises into the pipeline.
"""
import json
import sys
import html
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

import claude_runner
import telegram_bot as tg

HERE = Path(__file__).resolve().parent
ROOT = HERE                       # git working dir (monkeypatched to a temp repo in tests)
DATA = HERE / "data"
LEDGER_FILE = DATA / "fixes_ledger.json"
PENDING_FILE = DATA / "pending_retry.json"
WONTFIX_FILE = DATA / "wontfix.json"

TERMINAL = {"applied", "failed", "rejected", "diff_rejected", "abandoned"}

FIX_SYSTEM = (
    "You are a senior engineer applying ONE minimal, surgical bug fix to a Python repo. "
    "Make the smallest change that fixes the described root cause. Do NOT refactor, do "
    "NOT 'improve' unrelated code, do NOT add dependencies, and do NOT edit tests, "
    "config.json, plists, .env, or anything under data/ or logs/. When done, print a "
    "one-line summary of exactly what you changed."
)


def _cfg():
    try:
        return json.loads((HERE / "config.json").read_text()).get("fixer", {})
    except Exception:
        return {}


def _load(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save(p, obj):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2))
    except Exception:
        pass


def load_ledger():
    led = _load(LEDGER_FILE, {})
    return led if isinstance(led, dict) else {}


def save_ledger(led):
    _save(LEDGER_FILE, led)


def _signature(job_id, root_cause):
    """fix_id = stable hash of the bug signature, so the same bug is never auto-fixed twice."""
    basis = f"{job_id}|{(root_cause or '')[:200]}"
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def upsert_proposed(job_id, title, diag, nonce):
    """Create a 'proposed' ledger entry IF this bug signature isn't already tracked.
    Returns fix_id for a NEW proposal (caller sends the card) or None (no duplicate card)."""
    root_cause = (diag or {}).get("root_cause", "")
    fix_id = _signature(job_id, root_cause)
    led = load_ledger()
    if fix_id in led:
        return None
    led[fix_id] = {
        "fix_id": fix_id, "job_id": str(job_id), "title": title,
        "root_cause": root_cause, "proposed_fix": (diag or {}).get("proposed_fix"),
        "confidence": (diag or {}).get("confidence"),
        "status": "proposed", "nonce": nonce, "nonce2": None, "tier": None,
        "card_msg_id": None, "diff_msg_id": None,
        "changed_files": [], "patch": None, "commit": None,
        "fail_step": None, "fail_detail": None, "dry_checked": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "approved_at": None, "resolved_at": None, "attempts": 0,
    }
    save_ledger(led)
    return fix_id


def set_status(fix_id, status, **fields):
    led = load_ledger()
    e = led.get(fix_id)
    if not e:
        return
    e["status"] = status
    e.update(fields)
    if status in TERMINAL:
        e["resolved_at"] = datetime.now().isoformat(timespec="seconds")
    save_ledger(led)


# ── git helpers (operate on ROOT; reset --hard+clean is safe given the clean-tree
#    precondition — it only discards the coding pass's changes; -fd without -x leaves
#    ignored data/, logs/, .env untouched) ─────────────────────────────────────
def _git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def _clean_tree():
    return _git("status", "--porcelain").stdout.strip() == ""


def _revert():
    _git("reset", "--hard", "-q")
    _git("clean", "-fd")


def _changed_files():
    mod = _git("diff", "--name-only").stdout.splitlines()
    unt = _git("ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted({f for f in (mod + unt) if f.strip()})


def _capture_patch():
    _git("add", "-A", "-N")                  # intent-to-add untracked so diff includes them
    return _git("diff").stdout


def _commit(changed, e):
    _git("add", "--", *changed)
    msg = f"autofix {e['fix_id']}: {(e.get('root_cause') or '')[:60]}"
    body = f"job {e.get('job_id')}; autonomous fix, validated (py_compile + import + unittest)"
    _git("commit", "-q", "-m", msg, "-m", body,
         "-m", "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>")
    return _git("rev-parse", "HEAD").stdout.strip()


def _tier_of(files):
    """Most-restrictive tier among the changed files. Tier-1 = the engine's own machinery +
    secrets + tests + schedule (never auto-edit); Tier-2 = submit-critical; else Tier-3."""
    cfg = _cfg()
    t1 = set(cfg.get("tier1_files", []))
    t2 = set(cfg.get("tier2_files", []))

    def is_t1(f):
        return (f in t1 or f.startswith("tests/") or f.endswith(".plist")
                or f in (".gitignore", ".env"))
    if any(is_t1(f) for f in files):
        return "tier1"
    if any(f in t2 for f in files):
        return "tier2"
    return "tier3"


def validate(changed):
    """Gate a fix before it can go live: compile the changed files + the whole tree,
    import-smoke the package, and run the committed unittest suite. Returns (ok, report)."""
    py = sys.executable
    pyfiles = [f for f in changed if f.endswith(".py")]
    steps = []
    if pyfiles:
        steps.append(("py_compile(changed)", [py, "-m", "py_compile", *pyfiles]))
    steps.append(("py_compile(tree)", [py, "-m", "py_compile", *[p.name for p in ROOT.glob("*.py")]]))
    steps.append(("import", [py, "-c", "import pipeline, supervisor, auto_apply, perception, "
                  "screening, tailor, cover_letter, telegram_bot, claude_runner, fixer, stats, "
                  "discover, score, apply_type, digest, session_check, browser"]))
    steps.append(("unittest", [py, "-m", "unittest", "discover", "-s", "tests"]))
    for name, cmd in steps:
        try:
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180)
        except Exception as ex:
            return (False, f"{name}: {type(ex).__name__}: {ex}")
        if r.returncode != 0:
            return (False, f"{name} failed:\n{(r.stdout + r.stderr)[-1500:]}")
    return (True, "ok")


def _coding_pass(e):
    prompt = (f"ROOT CAUSE:\n{e.get('root_cause', '')}\n\n"
              f"PROPOSED FIX:\n{e.get('proposed_fix', '')}\n\n"
              "Apply the MINIMAL change that fixes this root cause. Edit only the file(s) "
              "strictly necessary. When done, print a one-line summary of what you changed.")
    try:
        return claude_runner.run_claude_p(
            prompt, ROOT, append_system_prompt=FIX_SYSTEM,
            timeout=int(_cfg().get("coding_pass_timeout", 300)), max_turns=30,
            permission_mode="acceptEdits", model="claude-opus-4-8")
    except Exception as ex:
        return {"returncode": 1, "stderr": f"{type(ex).__name__}: {ex}", "timed_out": False}


def _requeue(e):
    """After an applied fix: drop the job from wontfix and re-queue it so the next run
    retries with the fix in place. Direct JSON (no pipeline import -> no cycle)."""
    jid = str(e.get("job_id"))
    wf = _load(WONTFIX_FILE, {})
    info = wf.pop(jid, {}) if isinstance(wf, dict) else {}
    _save(WONTFIX_FILE, wf)
    pend = _load(PENDING_FILE, [])
    if not isinstance(pend, list):
        pend = []
    if not any(str(p.get("job_id")) == jid for p in pend):
        now = datetime.now().isoformat()
        pend.append({"job_id": jid, "title": e.get("title") or info.get("title", ""),
                     "company": info.get("company", ""), "url": info.get("url", ""),
                     "_attempts": 1, "_last_class": "transient", "_first_queued": now,
                     "_queued_at": now, "_last_detail": "code fix applied; retrying"})
        _save(PENDING_FILE, pend)


def _escalate(msg):
    print(f"[fixer] {msg}", flush=True)
    try:
        if tg.configured():
            tg.send_message(msg, parse_mode="")   # plain text: fix details may contain < & >
    except Exception:
        pass


# ── apply one approved fix (the engine core) ──────────────────────────────────
def run_one_fix(e, now):
    """Coding pass -> tier gate -> validation. With apply_enabled=false (5c) ALWAYS revert
    and only report. With apply_enabled=true (5d): Tier-3 commit + re-queue; Tier-2 capture
    the diff, revert, and send a diff-review card; Tier-1 reject. Never raises."""
    fid = e["fix_id"]
    title = str(e.get("title", ""))[:40]
    apply_enabled = bool(_cfg().get("apply_enabled", False))
    attempts = int(e.get("attempts", 0) or 0)
    if not _clean_tree():
        _escalate(f"⚠️ Auto-fix {fid}: working tree not clean — skipping (resolve by hand).")
        return

    _coding_pass(e)
    changed = _changed_files()

    def _dry_done(extra_detail=""):
        _revert()
        set_status(fid, e.get("status", "approved"), dry_checked=True,
                   tier=_tier_of(changed) if changed else None, changed_files=changed,
                   fail_detail=extra_detail or None)

    if not changed:
        if apply_enabled:
            _revert()
            set_status(fid, "failed", attempts=attempts + 1, fail_step="coding_pass",
                       fail_detail="no changes produced")
            _escalate(f"🛠️ Auto-fix {fid} ({title}): coding pass made no change — left for manual fix.")
        else:
            _dry_done("dry: coding pass made no change (claude may be unavailable here)")
            _escalate(f"🧪 DRY auto-fix {fid} ({title}): coding pass made no change.")
        return

    tier = _tier_of(changed)

    if tier == "tier1":
        _revert()
        if apply_enabled:
            set_status(fid, "failed", attempts=attempts + 1, tier="tier1",
                       changed_files=changed, fail_step="tier1_protected", fail_detail=str(changed))
        else:
            _dry_done()
        _escalate(f"🛑 Auto-fix {fid} ({title}) touched PROTECTED file(s) {changed} — "
                  "rejected + reverted. Apply by hand.")
        return

    ok, report = validate(changed)
    if not ok:
        _revert()
        if apply_enabled:
            set_status(fid, "failed", attempts=attempts + 1, tier=tier, changed_files=changed,
                       fail_step="validation", fail_detail=report[:1000])
        else:
            _dry_done("dry: would fail validation: " + report[:300])
        _escalate(f"❌ Auto-fix {fid} ({title}) failed validation — reverted. {report[:140]}")
        return

    # Validated. Decide by tier.
    if not apply_enabled:
        would = "go to diff-review (submit-critical)" if tier == "tier2" else "be applied + committed"
        _dry_done()
        _escalate(f"🧪 DRY auto-fix {fid} ({title}): validated; would {would}. "
                  f"Files {changed}. Reverted (apply_enabled=false).")
        return

    if tier == "tier2":
        patch = _capture_patch()
        _revert()
        import uuid
        nonce2 = uuid.uuid4().hex[:8]
        mid = None
        try:
            if tg.configured():
                mid = tg.send_diff_review_card(fid, nonce2, e.get("title", ""), patch)
        except Exception:
            pass
        set_status(fid, "awaiting_diff_review", attempts=attempts + 1, tier=tier,
                   changed_files=changed, patch=patch, nonce2=nonce2, diff_msg_id=mid)
        _escalate(f"🔬 Auto-fix {fid} ({title}) is submit-critical — sent you the diff to review "
                  "before it goes live.")
    else:  # tier3 -> commit live
        sha = _commit(changed, e)
        set_status(fid, "applied", attempts=attempts + 1, tier=tier, changed_files=changed, commit=sha)
        _requeue(e)
        _escalate(f"✅ Auto-fix {fid} ({title}) applied + committed ({sha[:8]}); job re-queued.")


def _apply_approved_diff(e, now):
    """5d: the user approved a submit-critical fix's diff -> re-apply the captured patch,
    re-validate, commit, re-queue. Never raises."""
    fid = e["fix_id"]
    title = str(e.get("title", ""))[:40]
    attempts = int(e.get("attempts", 0) or 0)
    if not _clean_tree():
        _escalate(f"⚠️ Diff-approved fix {fid}: tree not clean — skipping.")
        return
    proc = subprocess.run(["git", "-C", str(ROOT), "apply", "--whitespace=nowarn"],
                          input=e.get("patch") or "", capture_output=True, text=True)
    if proc.returncode != 0:
        _revert()
        set_status(fid, "failed", attempts=attempts + 1, fail_step="git_apply",
                   fail_detail=proc.stderr[:500])
        _escalate(f"❌ Diff-approved fix {fid} ({title}): patch no longer applies cleanly — skipped.")
        return
    changed = _changed_files()
    ok, report = validate(changed)
    if not ok:
        _revert()
        set_status(fid, "failed", attempts=attempts + 1, fail_step="validation", fail_detail=report[:1000])
        _escalate(f"❌ Diff-approved fix {fid} ({title}): validation failed after apply — reverted.")
        return
    sha = _commit(changed, e)
    set_status(fid, "applied", attempts=attempts + 1, commit=sha, changed_files=changed)
    _requeue(e)
    _escalate(f"✅ Submit-critical fix {fid} ({title}) applied after your diff review ({sha[:8]}); job re-queued.")


# ── harvest (runs first in pipeline.main(), before any other Telegram poll) ───
def harvest_and_run(now=None):
    """Drain fix-approval taps, move the ledger, then run approved fixes (capped). 5c:
    apply_enabled=false -> every fix is dry-checked + reverted, nothing committed. Best-effort."""
    try:
        _harvest(now or datetime.now())
    except Exception as ex:
        print(f"[fixer] harvest error: {type(ex).__name__}: {ex}", flush=True)


def _harvest(now):
    led = load_ledger()
    pending = {fid: {"job_id": e["job_id"], "nonce": e.get("nonce")}
               for fid, e in led.items() if e.get("status") == "proposed"}
    awaiting = {fid: {"job_id": e["job_id"], "nonce": e.get("nonce2")}
                for fid, e in led.items() if e.get("status") == "awaiting_diff_review"}
    if (pending or awaiting) and tg.configured():
        poll = int(_cfg().get("harvest_poll_seconds", 20))
        for fid, action in tg.harvest_fix_decisions({**pending, **awaiting}, timeout=poll).items():
            led_e = load_ledger().get(fid, {})
            st = led_e.get("status")
            stamp = now.isoformat(timespec="seconds")
            if st == "proposed":
                tg.disable_card_buttons(led_e.get("card_msg_id"))   # acted on -> kill its buttons
                set_status(fid, "approved" if action == "approve" else "rejected",
                           approved_at=stamp if action == "approve" else None)
            elif st == "awaiting_diff_review":
                tg.disable_card_buttons(led_e.get("diff_msg_id"))
                set_status(fid, "diff_approved" if action == "approve" else "diff_rejected",
                           approved_at=stamp if action == "approve" else None)
    _run_approved(now)


def _run_approved(now):
    apply_enabled = bool(_cfg().get("apply_enabled", False))
    cap = int(_cfg().get("fix_cap_per_run", 1))
    runnable = []
    for fid, e in load_ledger().items():
        st, att = e.get("status"), int(e.get("attempts", 0) or 0)
        if st == "approved":
            if not apply_enabled and e.get("dry_checked"):
                continue                      # already observed in dry mode
            if apply_enabled and att >= 1:
                continue                      # already had its one live attempt
            runnable.append(fid)
        elif st == "diff_approved" and apply_enabled and att < 2:
            runnable.append(fid)
    for fid in runnable[:cap]:
        e = load_ledger().get(fid)
        if not e:
            continue
        if e.get("status") == "diff_approved":
            _apply_approved_diff(e, now)
        else:
            run_one_fix(e, now)
