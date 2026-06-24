#!/usr/bin/env python3
"""
Telegram approval channel for the Handshake auto-apply pipeline.

Adapted from nuworks_autoapply/telegram_bot.py. Reads TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID from ./.env (the DEDICATED 4th Handshake bot — separate from the
nuworks/wellfound/bridge bots). Sends the tailored resume PDF with the proposed
screening answers and Approve/Reject buttons, then polls getUpdates (nonce-guarded)
until the user taps one.

Setup (one time):
  1. Telegram -> @BotFather -> /newbot -> copy the token
  2. Open the new bot, tap Start
  3. Save TELEGRAM_BOT_TOKEN (+ TELEGRAM_CHAT_ID) into ./.env
"""
import os
import re
import json
import time
import html
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"


def configured() -> bool:
    return bool(TOKEN and CHAT_ID)


def _require():
    if not configured():
        raise RuntimeError(
            "Telegram not configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env"
        )


def send_message(text: str, parse_mode: str = "HTML") -> dict:
    _require()
    r = requests.post(f"{API}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
                      timeout=30)
    return r.json()


def _fmt_pay(salary: str) -> str:
    s = (salary or "").strip()
    if not s:
        return "Not listed"
    m = re.match(r'^\$?([\d,.]+)\s*[–-]\s*\$?([\d,.]+)\s*(.*)$', s)
    if m:
        lo, hi, rest = m.group(1), m.group(2), m.group(3).strip()
        return (f"${lo} {rest}".strip() if lo == hi else f"${lo}–${hi} {rest}".strip())
    return s


# Source -> marker for the approval card. profile = deterministic (safe);
# claude = drafted (review); none = unanswerable (blocks auto-submit if required).
_SRC_MARK = {"profile": "", "claude": "🟡 ", "none": "🔴 "}


def _application_caption(job: dict, answers=None, summary: str = "",
                         preference_mismatch: bool = False, has_cover: bool = False,
                         has_transcript: bool = False) -> str:
    """Build the approval-card caption (factored out so it's unit-testable without
    sending). Telegram caption hard-limit is 1024 chars — we truncate to fit."""
    cap = (
        f"📄 <b>Handshake application ready</b>\n\n"
        f"<b>{html.escape(job.get('title','')[:90])}</b>\n"
        f"{html.escape(job.get('company',''))} — {html.escape(job.get('location',''))}\n"
        f"🧰 {html.escape(job.get('type',''))}   💵 {html.escape(_fmt_pay(job.get('salary','')))}\n"
        f"🗓 Deadline: {html.escape(str(job.get('deadline') or '—'))}\n"
    )
    if preference_mismatch:
        cap += "⚠️ You don't meet all employer preferences (can still apply).\n"
    if has_cover:
        cap += "✍️ Tailored cover letter attached below (review it too).\n"
    if has_transcript:
        cap += "📑 Transcript will be attached (this employer requires one).\n"
    if answers:
        cap += "\n<b>Screening answers I'll submit:</b>\n"
        for a in answers:
            mark = _SRC_MARK.get(a.get("source"), "")
            q = html.escape(str(a.get("question", ""))[:70])
            ans = html.escape(str(a.get("answer") or "—")[:70])
            cap += f"• {mark}{q} → {ans}\n"
        if any(a.get("source") == "none" and a.get("required") for a in answers):
            cap += "🔴 A required question is unanswered — I will NOT auto-submit; finish it manually.\n"
    if summary:
        cap += f"\n{html.escape(summary[:200])}"
    cap += "\n\nApprove to auto-submit on Handshake, or Reject to skip."
    return cap[:1024]


def send_application_for_approval(pdf_path: str, job: dict, answers=None, summary: str = "",
                                  nonce: str = "", preference_mismatch: bool = False,
                                  cover_pdf: str = None, has_transcript: bool = False) -> int:
    """Send the tailored resume PDF (+ the tailored cover letter PDF, if any) + proposed
    screening answers with Approve/Reject buttons. callback_data = decision:job_id:nonce
    (nonce guards against a stale tap auto-approving a card the user never reviewed)."""
    _require()
    jid = str(job["job_id"])
    caption = _application_caption(job, answers, summary, preference_mismatch,
                                   has_cover=bool(cover_pdf), has_transcript=has_transcript)
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve & Submit", "callback_data": f"approve:{jid}:{nonce}"},
        {"text": "❌ Reject", "callback_data": f"reject:{jid}:{nonce}"},
    ]]}
    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML",
                  "reply_markup": json.dumps(keyboard)},
            files={"document": (Path(pdf_path).name, f, "application/pdf")},
            timeout=60,
        )
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed: {res}")
    # Follow the resume card with the cover-letter PDF (no buttons; the approval tap on
    # the resume card covers the whole application — resume + cover letter).
    if cover_pdf:
        try:
            with open(cover_pdf, "rb") as cf:
                requests.post(
                    f"{API}/sendDocument",
                    data={"chat_id": CHAT_ID,
                          "caption": f"✍️ Tailored cover letter — {html.escape(job.get('title','')[:70])}"},
                    files={"document": (Path(cover_pdf).name, cf, "application/pdf")},
                    timeout=60,
                )
        except Exception:
            pass
    return res["result"]["message_id"]


def send_cover_letter_for_approval(pdf_path: str, job: dict, nonce: str = "") -> int:
    """Send a generated cover-letter PDF with its OWN Approve/Reject buttons, so a
    required cover letter is never submitted sight-unseen."""
    _require()
    jid = str(job["job_id"])
    caption = (
        f"✍️ <b>Cover letter ready — review before it's sent</b>\n\n"
        f"<b>{html.escape(job.get('title','')[:80])}</b>\n{html.escape(job.get('company',''))}\n\n"
        "This job REQUIRES a cover letter. Approve to attach + submit it with your resume, "
        "or Reject to skip this application."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve letter", "callback_data": f"approve:{jid}:{nonce}"},
        {"text": "❌ Reject", "callback_data": f"reject:{jid}:{nonce}"},
    ]]}
    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML",
                  "reply_markup": json.dumps(keyboard)},
            files={"document": (Path(pdf_path).name, f, "application/pdf")},
            timeout=60,
        )
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendDocument (cover letter) failed: {res}")
    return res["result"]["message_id"]


def _commit_offset(offset):
    """Confirm processed updates so a decided tap isn't replayed by a later poll."""
    if offset is None:
        return
    try:
        requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=20)
    except requests.RequestException:
        pass


def wait_for_decision(job_id: str, nonce: str = "", timeout: int = 3600, poll: int = 3) -> str:
    """Poll getUpdates until the user taps Approve/Reject for this job_id + nonce.
    Returns 'approve', 'reject', or 'timeout'. Acks every tap so spinners clear; a tap
    for another job or a stale nonce is acked + ignored."""
    _require()
    deadline = time.time() + timeout
    offset = None
    while time.time() < deadline:
        params = {"timeout": 25}
        if offset is not None:
            params["offset"] = offset
        try:
            r = requests.get(f"{API}/getUpdates", params=params, timeout=40).json()
        except requests.RequestException:
            time.sleep(poll)
            continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq:
                continue
            parts = cq.get("data", "").split(":")
            if len(parts) < 2:
                continue
            action, jid = parts[0], parts[1]
            n = parts[2] if len(parts) > 2 else ""
            if jid != job_id:
                requests.post(f"{API}/answerCallbackQuery",
                              data={"callback_query_id": cq["id"], "text": "This card is no longer active."},
                              timeout=20)
                continue
            if nonce and n != nonce:
                requests.post(f"{API}/answerCallbackQuery",
                              data={"callback_query_id": cq["id"], "text": "This card expired — see the latest one."},
                              timeout=20)
                continue
            requests.post(f"{API}/answerCallbackQuery",
                          data={"callback_query_id": cq["id"],
                                "text": "Approved ✅" if action == "approve" else "Rejected ❌"},
                          timeout=20)
            _commit_offset(offset)
            return action
        time.sleep(poll)
    _commit_offset(offset)
    return "timeout"


def collect_decisions(pending: dict, timeout: int = 3600, poll: int = 3, on_decision=None) -> list:
    """Batch sibling of wait_for_decision: when MANY approval cards are sent at once,
    poll getUpdates and route each Approve/Reject tap to the right card by job_id+nonce.

    pending: {job_id: nonce} for cards already sent. As each tap arrives it's acked and
    on_decision(job_id, action) is called (so the caller can submit immediately, in tap
    order). A tap for an unknown/stale card is acked + ignored. Returns the list of
    job_ids that never got a tap before `timeout` (caller treats them as timed out).
    on_decision must not raise (the caller makes it bulletproof)."""
    _require()
    pending = dict(pending)
    deadline = time.time() + timeout
    offset = None
    while pending and time.time() < deadline:
        params = {"timeout": 25}
        if offset is not None:
            params["offset"] = offset
        try:
            r = requests.get(f"{API}/getUpdates", params=params, timeout=40).json()
        except requests.RequestException:
            time.sleep(poll)
            continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq:
                continue
            parts = cq.get("data", "").split(":")
            if len(parts) < 2:
                continue
            action, jid = parts[0], parts[1]
            n = parts[2] if len(parts) > 2 else ""
            if jid not in pending or (pending[jid] and n != pending[jid]):
                requests.post(f"{API}/answerCallbackQuery",
                              data={"callback_query_id": cq["id"], "text": "This card is no longer active."},
                              timeout=20)
                continue
            requests.post(f"{API}/answerCallbackQuery",
                          data={"callback_query_id": cq["id"],
                                "text": "Approved ✅" if action == "approve" else "Rejected ❌"},
                          timeout=20)
            del pending[jid]
            _commit_offset(offset)
            if on_decision:
                on_decision(jid, action)
        time.sleep(poll)
    _commit_offset(offset)
    return list(pending.keys())


def send_fix_approval_card(fix_id: str, nonce: str, title: str, root_cause: str = "",
                           proposed_fix: str = "", confidence=None) -> dict:
    """Phase 5: a one-tap approval card for an autonomously-diagnosed CODE fix. Approve
    lets the engine apply it (validated; submit-critical changes get a 2nd diff review);
    Reject skips it. callback_data prefix encodes the choice: fix_approve / fix_reject."""
    _require()
    conf = f" · confidence {confidence}" if confidence is not None else ""
    text = (f"🛠️ <b>Auto-fix proposed</b>{conf}\n\n"
            f"<b>{html.escape(str(title)[:80])}</b>\n"
            f"<b>Cause:</b> {html.escape(str(root_cause)[:400])}\n"
            f"<b>Proposed fix:</b> {html.escape(str(proposed_fix)[:600])}\n\n"
            "Approve to let the engine apply this (it validates first; submit-critical code "
            "comes back for a diff review). Reject to skip.")
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve fix", "callback_data": f"fix_approve:{fix_id}:{nonce}"},
        {"text": "❌ Reject", "callback_data": f"fix_reject:{fix_id}:{nonce}"},
    ]]}
    r = requests.post(f"{API}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text[:4096], "parse_mode": "HTML",
                            "reply_markup": json.dumps(keyboard)}, timeout=30)
    res = r.json()
    return res.get("result", {}).get("message_id") if res.get("ok") else None


def send_diff_review_card(fix_id: str, nonce: str, title: str, diff: str) -> dict:
    """Phase 5 (diff-review): for a submit-critical fix, show the exact code diff and ask
    for a 2nd one-tap approval before it goes live. callback prefix diff_approve / diff_reject."""
    _require()
    body = diff if len(diff) <= 3200 else (diff[:3200] + "\n... (truncated; full diff in the commit)")
    text = (f"🔬 <b>Submit-critical fix — review the diff</b>\n\n"
            f"<b>{html.escape(str(title)[:80])}</b>\n"
            f"<pre>{html.escape(body)}</pre>\n"
            "Approve to apply this exact change, or Reject to skip.")
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve diff", "callback_data": f"diff_approve:{fix_id}:{nonce}"},
        {"text": "❌ Reject", "callback_data": f"diff_reject:{fix_id}:{nonce}"},
    ]]}
    r = requests.post(f"{API}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text[:4096], "parse_mode": "HTML",
                            "reply_markup": json.dumps(keyboard)}, timeout=30)
    res = r.json()
    return res.get("result", {}).get("message_id") if res.get("ok") else None


def disable_card_buttons(message_id) -> None:
    """Strip the inline keyboard off a previously-sent card so it can no longer be tapped.
    Called once a card has been acted on / superseded — removes the stale-card trap where
    an old card's buttons stay live and tapping them is silently ignored. Best-effort."""
    if not message_id:
        return
    try:
        requests.post(f"{API}/editMessageReplyMarkup",
                      data={"chat_id": CHAT_ID, "message_id": message_id,
                            "reply_markup": json.dumps({"inline_keyboard": []})}, timeout=20)
    except Exception:
        pass


def harvest_fix_decisions(pending: dict, timeout: int = 20, poll: int = 3) -> dict:
    """Short drain of getUpdates for fix-approval taps (fix_approve/fix_reject/diff_approve/
    diff_reject). `pending`: {fix_id: {"nonce": ...}}. Returns {fix_id: "approve"|"reject"}.
    Acks every tap; acks + ignores non-fix taps. MUST run before the app-approval poll
    (collect_decisions) so the two getUpdates loops never overlap (the documented 409)."""
    _require()
    out = {}
    if not pending:
        return out
    deadline = time.time() + timeout
    offset = None
    FIX_ACTIONS = ("fix_approve", "fix_reject", "diff_approve", "diff_reject")
    while time.time() < deadline:
        params = {"timeout": poll}
        if offset is not None:
            params["offset"] = offset
        try:
            r = requests.get(f"{API}/getUpdates", params=params, timeout=poll + 15).json()
        except requests.RequestException:
            time.sleep(1)
            continue
        updates = r.get("result", [])
        for upd in updates:
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq:
                continue
            parts = (cq.get("data", "") or "").split(":")
            act = parts[0]
            is_fix = act in FIX_ACTIONS and len(parts) >= 3
            try:
                requests.post(f"{API}/answerCallbackQuery",
                              data={"callback_query_id": cq["id"],
                                    "text": ("Got it ✅" if "approve" in act else "Skipped ❌")
                                            if is_fix else "No longer active."}, timeout=20)
            except requests.RequestException:
                pass
            if is_fix:
                fix_id, n = parts[1], parts[2]
                p = pending.get(fix_id)
                if p and (not p.get("nonce") or p.get("nonce") == n):
                    out[fix_id] = "approve" if "approve" in act else "reject"
        if not updates:
            break
    _commit_offset(offset)
    return out


if __name__ == "__main__":
    # Smoke test: confirms the 4th-bot creds work and sends a hello message.
    if not configured():
        print("Telegram NOT configured. Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to .env "
              "(the dedicated Handshake bot).")
    else:
        print(send_message("✅ Handshake auto-apply bot connected."))
