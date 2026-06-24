"""Screening-question engine (Handshake). Three tiers + a hard safety rule.

Given the questions read from the apply modal, compute the answer to submit for
each, tagged with `source`:
  profile : deterministic from profile.json (safe)
  claude  : drafted by claude (review — 🟡 on the card)
  none    : unanswerable (🔴; auto_apply BLOCKS the submit if a REQUIRED question
            is 'none', so nothing the user hasn't seen is ever submitted)

Reads canonical answers from this project's own data/profile.json (gitignored, per-user).
"""
import json
import re
from pathlib import Path

import claude_runner

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "data" / "profile.json"
try:
    _P = json.loads(PROFILE.read_text())
except Exception:
    _P = {}


def _candidate_facts() -> str:
    """Short factual background used ONLY to ground claude-drafted screening answers.
    Loaded from data/candidate_facts.txt (gitignored, per-user) so nothing personal is
    hardcoded; the generic fallback keeps the engine working before you create that file."""
    try:
        t = (HERE / "data" / "candidate_facts.txt").read_text().strip()
        if t:
            return t
    except Exception:
        pass
    return ("an MS Computer Science student and software/ML engineer; skills include Python, "
            "Java, JavaScript/TypeScript, SQL, cloud services, and ML frameworks; builds "
            "practical backend and AI/ML systems")


CANDIDATE_FACTS = _candidate_facts()


def _yesno(v):
    return "Yes" if v else "No"


# Tier 1: regex on the question text -> a deterministic answer from profile.json.
# (Order matters: sponsorship is checked before the generic work-auth pattern.)
KNOWN = [
    (r"require.*sponsor|need.*sponsor|visa sponsor|sponsorship (now|in the future|required)|require.*visa",
     _yesno(_P.get("requires_sponsorship"))),
    (r"authoriz.*(work|employ)|legally.*(work|authorized)|eligible to work|work in the (us|united states)",
     _yesno(_P.get("work_authorized_us"))),
    (r"relocat", _yesno(_P.get("willing_to_relocate"))),
    (r"on[- ]?site|in[- ]?office|hybrid|in[- ]person|onsite|commute", _yesno(_P.get("willing_in_office_hybrid"))),
    (r"referr", _yesno(_P.get("referred"))),
    (r"\bgpa\b|grade[- ]point", str(_P.get("gpa", ""))),
    (r"race|ethnic|gender|veteran|disabilit|sexual orientation|\beeo\b|self[- ]?identif|hispanic|latino|pronoun",
     "Decline to self-identify"),
    (r"linkedin", _P.get("linkedin", "")),
    (r"portfolio|personal (web)?site|\bwebsite\b", _P.get("portfolio", "")),
    (r"github", _P.get("github", "")),
    (r"phone|mobile|\bcell\b", _P.get("phone", "")),
    (r"e[- ]?mail", _P.get("email", "")),
    (r"full name|legal name|your name|first and last", _P.get("name", "")),
    (r"address|street", _P.get("address", "")),
    (r"\bcity\b|current location|where are you (based|located)", _P.get("location", "")),
]

CHOICE_TYPES = {"single_choice", "yes_no", "radio", "select", "multi_choice", "checkbox", "dropdown"}
TEXT_TYPES = {"short_text", "textarea", "text", "long_text", "paragraph", "number", "numeric"}


def _tier1(qtext):
    t = qtext.lower()
    for pat, val in KNOWN:
        if re.search(pat, t):
            return val
    return None


def _match_option(answer, options):
    """Fuzzy-match a desired answer string to one of the rendered options."""
    if not options:
        return answer
    a = (answer or "").strip().lower()
    for o in options:
        if a and a == str(o).strip().lower():
            return o
    for o in options:
        if a and a in str(o).strip().lower():
            return o
    if a in ("yes", "no"):
        for o in options:
            if str(o).strip().lower().startswith(a):
                return o
    return None


def _claude_draft(qtext, job):
    prompt = (
        "You are filling a job-application screening question for the candidate. Use ONLY "
        "these facts, do not invent anything: " + CANDIDATE_FACTS + ". "
        f"Job: {job.get('title','')} at {job.get('company','')}. "
        f'Question: "{qtext}". '
        "Write a concise, direct, first-person answer (1-3 sentences, under 60 words). "
        "Output ONLY the answer text — no preamble, no quotes, no labels."
    )

    def ok(run):
        out = (run.get("stdout") or "").strip()
        return out if out and len(out) > 3 else None

    res = claude_runner.run_with_retries(prompt, HERE, ok, timeout=120, retries=2,
                                         model="claude-sonnet-4-6")
    return res["value"].strip() if res["ok"] else None


def answer_questions(questions, job):
    """questions: [{text, type, options, required}]
    -> [{question, answer, source, confidence, required, type}]."""
    out = []
    for q in questions:
        qtext = (q.get("text") or "").strip()
        qtype = (q.get("type") or "short_text").lower()
        options = q.get("options") or []
        required = bool(q.get("required", True))

        val = _tier1(qtext)
        if val:
            if qtype in CHOICE_TYPES:
                opt = _match_option(val, options)
                src, ans = ("profile", opt) if opt is not None else ("none", None)
                out.append({"question": qtext, "answer": ans, "source": src,
                            "confidence": "high" if opt is not None else "low",
                            "required": required, "type": qtype})
            else:
                out.append({"question": qtext, "answer": val, "source": "profile",
                            "confidence": "high", "required": required, "type": qtype})
            continue

        if qtype in TEXT_TYPES:
            draft = _claude_draft(qtext, job)
            if draft:
                out.append({"question": qtext, "answer": draft, "source": "claude",
                            "confidence": "low", "required": required, "type": qtype})
                continue

        out.append({"question": qtext, "answer": None, "source": "none",
                    "confidence": "low", "required": required, "type": qtype})
    return out


def has_blocker(answers):
    """True if any REQUIRED question is unanswerable -> auto_apply must not submit."""
    return any(a["source"] == "none" and a.get("required") for a in answers)


if __name__ == "__main__":
    sample = [
        {"text": "Are you legally authorized to work in the United States?", "type": "yes_no", "options": ["Yes", "No"], "required": True},
        {"text": "Will you now or in the future require sponsorship for an employment visa?", "type": "yes_no", "options": ["Yes", "No"], "required": True},
        {"text": "Are you willing to relocate?", "type": "single_choice", "options": ["Yes", "No", "Maybe"], "required": True},
        {"text": "What is your GPA?", "type": "short_text", "options": [], "required": True},
        {"text": "Why are you interested in this role?", "type": "textarea", "options": [], "required": True},
        {"text": "Which of our product lines excites you most?", "type": "single_choice", "options": ["Payments", "Lending", "Crypto"], "required": True},
    ]
    res = answer_questions(sample, {"title": "Software Engineer Intern", "company": "Acme"})
    for a in res:
        print(f"[{a['source']:7}] {a['question'][:55]:<55} -> {str(a['answer'])[:50]}")
    print("BLOCKER (required unanswerable present):", has_blocker(res))
