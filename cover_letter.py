#!/usr/bin/env python3
"""
Cover-letter generation for Handshake — adapted from nuworks_autoapply/cover_letter.py.

Same engine, repointed to Handshake's own dirs and the SHARED style samples in
../nuworks_autoapply/documents/CoverLetter_*.pdf (do not fork the samples). A letter is
written from the job description + the tailored resume, matching the style of those
sample letters (header -> date -> recipient -> 4 flowing paragraphs -> "Sincerely,
<Your Name>").

Unattended runs generate the body via `claude -p` (like resume tailoring); in an
interactive session the agent writes it directly. Either way `render()` fills the LaTeX
template, compiles to PDF, and consolidates into cover_letters/.

NOTE (same as tailor.py): spawning a nested `claude -p` from inside an interactive
Claude Code session is blocked by the safety classifier — in that case the in-session
agent writes the body and calls render() directly. The scheduled launchd run invokes
`claude -p` unattended (permitted). render()/compile are testable anywhere.
"""
import re
import json
import subprocess
from pathlib import Path
from datetime import date

import claude_runner

HERE = Path(__file__).resolve().parent
COVERS = HERE / "cover_letters"                              # all generated cover letters
SAMPLES_DIR = HERE / "documents"                                # style references (your own CoverLetter_*.pdf)
APPS = HERE / "applications"

def _identity() -> tuple[str, str]:
    """Applicant name + LaTeX contact line from data/profile.json (gitignored, per-user)
    so no personal contact info is hardcoded. Neutral placeholders if absent."""
    try:
        p = json.loads((HERE / "data" / "profile.json").read_text())
    except Exception:
        p = {}
    name = p.get("name") or "Your Name"
    contact = (f"{p.get('location', 'Your City, ST')} $\\bullet$ {p.get('phone', '(000) 000-0000')} "
               f"$\\bullet$ {p.get('email', 'you@example.com')} $\\bullet$ LinkedIn: {name}")
    return name, contact


NAME, CONTACT = _identity()

CL_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage[margin=1in]{geometry}
\usepackage{helvet}
\renewcommand{\familydefault}{\sfdefault}
\usepackage{ragged2e}
\usepackage[none]{hyphenat}
\usepackage{microtype}
\usepackage{setspace}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{8pt}
\begin{document}
\begin{center}
{\fontsize{16}{18}\selectfont \textbf{%(name)s}}\\[2pt]
\small %(contact)s
\end{center}
\vspace{6pt}
%(date)s

%(recipient)s

\vspace{4pt}
%(salutation)s

\justifying
%(body)s

\vspace{6pt}
Sincerely,\\[2pt]
%(name)s
\end{document}
"""


def needs_cover_letter(required_docs) -> bool:
    return any("cover letter" in str(d).lower() for d in (required_docs or []))


def _pascal(s: str) -> str:
    s = re.sub(r"[^0-9A-Za-z ]", "", s).strip()
    return "".join(w[:1].upper() + w[1:] for w in s.split())


def latex_escape(s: str) -> str:
    repl = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
            "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(repl.get(c, c) for c in (s or ""))


def basename(job: dict) -> str:
    return f"CoverLetter_{_pascal(job.get('title','Role'))}_{_pascal(job.get('company','Company'))}"


def _workdir(job: dict) -> Path:
    d = APPS / date.today().isoformat() / str(job["job_id"])[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


def render(job: dict, body: str, recipient_lines, salutation: str) -> str:
    """Fill the template with an already-written body, compile, consolidate.
    `body` is plain text (paragraphs separated by blank lines); it is escaped."""
    d = _workdir(job)
    base = basename(job)

    body_tex = "\n\n".join(latex_escape(p.strip()) for p in body.split("\n\n") if p.strip())
    recipient_tex = r"\\".join(latex_escape(x) for x in recipient_lines)
    tex = CL_TEMPLATE % {
        "name": NAME,
        "contact": CONTACT,
        "date": latex_escape(date.today().strftime("%B %d, %Y")),
        "recipient": recipient_tex,
        "salutation": latex_escape(salutation),
        "body": body_tex,
    }
    tex_path = d / f"{base}.tex"
    tex_path.write_text(tex)
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", f"{base}.tex"],
                   cwd=str(d), capture_output=True)
    for ext in (".aux", ".log", ".out"):
        (d / f"{base}{ext}").unlink(missing_ok=True)

    pdf = d / f"{base}.pdf"
    if pdf.exists():
        COVERS.mkdir(exist_ok=True)
        stamp = date.today().isoformat()
        dest = COVERS / f"{stamp}__{base}.pdf"
        dest.write_bytes(pdf.read_bytes())
        (COVERS / f"{stamp}__{base}.tex").write_text(tex)
        return str(dest)
    return ""


def make(job: dict, body: str) -> str:
    """Convenience: build the standard recipient + salutation from the job and render.
    Used by the in-session agent path (body written directly)."""
    company = job.get("company", "")
    loc = (job.get("location", "") or "").split(",")[0].strip()
    recipient = [company, "Hiring Team"] + ([loc] if loc else [])
    return render(job, body, recipient, f"Dear {company} Hiring Team,")


def generate_with_claude(job: dict, resume_tex_path: str,
                         timeout: int = claude_runner.DEFAULT_TIMEOUT,
                         retries: int = claude_runner.DEFAULT_RETRIES) -> dict:
    """Unattended path: have `claude -p` write the cover-letter body in your style
    (referencing ../nuworks_autoapply/documents/CoverLetter_*.pdf) from the JD + resume,
    then render it. Hardened like resume tailoring. Returns {ok, pdf} or, on failure,
    {ok:False, fail_code, fail_reason, log}."""
    d = _workdir(job)
    (d / "job_description.txt").write_text(
        f"Job Title: {job.get('title','')}\nCompany: {job.get('company','')}\n"
        f"Location: {job.get('location','')}\n\n{job.get('description','')}\n")
    log_path = d / "claude_coverletter.log"

    # The style samples are PDFs outside claude -p's sandbox; extract them to .txt
    # INSIDE the workdir so the model can read them, and reference those local files.
    sample_note = "the four-paragraph structure described above"
    try:
        names = []
        for pdf_sample in sorted(SAMPLES_DIR.glob("CoverLetter_*.pdf")):
            txt_out = d / (pdf_sample.stem + ".sample.txt")
            subprocess.run(["pdftotext", "-layout", str(pdf_sample), str(txt_out)],
                           capture_output=True)
            if txt_out.exists() and txt_out.stat().st_size > 0:
                names.append(txt_out.name)
        if names:
            sample_note = "the tone and structure of the sample letters " + ", ".join(names)
    except Exception:
        pass

    # Embed the résumé CONTENT inline. claude -p is sandboxed to the workdir `d` and
    # cannot read the résumé file (it lives in resumes/ or a prior day's workdir), which
    # made it ask for access and produce no_output. Extract its text and pass it inline.
    resume_text = ""
    try:
        src = Path(resume_tex_path)
        cand = src if src.exists() else src.with_suffix(".pdf")
        if cand.exists():
            if cand.suffix == ".tex":
                resume_text = cand.read_text()[:6000]
            else:
                out = subprocess.run(["pdftotext", "-layout", str(cand), "-"],
                                     capture_output=True, text=True)
                resume_text = (out.stdout or "")[:6000]
    except Exception:
        resume_text = ""

    prompt = (
        "Write a one-page cover letter body (4 short flowing paragraphs, no header/"
        "date/signature) for the job in job_description.txt, drawn from the tailored "
        "résumé included below. Match " + sample_note + " (enthusiasm + role; experience "
        "mapping with metrics; projects; mission fit + commitment). Use only TRUE "
        "experience from the résumé — never invent anything. Output ONLY the body text, "
        "paragraphs separated by blank lines — no markdown, no preamble like 'Here is', "
        "no salutation, no signature.\n\n=== TAILORED RÉSUMÉ ===\n" + resume_text
    )

    def success_check(run):
        body = (run.get("stdout") or "").strip()
        if len(body) < 80:
            return None
        low = body.lower()
        bad = ("i cannot", "i can't", "i am unable", "i'm unable", "as an ai",
               "```", "here is the", "here's the", "here is a", "i don't have access")
        if any(b in low for b in bad):
            return None
        if body.count("\n\n") < 2:
            return None
        return body

    res = claude_runner.run_with_retries(
        prompt, d, success_check, timeout=timeout, retries=retries,
        add_dir=str(d), log_path=log_path, model="claude-sonnet-4-6")

    if not res["ok"]:
        code, reason = claude_runner.classify_failure(res["runs"])
        return {"ok": False, "pdf": None, "attempts": res["attempts"],
                "fail_code": code, "fail_reason": reason, "log": str(log_path)}

    pdf = make(job, res["value"])
    if not pdf:
        return {"ok": False, "pdf": None, "fail_code": "compile_failed",
                "fail_reason": "Cover-letter body generated but LaTeX render produced no PDF.",
                "log": str(log_path)}
    return {"ok": True, "pdf": pdf}


if __name__ == "__main__":
    job = {"job_id": "testtest", "title": "Software Engineer Co-op", "company": "Acme Corp",
           "location": "Boston, MA"}
    body = ("I am writing to express my enthusiasm for the Software Engineer Co-op role at Acme Corp. "
            "As a Master's student in Computer Science at Northeastern University, I am excited to contribute.\n\n"
            "My technical profile strongly matches your requirements. At a previous internship I built REST APIs "
            "and microservices serving 500+ users with 95% test coverage.\n\n"
            "My project work further demonstrates my readiness, including an AI chatbot processing 2,000+ queries.\n\n"
            "What excites me most about Acme is the mission. Thank you for your consideration.")
    print("rendered ->", make(job, body))
