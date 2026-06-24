#!/usr/bin/env python3
"""Pre-submit PERCEPTION audit (Phase 2).

The apply state machine's deterministic checks only understand the things they were
hand-coded for: the resume/cover/transcript react-selects, <select>s and <textarea>s.
Anything else a form requires — a differently-worded required upload, an agreement
checkbox, a radio group, a second document — is invisible, so the bot can click Submit
with an unmet requirement (exactly the Numeriq/transcript failure). This module closes
that gap: right before the irreversible Submit click, it reads the WHOLE modal and has
a Claude "brain" enumerate every requirement and judge whether each is satisfied.

Design:
  • enumerate_controls / snapshot_modal — DETERMINISTIC superset of every interactive
    element in the modal (incl. file inputs, radios, checkboxes, comboboxes). Pure DOM.
  • audit_requirements — the BRAIN call (claude_runner, hard-timed-out). Returns a
    structured verdict, or {"available": False} on any failure.
  • reconcile — merges the brain verdict with what the deterministic layer already
    confirmed. SATISFY-OR-HALT: blocks only on a required item that is genuinely unmet
    AND not already covered deterministically. The brain may only ADD requirements; it
    can never override a heuristic-confirmed "satisfied".

Safety: if the brain is unavailable (it cannot be spawned from an interactive Claude
Code session — the safety classifier blocks nested `claude -p` — only under launchd),
or its output won't parse, reconcile returns {"action": "proceed"} and behaviour is
identical to today. A wrong audit can therefore only cause a FALSE BLOCK (escalated to
the user), never a bad submit.
"""
import json
from pathlib import Path

import claude_runner

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "documents" / "registry.json"

# Deterministic enumeration of every interactive control in the modal (the element
# handle is passed as `root`). A superset of _read_questions, which only saw select/
# textarea — this also sees file inputs, radios, checkboxes, comboboxes, the submit btn.
_ENUMERATE_JS = r"""(root) => {
  const lab = (el) => {
    let l = '';
    try { if (el.labels && el.labels[0]) l = el.labels[0].innerText; } catch(e){}
    if (!l) l = (el.getAttribute && el.getAttribute('aria-label')) || '';
    if (!l) l = (el.getAttribute && el.getAttribute('placeholder')) || '';
    if (!l) { const c = el.closest('label'); if (c) l = c.innerText; }
    if (!l) { const f = el.closest('fieldset'); const lg = f && f.querySelector('legend'); if (lg) l = lg.innerText; }
    return (l || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  };
  const out = [];
  const q = 'input, select, textarea, [role=combobox], [role=radiogroup], [role=listbox], button[type=submit]';
  for (const el of root.querySelectorAll(q)) {
    const tag = (el.tagName || '').toLowerCase();
    const type = ((el.getAttribute && el.getAttribute('type')) || el.type || '').toLowerCase();
    const role = (el.getAttribute && el.getAttribute('role')) || '';
    let required = false;
    try { required = !!el.required || el.getAttribute('aria-required') === 'true'; } catch(e){}
    let checked = null;
    if (type === 'checkbox' || type === 'radio') { try { checked = !!el.checked; } catch(e){} }
    let options = [];
    if (tag === 'select') { try { options = [...el.options].map(o => (o.textContent||'').trim()).filter(Boolean).slice(0,25); } catch(e){} }
    let value = '';
    try { value = String(el.value || '').slice(0, 60); } catch(e){}
    out.push({tag, type, role, label: lab(el), required, checked, options, value});
  }
  return out;
}"""

AUDIT_SYSTEM = (
    "You are a meticulous job-application form AUDITOR. You NEVER click, submit, edit, "
    "or use any tool — you only read the provided application-modal content and report, "
    "as strict JSON, every requirement that must be satisfied for the Submit button to "
    "actually register the application. Never invent a requirement that isn't shown."
)


def load_registry() -> dict:
    try:
        reg = json.loads(REGISTRY.read_text())
        return {k: v for k, v in reg.items() if not k.startswith("_")}
    except Exception:
        return {}


def available_assets() -> dict:
    """What we can attach/answer: resume + cover are always tailorable; registry docs
    (e.g. transcript) are available when their file exists and auto_attach is on."""
    assets = {"resume": True, "cover_letter": True}
    for name, spec in load_registry().items():
        try:
            if (HERE / spec.get("file", "")).exists():
                assets[name] = bool(spec.get("auto_attach", True))
        except Exception:
            pass
    return assets


def enumerate_controls(modal) -> list:
    try:
        return modal.evaluate(_ENUMERATE_JS) or []
    except Exception:
        return []


def snapshot_modal(modal) -> dict:
    try:
        text = modal.inner_text() or ""
    except Exception:
        text = ""
    return {"modal_text": text[:6000], "controls": enumerate_controls(modal)}


# ── brain ───────────────────────────────────────────────────────────────────
def _first_json_object(s: str):
    """Extract the first balanced {...} object from arbitrary text."""
    start = s.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None


def parse_verdict(text: str):
    """Tolerant: accept a bare JSON object, a ```json fenced block, or JSON embedded in
    prose. Returns the dict only if it has a 'requirements' list, else None."""
    if not text:
        return None
    s = text.strip()
    obj = None
    try:
        obj = json.loads(s)
    except Exception:
        obj = _first_json_object(s)
    if isinstance(obj, dict) and isinstance(obj.get("requirements"), list):
        return obj
    return None


def audit_requirements(snapshot: dict, job: dict, deterministic: dict) -> dict:
    """The brain call. Returns the parsed verdict (+ available=True), or
    {"available": False} on any failure (timeout / parse fail / nested-claude block)."""
    prompt = (
        "Audit this Handshake application modal and list EVERY requirement that must be "
        "satisfied for Submit to actually register the application (required questions, "
        "required document uploads such as a transcript or writing sample, agreement "
        "checkboxes, etc.). For each, decide if it is already satisfied, using the "
        "DETERMINISTIC_STATE (already confirmed by code) and the MODAL content as "
        "evidence. Do NOT invent requirements that aren't visible; mark optional items "
        'required=false.\n\n'
        f"JOB: {job.get('title','')} at {job.get('company','')}\n\n"
        f"AVAILABLE_ASSETS (things we can attach/answer): {json.dumps(available_assets())}\n\n"
        f"DETERMINISTIC_STATE: {json.dumps(deterministic)}\n\n"
        f"CONTROLS (interactive elements): {json.dumps(snapshot.get('controls'))[:4000]}\n\n"
        f"MODAL_TEXT:\n{snapshot.get('modal_text','')[:5000]}\n\n"
        "Output ONLY a JSON object (no prose, no code fence), shape:\n"
        '{"ready_to_submit": bool, "requirements": [{"id": str, "label": str, '
        '"kind": "document|question|checkbox|agreement|other", "required": bool, '
        '"satisfied": bool, "satisfiable": bool, "asset": "transcript|resume|cover_letter|null", '
        '"evidence": str, "remediation": "attach_transcript|attach_resume|attach_cover|answer_question|none"}]}'
    )

    def ok(run):
        return parse_verdict(run.get("stdout") or "")

    try:
        res = claude_runner.run_with_retries(
            prompt, HERE, ok, timeout=120, retries=2, max_turns=6,
            append_system_prompt=AUDIT_SYSTEM, model="claude-sonnet-4-6")
    except Exception:
        return {"available": False}
    if res.get("ok") and res.get("value"):
        v = dict(res["value"])
        v["available"] = True
        return v
    return {"available": False}


# ── reconcile (deterministic; unit-tested without the brain) ──────────────────
def _det_satisfied(req: dict, det: dict) -> bool:
    """True if the deterministic layer already confirmed this requirement, so the brain
    must not block on it (it can only ADD genuinely-missed requirements)."""
    blob = ((req.get("asset") or "") + " " + (req.get("kind") or "") + " "
            + (req.get("label") or "")).lower()
    if "resume" in blob and det.get("resume_ok"):
        return True
    if "cover" in blob and det.get("cover_ok"):
        return True
    if "transcript" in blob and det.get("transcript_attached"):
        return True
    if (req.get("kind") or "").lower() == "question":
        label = (req.get("label") or "").lower()
        unanswered = [str(x).lower() for x in det.get("unanswered_required", [])]
        # answered already unless it matches one we explicitly couldn't answer
        if label and not any(label[:30] in u or u[:30] in label for u in unanswered):
            return True
    return False


def reconcile(verdict: dict, deterministic: dict) -> dict:
    """Merge brain verdict + deterministic state -> a Decision:
       {"action": "proceed"|"block", "detail": str, "missing": [...]}.
    Proceeds (today's behaviour) whenever the brain is unavailable or finds nothing the
    deterministic layer didn't already handle. Blocks only on genuinely-unmet required
    items — satisfy-or-halt."""
    if not verdict or not verdict.get("available"):
        return {"action": "proceed"}
    if verdict.get("ready_to_submit") is True:
        return {"action": "proceed"}
    unmet = []
    for req in verdict.get("requirements", []):
        if not req.get("required") or req.get("satisfied"):
            continue
        if _det_satisfied(req, deterministic):
            continue
        unmet.append(req)
    if not unmet:
        return {"action": "proceed"}
    labels = [r.get("label") or r.get("id") or "a required item" for r in unmet]
    return {"action": "block",
            "detail": "Unmet submit requirement(s): " + "; ".join(labels[:4]),
            "missing": [{"id": r.get("id"), "label": r.get("label"), "kind": r.get("kind"),
                         "satisfiable": r.get("satisfiable"), "evidence": r.get("evidence")}
                        for r in unmet]}


def audit_before_submit(page, modal, job, deterministic) -> dict:
    """High-level entry used by auto_apply right before the Submit click: snapshot the
    modal, run the brain audit, reconcile. Returns the Decision dict. Never raises."""
    try:
        snap = snapshot_modal(modal)
        verdict = audit_requirements(snap, job, deterministic)
        return reconcile(verdict, deterministic)
    except Exception:
        return {"action": "proceed"}
