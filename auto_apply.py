#!/usr/bin/env python3
"""Handshake in-portal apply state machine (adapted from nuworks_autoapply/auto_apply.py).

apply_to_job(job, resume_pdf, answers=None, mode="dry"):
  1. Put the per-job TAILORED resume into ONE reusable PRIVATE slot doc in /docs
     (replace its file; don't upload a new doc each time — Handshake has no delete UI
     so per-job uploads pile up forever). Falls back to a fresh upload if the slot
     can't be edited. The modal auto-attaches the most-recent doc, so when using the
     slot we explicitly remove that and attach the slot via the resume picker.
  2. Open the job -> double-apply guard -> click visible Apply -> wait for the
     apply dialog (the one containing "Submit Application"; NOT the app-promo).
  3. FAIL-SAFE: verify the modal's attached resume is the tailored one; if not,
     BLOCK (blocked_wrong_resume) — never submit the wrong (default) resume.
  4. Fill screening questions from the approved `answers`.
  5. Readiness gate: only proceed if "Submit Application" is ENABLED (a disabled
     button = a required document/question is missing -> blocked_missing_documents).
  6. mode="dry" screenshots + closes BEFORE submit; mode="submit" clicks + verifies.

Statuses: staged, applied, already_applied, submitted_unverified, no_inportal_apply,
blocked_wrong_resume, blocked_cover_letter, blocked_missing_documents,
blocked_unanswered_question, blocked_unmet_requirement, challenge_abort, error.

CLI (testing): python3 auto_apply.py <job_id_or_url> <resume_pdf> [--submit]
"""
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import browser
import perception
import screening

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "logs" / "apply_screenshots"
SUBMIT_TEXT = "Submit Application"

# The single reusable "AutoApply" resume slot in /docs (My Documents). Each run we
# REPLACE this doc's file + rename it (instead of uploading a new doc per job, which
# pile up with no delete UI), then explicitly attach it in the apply modal.
SLOT_DOC_ID = "71548437"

# The resume react-select is the combobox between the "Attach your resume" and the
# "Attach your cover letter" headers (both sections render identical pickers).
FIND_RESUME_CB = r"""(root)=>{
  const h4=[...root.querySelectorAll('h4')];
  const r=h4.find(h=>/attach your resume/i.test(h.innerText));
  const c=h4.find(h=>/cover letter/i.test(h.innerText));
  for(const cb of root.querySelectorAll('input[role=combobox]')){
    const afterR=r && (r.compareDocumentPosition(cb)&Node.DOCUMENT_POSITION_FOLLOWING);
    const beforeC=!c || (c.compareDocumentPosition(cb)&Node.DOCUMENT_POSITION_PRECEDING);
    if(afterR && beforeC) return cb;
  }
  return root.querySelector('input[role=combobox]');
}"""

# A second reusable slot for COVER LETTERS (repurposed stray test doc). Same edit/
# attach mechanics as the resume slot, but documentTypeId = Cover Letter.
CL_SLOT_DOC_ID = "71548396"

# The cover-letter react-select is the combobox AFTER the 'Attach your cover letter' h4.
FIND_CL_CB = r"""(root)=>{
  const h4=[...root.querySelectorAll('h4')];
  const c=h4.find(h=>/cover letter/i.test(h.innerText));
  if(!c) return null;
  for(const cb of root.querySelectorAll('input[role=combobox]')){
    if(c.compareDocumentPosition(cb)&Node.DOCUMENT_POSITION_FOLLOWING) return cb;
  }
  return null;
}"""

# The transcript react-select is the combobox AFTER the 'Attach your transcript' h4.
FIND_TRANSCRIPT_CB = r"""(root)=>{
  const h4=[...root.querySelectorAll('h4')];
  const tr=h4.find(h=>/attach your transcript/i.test(h.innerText));
  if(!tr) return null;
  for(const cb of root.querySelectorAll('input[role=combobox]')){
    if(tr.compareDocumentPosition(cb)&Node.DOCUMENT_POSITION_FOLLOWING) return cb;
  }
  return null;
}"""


def _apply_modal(page):
    """The apply dialog (filtering out the 'It's better on the app' promo dialog)."""
    for d in page.query_selector_all('[role="dialog"]'):
        try:
            if d.is_visible() and SUBMIT_TEXT in d.inner_text():
                return d
        except Exception:
            pass
    return None


def _click_visible_apply(page):
    for e in page.query_selector_all("button"):
        try:
            if e.is_visible() and (e.inner_text() or "").strip().lower() == "apply":
                e.click()
                return True
        except Exception:
            pass
    return False


def _visible_low(page):
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


def upload_resume_to_docs(page, pdf_path, name, doc_type="resume"):
    """Upload a (tailored) resume to My Documents as PRIVATE (not visible to
    employers). Returns True if it appears saved. The apply modal then attaches
    the most-recent resume — verified in-modal as a fail-safe by the caller."""
    try:
        page.goto(f"{browser.BASE_URL}/docs", wait_until="domcontentloaded")
        time.sleep(3)
        btn = page.query_selector('text=/upload new document/i')
        if not btn:
            return False
        btn.click()
        time.sleep(2)
        fi = page.query_selector('input[type=file][name="file"]') or page.query_selector('input[type=file]')
        if not fi:
            return False
        fi.set_input_files(pdf_path)
        time.sleep(1)
        # Name field
        try:
            page.get_by_label("Name", exact=True).fill(name)
        except Exception:
            for inp in page.query_selector_all('input[type=text]'):
                try:
                    if inp.is_visible() and not inp.input_value():
                        inp.fill(name)
                        break
                except Exception:
                    pass
        # Document Type: defaults to Resume; set it explicitly for non-resume docs
        # (e.g. a transcript -> documentTypeId 3) so it lands in the right picker.
        type_val = {"resume": "1", "cover": "2", "transcript": "3"}.get(doc_type)
        if type_val and type_val != "1":
            try:
                page.select_option('select[name="documentTypeId"]', value=type_val)
            except Exception:
                pass
        # Keep PRIVATE: uncheck "Visible to employers".
        try:
            cb = page.get_by_label("Visible to employers")
            if cb.is_checked():
                cb.uncheck()
        except Exception:
            pass
        save = page.query_selector('button:has-text("Save")')
        if save:
            save.click()
        time.sleep(4)
        return name.lower() in page.inner_text("body").lower()
    except Exception:
        return False


_transcript_ensured = False


def ensure_transcript_doc(page):
    """Ensure a static 'Transcript' doc exists in /docs so the transcript picker can
    attach it (the modal uses a doc react-select, not a file input). The transcript is
    static, so this is a one-time upload; afterwards it's a cheap existence check. Runs
    at most once per pipeline run. A failure degrades safely — the transcript simply
    won't attach and the existing transcript_ok gate blocks the submit."""
    global _transcript_ensured
    if _transcript_ensured:
        return True
    tpath = HERE / "documents" / "transcript.pdf"
    if not tpath.exists():
        return False
    try:
        page.goto(f"{browser.BASE_URL}/docs", wait_until="domcontentloaded")
        time.sleep(3)
        if "transcript" in (page.inner_text("body") or "").lower():
            _transcript_ensured = True
            return True
        ok = upload_resume_to_docs(page, str(tpath), "Transcript", doc_type="transcript")
        _transcript_ensured = bool(ok)
        return _transcript_ensured
    except Exception:
        return False


def _edit_form_ready(page):
    return page.query_selector('input[type=file][name="file"]')


def update_slot_resume(page, pdf_path, slot_name, slot_id=SLOT_DOC_ID, doc_type="resume"):
    """Replace a reuse slot doc's FILE and rename it to slot_name, keeping it PRIVATE —
    instead of uploading a new /docs entry per job. doc_type selects the documentTypeId
    ("resume" or "cover"). Returns True if saved.

    The edit FORM (with the file input) is NOT shown by a direct /docs/<id> visit, so
    we try /docs/<id>/edit, then fall back to the docs list -> Actions -> Edit path."""
    try:
        page.goto(f"{browser.BASE_URL}/docs/{slot_id}/edit", wait_until="domcontentloaded")
        time.sleep(3)
        if not _edit_form_ready(page):
            page.goto(f"{browser.BASE_URL}/docs", wait_until="domcontentloaded")
            time.sleep(4)
            link = page.query_selector(f'a[href*="/docs/{slot_id}"]')
            if link:
                link.click()
                time.sleep(3)
            for e in page.query_selector_all("button, a"):
                try:
                    if e.is_visible() and "actions" in (e.inner_text() or "").strip().lower():
                        e.click()
                        time.sleep(1.5)
                        break
                except Exception:
                    pass
            for e in page.query_selector_all("button, a, [role=menuitem]"):
                try:
                    if e.is_visible() and (e.inner_text() or "").strip().lower() == "edit":
                        e.click()
                        time.sleep(3)
                        break
                except Exception:
                    pass
        if not _edit_form_ready(page):
            return False
        page.set_input_files('input[type=file][name="file"]', pdf_path)
        time.sleep(2)
        nm = page.query_selector('input[name="name"]')
        if nm:
            nm.fill(slot_name)
        pub = page.query_selector('input[type=checkbox][name="public"]')
        if pub and pub.is_checked():
            pub.uncheck()
        # Handshake's documentTypeId options are numeric ids (1=Resume, 2=Cover Letter,
        # 3=Transcript); the labels are rendered by a custom widget, so match by id.
        type_val = {"resume": "1", "cover": "2", "transcript": "3"}.get(doc_type)
        sel = page.query_selector('select[name="documentTypeId"]')
        if sel and type_val:
            try:
                page.select_option('select[name="documentTypeId"]', value=type_val)
                sel.dispatch_event("change")
            except Exception:
                pass
        for e in page.query_selector_all("button, input[type=submit]"):
            try:
                t = (e.inner_text() or e.get_attribute("value") or "").strip().lower()
                if e.is_visible() and t in ("save", "update", "save document",
                                            "update document", "save changes"):
                    e.click()
                    break
            except Exception:
                pass
        time.sleep(5)
        return slot_name.lower() in page.inner_text("body").lower()
    except Exception:
        return False


def _pick_in_combobox(page, cb, query, match=None):
    """Open a react-select combobox, type `query` to filter, and click the option that
    contains `match` (defaults to `query`). Used for résumé/cover (match=job id) and
    transcript (match='transcript')."""
    match = str(match if match is not None else query)
    cb.click()
    time.sleep(1.2)
    page.keyboard.type(str(query), delay=45)
    time.sleep(2.5)
    try:
        page.get_by_role("option", name=re.compile(re.escape(match), re.I)).first.click(timeout=6000)
    except Exception:
        opt = next((o for o in page.query_selector_all("[role=option]")
                    if o.is_visible() and match.lower() in (o.inner_text() or "").lower()), None)
        if not opt:
            return False
        bb = opt.bounding_box()
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    time.sleep(4)
    return True


def _attach_slot_in_modal(page, modal, slot_name, jid):
    """RESUME: remove the modal's auto-attached default, then attach the reuse slot
    via the resume react-select. The typeahead filters by document NAME, so type the
    slot's filename (slot_name carries the job id) and click the matching option — typing
    the bare job id never surfaces the doc, so nothing gets attached. Returns True if tried."""
    try:
        for b in modal.query_selector_all('button[aria-label="Close"]'):
            if b.is_visible():
                b.click()
                break
        time.sleep(2.5)
        cb = modal.evaluate_handle(FIND_RESUME_CB).as_element()
        return bool(cb) and _pick_in_combobox(page, cb, slot_name, match=jid)
    except Exception:
        return False


def _attach_cl_in_modal(page, modal, jid):
    """COVER LETTER: the CL section starts empty, so just attach the CL slot via the
    cover-letter react-select (the combobox after the 'Attach your cover letter' h4)."""
    try:
        cb = modal.evaluate_handle(FIND_CL_CB).as_element()
        return bool(cb) and _pick_in_combobox(page, cb, jid)
    except Exception:
        return False


def _attach_transcript_in_modal(page, modal):
    """TRANSCRIPT: attach Dev's EXISTING transcript (a static doc named 'Transcript' in
    /docs) via the transcript react-select. Some employers require it on submit even
    though Submit shows enabled. Returns True if tried."""
    try:
        cb = modal.evaluate_handle(FIND_TRANSCRIPT_CB).as_element()
        return bool(cb) and _pick_in_combobox(page, cb, "transcript")
    except Exception:
        return False


def _attached_resume_name(modal):
    """The resume name the modal shows attached (the green box) — the line right
    under 'Attach your resume' (Handshake may omit the .pdf extension). Returns None
    when nothing is selected: the unselected picker shows the 'or' separator (between
    the typeahead and 'Upload new'), which must NOT be read back as a document name."""
    txt = modal.inner_text()
    m = re.search(r'attach your resume\s*\n\s*([^\n]+)', txt, re.I)
    if m:
        val = m.group(1).strip()
        if val.lower() not in ("or", "upload new", "preview document"):
            return val
    return None


def _attached_cover_name(modal):
    """The cover-letter name attached (the line under 'Attach your cover letter');
    None if the section is still empty (it shows 'or' / 'Upload new')."""
    m = re.search(r'attach your cover letter\s*\n\s*([^\n]+)', modal.inner_text(), re.I)
    if m:
        val = m.group(1).strip()
        if val.lower() not in ("or", "upload new", "preview document"):
            return val
    return None


def _attached_transcript_name(modal):
    """The transcript name attached (line under 'Attach your transcript'); None if empty."""
    m = re.search(r'attach your transcript\s*\n\s*([^\n]+)', modal.inner_text(), re.I)
    if m:
        val = m.group(1).strip()
        if val.lower() not in ("or", "upload new", "preview document"):
            return val
    return None


def _read_questions(modal):
    """Best-effort: read screening-question controls (selects + free-text) that
    aren't the resume/cover-letter doc pickers. Finalized against a live job that
    actually has questions."""
    qs = []
    for sel in modal.query_selector_all("select"):
        try:
            label = (sel.evaluate("el => (el.closest('label')?.innerText) || (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || ''") or "").strip()
            opts = [o.strip() for o in sel.evaluate("el => Array.from(el.options).map(o=>o.textContent)") if o.strip()]
            if label and "resume" not in label.lower() and "cover" not in label.lower():
                qs.append({"text": label, "type": "select", "options": opts, "required": True})
        except Exception:
            pass
    for ta in modal.query_selector_all("textarea"):
        try:
            ph = (ta.get_attribute("placeholder") or "").lower()
            if "cover" in ph or "search" in ph:
                continue
            label = (ta.evaluate("el => (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || el.placeholder || ''") or "").strip()
            if label:
                qs.append({"text": label, "type": "textarea", "options": [], "required": True})
        except Exception:
            pass
    return qs


def _fill_answers(modal, qa):
    """Type/select the approved answers into the modal's question controls.
    Best-effort by question-label proximity; finalized against a live questions job."""
    filled = []
    for a in qa:
        if a.get("source") == "none" or not a.get("answer"):
            continue
        q, ans, qtype = a["question"], str(a["answer"]), a.get("type", "")
        try:
            lab = modal.query_selector(f'text=/{re.escape(q[:40])}/i')
            holder = lab.evaluate_handle("el => el.closest('div,fieldset,li,label,section') || el.parentElement").as_element() if lab else None
            scope = holder or modal
            if qtype in ("select", "dropdown"):
                s = scope.query_selector("select")
                if s:
                    s.select_option(label=ans); filled.append(q[:25]); continue
            if qtype in ("textarea", "long_text", "paragraph"):
                t = scope.query_selector("textarea")
                if t:
                    t.fill(ans); filled.append(q[:25]); continue
            # radio / yes-no / single choice
            opt = scope.query_selector(f'text=/^\\s*{re.escape(ans)}\\s*$/i')
            if opt:
                opt.click(); filled.append(q[:25]); continue
            ti = scope.query_selector('input[type=text], textarea')
            if ti:
                ti.fill(ans); filled.append(q[:25])
        except Exception:
            pass
    return filled


def _close(page):
    for e in page.query_selector_all("button"):
        try:
            if e.is_visible() and (e.inner_text() or "").strip().lower() in ("cancel", "close"):
                e.click()
                return
        except Exception:
            pass
    page.keyboard.press("Escape")


def _dismiss_visibility_popup(page):
    """After submit, Handshake may pop 'Make your resume visible to other employers'
    (Save would make the tailored resume PUBLIC). Close it WITHOUT saving."""
    try:
        for d in page.query_selector_all('[role="dialog"]'):
            t = (d.inner_text() or "").lower()
            if d.is_visible() and "visible to" in t and "employer" in t:
                x = d.query_selector('button[aria-label*="close" i]')
                if x:
                    x.click()
                else:
                    page.keyboard.press("Escape")
                return True
    except Exception:
        pass
    return False


def _verify_submitted(page):
    """Robustly confirm a submit registered: poll (~22s) for the apply modal to CLOSE and
    the job to show an applied state (Withdraw/Applied) or a submitted toast. Also dismisses
    the post-submit 'make resume visible' popup (keeps the résumé private). Returns True
    only if confirmed — replaces the old single 4-7s body-text check that gave false
    'unverified' (and missed that some jobs simply didn't submit, e.g. missing transcript)."""
    for _ in range(11):
        browser.human_pause(2, 2.5)
        _dismiss_visibility_popup(page)
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            body = ""
        toast = any(s in body for s in ("application submitted", "successfully submitted",
                                        "has been submitted", "application has been sent",
                                        "you've applied", "you have applied"))
        modal_gone = _apply_modal(page) is None
        low = _visible_low(page)
        applied_state = any("withdraw" in t for t in low) or any(t == "applied" for t in low)
        if toast or (modal_gone and applied_state):
            return True
    return False


def apply_to_job(job, resume_pdf, answers=None, mode="dry", approved=False, cover_pdf=None, audit=True):
    resume_pdf = str(Path(resume_pdf).resolve())
    if not Path(resume_pdf).exists():
        return {"status": "error", "detail": f"resume not found: {resume_pdf}"}
    SHOTS.mkdir(parents=True, exist_ok=True)
    jid = str(job.get("job_id"))
    url = job.get("url") or f"{browser.BASE_URL}/jobs/{jid}"
    resume_name = Path(resume_pdf).stem            # name to verify in the modal
    challenge, on_resp = browser.make_challenge_listener()

    with sync_playwright() as p:
        br, ctx = browser.launch_authed(p)
        page = ctx.new_page()
        page.on("response", on_resp)
        try:
            # 1) Put the tailored resume into the single reusable slot (replace its
            #    file). Fall back to a fresh upload if the slot can't be edited.
            slot_name = f"{resume_name}_{jid}"
            use_slot = update_slot_resume(page, resume_pdf, slot_name)
            up_ok = use_slot or upload_resume_to_docs(page, resume_pdf, resume_name)

            # Cover letter: put the tailored letter into the CL reuse slot (if provided).
            cover_name = None
            use_cl = False
            if cover_pdf:
                cover_pdf = str(Path(cover_pdf).resolve())
                cover_name = f"{Path(cover_pdf).stem}_{jid}"
                use_cl = update_slot_resume(page, cover_pdf, cover_name,
                                            slot_id=CL_SLOT_DOC_ID, doc_type="cover")

            # Ensure the static transcript doc exists in /docs (once per run) so jobs
            # that require one can attach it. No-op after the first time this run.
            ensure_transcript_doc(page)

            # 2) Open the job.
            page.goto(url, wait_until="domcontentloaded")
            browser.human_pause(3, 5)
            if challenge["hit"]:
                br.close()
                return {"status": "challenge_abort", "detail": challenge["detail"]}
            # The apply controls can render slowly (especially after the /docs slot edits),
            # so poll for Apply OR Withdraw before deciding — a slow load must not be misread
            # as 'external/closed'.
            low = _visible_low(page)
            for _ in range(6):
                if (any("withdraw" in t for t in low) or any(t == "applied" for t in low)
                        or any(t == "apply" for t in low)):
                    break
                browser.human_pause(2, 3)
                low = _visible_low(page)
            if any("withdraw" in t for t in low) or any(t == "applied" for t in low):
                br.close()
                return {"status": "already_applied", "detail": "Withdraw/Applied shown on the job."}
            if not _click_visible_apply(page):
                br.close()
                return {"status": "no_inportal_apply", "detail": "No visible Apply button (external/closed)."}
            browser.human_pause(3, 5)
            modal = _apply_modal(page)
            if not modal:
                browser.shot(page, SHOTS, f"apply_{jid}_nomodal")
                br.close()
                return {"status": "no_inportal_apply", "detail": "Apply did not open the in-portal modal."}

            # The modal auto-attaches the most-recent doc, NOT our edited slot (editing
            # doesn't change creation order). When using the slot, explicitly remove the
            # default and attach the slot; the fail-safe below verifies it took.
            if use_slot:
                _attach_slot_in_modal(page, modal, slot_name, jid)
                browser.human_pause(1, 2)
            if cover_pdf and use_cl:
                _attach_cl_in_modal(page, modal, jid)
                browser.human_pause(1, 2)
            # Transcript: some employers require one — Submit shows enabled but silently
            # won't register without it. Attach Dev's existing transcript when present.
            has_transcript_section = bool(re.search(r"attach your transcript", modal.inner_text(), re.I))
            if has_transcript_section:
                _attach_transcript_in_modal(page, modal)
                browser.human_pause(1, 2)

            mtext = modal.inner_text()
            preference_mismatch = bool(re.search(r"do(es)?(n'?t| not) meet", mtext, re.I))
            has_cover_section = bool(re.search(r"attach your cover letter", mtext, re.I))
            transcript_attached = _attached_transcript_name(modal) if has_transcript_section else None
            transcript_ok = (not has_transcript_section) or bool(transcript_attached)
            attached = _attached_resume_name(modal)
            resume_ok = bool(attached and resume_name.lower() in attached.lower())
            if use_slot:
                # tie verification to THIS job: the slot's name carries the job id
                resume_ok = resume_ok and str(jid) in (attached or "")
            cover_attached = _attached_cover_name(modal) if cover_pdf else None
            cover_ok = (not cover_pdf) or bool(
                cover_attached and str(jid) in (cover_attached or "")
                and "cover" in (cover_attached or "").lower())

            questions = _read_questions(modal)
            qa = screening.answer_questions(questions, job) if questions else []
            blocker = screening.has_blocker(qa) if qa else False
            if qa:
                _fill_answers(modal, qa)

            shot = browser.shot(page, SHOTS, f"apply_{jid}_ready")
            base = {"answers": qa, "questions": [q["text"] for q in questions],
                    "preference_mismatch": preference_mismatch,
                    "attached_resume": attached, "upload_ok": up_ok, "used_slot": use_slot,
                    "attached_cover": cover_attached, "used_cl": use_cl,
                    "has_cover_section": has_cover_section, "cover_pdf": cover_pdf,
                    "has_transcript_section": has_transcript_section,
                    "attached_transcript": transcript_attached,
                    "screenshot": str(SHOTS / shot)}

            # 3) FAIL-SAFE: never submit if the tailored resume isn't the attached one.
            if not resume_ok:
                _close(page)
                br.close()
                return {"status": "blocked_wrong_resume",
                        "detail": f"Modal attached '{attached}', not the tailored '{resume_name}' "
                                  f"(docs upload_ok={up_ok}). NOT submitting.", **base}
            # FAIL-SAFE: if a cover letter was provided, it must be attached for THIS job.
            if cover_pdf and not cover_ok:
                _close(page)
                br.close()
                return {"status": "blocked_cover_letter",
                        "detail": f"Cover letter not attached as expected (got "
                                  f"'{cover_attached}'). NOT submitting.", **base}
            # FAIL-SAFE: a transcript section = the employer requires one; don't submit
            # unless our transcript attached (else the submit silently fails to register).
            if has_transcript_section and not transcript_ok:
                _close(page)
                br.close()
                return {"status": "blocked_missing_documents",
                        "detail": "Transcript section present but no transcript attached "
                                  "(need a 'Transcript' doc in /docs). NOT submitting.", **base}
            if blocker:
                _close(page)
                br.close()
                return {"status": "blocked_unanswered_question",
                        "detail": f"Required question(s) unanswered: "
                                  f"{[a['question'] for a in qa if a['source']=='none' and a['required']]}", **base}

            sb = modal.query_selector(f'button:has-text("{SUBMIT_TEXT}")')
            submit_enabled = bool(sb and sb.is_enabled())
            if not submit_enabled:
                browser.shot(page, SHOTS, f"apply_{jid}_blocked")
                _close(page)
                br.close()
                return {"status": "blocked_missing_documents",
                        "detail": "Submit Application is disabled — a required document (e.g. cover "
                                  "letter) or question is missing.", **base}

            if mode != "submit":
                _close(page)
                br.close()
                cl_note = f" + cover letter '{cover_attached}'" if cover_pdf else ""
                return {"status": "staged",
                        "detail": f"Tailored resume '{attached}'{cl_note} attached (private); "
                                  f"{len(questions)} question(s); Submit ready; STOPPED (dry).", **base}

            if not approved:
                _close(page)
                br.close()
                return {"status": "blocked_no_approval",
                        "detail": "Refusing to submit without prior approval — a real submit must go "
                                  "through the Telegram approval gate (approved=True).", **base}

            # PERCEPTION AUDIT — the irreversible gate. The deterministic checks above
            # only understand resume/cover/transcript/select/textarea; before the actual
            # Submit, the brain reads the WHOLE modal and confirms every required item is
            # satisfied. Satisfy-or-halt: an unmet requirement BLOCKS (never submit
            # blind). If the brain is unavailable it returns proceed -> today's behavior.
            if audit:
                decision = perception.audit_before_submit(page, modal, job, {
                    "resume_ok": resume_ok, "cover_ok": cover_ok,
                    "transcript_attached": transcript_attached,
                    "has_transcript_section": has_transcript_section,
                    "unanswered_required": [a["question"] for a in qa
                                            if a.get("source") == "none" and a.get("required")]})
                if decision.get("action") == "block":
                    try:
                        browser.shot(page, SHOTS, f"apply_{jid}_unmet")
                    except Exception:
                        pass
                    _close(page)
                    br.close()
                    return {"status": "blocked_unmet_requirement",
                            "detail": decision.get("detail", "an unmet submit requirement"),
                            "missing": decision.get("missing", []), **base}
            sb.click()
            ok = _verify_submitted(page)   # polls for modal-close + Withdraw/Applied (or toast)
            shot2 = browser.shot(page, SHOTS, f"apply_{jid}_result")
            base["screenshot"] = str(SHOTS / shot2)
            br.close()
            if ok:
                return {"status": "applied",
                        "detail": "Submitted; confirmed (modal closed + Withdraw/Applied state).", **base}
            return {"status": "submitted_unverified",
                    "detail": "Submit clicked but no applied confirmation within the wait; the "
                              "double-apply guard makes a retry a no-op if it actually went through.", **base}
        except Exception as e:
            try:
                browser.shot(page, SHOTS, f"apply_{jid}_error")
            except Exception:
                pass
            br.close()
            return {"status": "error", "detail": f"{type(e).__name__}: {e}"}


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 auto_apply.py <job_id_or_url> <resume_pdf> [--submit]")
        sys.exit(1)
    arg = sys.argv[1]
    job = {"job_id": arg.rstrip("/").split("/")[-1].split("?")[0],
           "url": arg if arg.startswith("http") else None}
    mode = "submit" if "--submit" in sys.argv else "dry"
    approved = "--i-approved" in sys.argv  # explicit human ack; real runs gate via pipeline.py
    print(f"Mode: {mode.upper()}  Job: {job['job_id']}  approved={approved}")
    res = apply_to_job(job, sys.argv[2], mode=mode, approved=approved)
    for k, v in res.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
