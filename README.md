# Handshake Auto-Apply

Daily auto-apply automation for university **Handshake** career portals. It discovers
relevant jobs, tailors a one-page résumé per job (the ResumeBot2.1 process), sends a
Telegram approval card, and on approval submits the in-Handshake application with the
tailored résumé and screening answers.

> **Human-in-the-loop by design.** Nothing is submitted without an explicit Telegram tap,
> and a required screening question the bot can't answer from your profile blocks the submit.

This project is **standalone** (it ships its own résumé templates and profile). It is a
work-in-progress sibling to the NUworks and Wellfound auto-apply bots; the apply step is
still being finished — see PLAN.md for build order. Use it responsibly with your own account.

---

## How it works

```
session check ─▶ discover ─▶ score ─▶ classify in-app vs external
   └▶ tailor ALL résumés in parallel  (SWE / AI-ML base + JD keywords → 1 page)
        └▶ Telegram approval card (résumé + proposed screening answers + flags)
              └─ Approve ─▶ attach résumé → answer screening → Submit → verify
```

- **Auth** (`capture_session.py`, `browser.py`, `session_check.py`): Northeastern-style Handshake uses Microsoft Entra SSO + DUO, which blocks automated login. So you log in once by hand in a normal Chrome (`capture_session.py`), and the tool captures the post-login session (`storage_state`) which the automation injects into a real (headed) Chrome. Re-capture only when the server session expires.
- **Discover / score** (`discover.py`, `score.py`): GraphQL job search → filter + rank → shortlist.
- **Tailor in parallel** (`tailor.py` via `pipeline.py`): ResumeBot2.1 through headless `claude -p`, all eligible résumés tailored **concurrently up front**, compiled to exactly one page.
- **Screening** (`screening.py`): deterministic answers from `data/profile.json`; claude drafts the rest (shown for review); unanswerable required questions block the submit.
- **Apply** (`auto_apply.py`, `perception.py`): attaches the résumé (kept private), answers questions, and submits with a pre-submit safety read of the modal.

A watchdog, single-instance lock, once-per-day guard, and a supervisor that classifies
outcomes make unattended runs safe; a failed run is left unmarked to retry.

---

## Requirements

- **Python 3.10+** + `requirements.txt` (`pip install -r requirements.txt`)
- **Real Chrome** via Playwright: `playwright install chrome`
- **TeX Live** (`pdflatex`) + **poppler** (`pdfinfo`) for résumé compilation
- The **`claude` CLI** (Claude Code), logged in once — used for tailoring + screening drafts
- A Handshake account and a Telegram bot (via **@BotFather**)

## Setup

```bash
pip install -r requirements.txt
playwright install chrome

cp .env.example .env                 # Telegram bot token + chat id
cp config.example.json config.json   # set saved_search_url to YOUR school's Handshake search
cp data/profile.example.json data/profile.json            # contact + work-auth answers
cp data/candidate_facts.example.txt data/candidate_facts.txt   # 1-line background for screening drafts
cp resume_templates/Resume_SWE_General.example.tex   resume_templates/Resume_SWE_General.tex
cp resume_templates/Resume_AI-ML_General.example.tex resume_templates/Resume_AI-ML_General.tex
# then edit the two .tex files with YOUR real one-page résumé content
```

Capture your session:

```bash
python3 capture_session.py     # opens normal Chrome; do SSO + DUO + "Stay signed in", then press Enter
python3 session_check.py       # prints "logged_in", exit 0
```

Everything personal (`.env`, `config.json`, `data/`, the captured session under `data/`,
`resumes/`, `applications/`, `cover_letters/`, your filled `resume_templates/*.tex`, PDFs,
screenshots) is gitignored — only the `*.example.*` templates are tracked.

## Running

```bash
python3 pipeline.py --dry-apply     # full run but never click Submit (safe test)
python3 pipeline.py                 # live run (approval still required per job)
python3 pipeline.py --no-scrape     # reuse today's jobs file
python3 pipeline.py --limit N       # cap jobs processed
```

> Note: a nested `claude -p` is blocked inside an interactive Claude Code session, so real
> tailoring runs from a normal terminal or the scheduled job.

## Scheduling

Run `pipeline.py` on a daily schedule with your OS scheduler (macOS launchd, or cron/systemd).
The schedule files are intentionally not committed (they hardcode machine paths) — create
your own. A digest (`digest.py`) and a supervisor pass run alongside the main pipeline.

## Layout

```
handshake_autoapply/
├── pipeline.py              # orchestrator
├── capture_session.py       # one-time SSO+DUO login → captured session
├── browser.py · session_check.py
├── discover.py · score.py · apply_type.py · screening.py
├── tailor.py · cover_letter.py     # ResumeBot2.1 tailoring + cover letters
├── auto_apply.py · perception.py   # drive + safety-check the apply modal
├── telegram_bot.py · stats.py · digest.py · supervisor.py · fixer.py
├── resume_templates/        # system_instructions.md + *.example.tex (your real .tex gitignored)
├── config.example.json · .env.example · requirements.txt
├── data/                    # session + runtime state (gitignored; *.example.* tracked)
└── tests/
```
