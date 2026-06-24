#!/usr/bin/env python3
"""
Hardened wrapper around `claude -p` for the unattended pipeline.

Why this exists: the 2026-06-05 run hung for ~113 minutes inside a single
`claude -p` call (its Anthropic API request stalled and only failed with
"Request timed out" almost two hours later). The plain
`subprocess.run(timeout=...)` guard did not reliably terminate it. This module
fixes that with three things the bare call lacked:

  1. A *hard* wall-clock kill — `claude` (a Node CLI) spawns child processes, so
     we launch it in its own process group (`start_new_session=True`) and on
     timeout SIGKILL the whole group via `os.killpg`. The TimeoutExpired is
     caught, never propagated.
  2. Retry-with-backoff — a transient API/network stall self-heals on a fresh
     process instead of dropping the job.
  3. Per-attempt logging + failure classification — every attempt's stdout/stderr
     is appended to a log file, and `classify_failure()` turns the captured
     output into a short human-readable probable cause (for the Telegram alert).

`run_with_retries()` is generic: the caller supplies a `success_check(run)` that
returns a truthy value once the run produced what it needed (a written .tex, a
non-empty body, ...). tailor.py and cover_letter.py both build on it.
"""
import os
import time
import signal
import subprocess
from pathlib import Path
from datetime import datetime

# Defaults shared with the pipeline so the watchdog can size its budget to match.
DEFAULT_TIMEOUT = 300   # seconds per attempt (hard wall-clock kill)
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 30    # seconds; multiplied by attempt number (linear backoff)
DEFAULT_MAX_TURNS = 40  # bound the agentic loop so it can't wander indefinitely


def run_claude_p(prompt: str, cwd, *, append_system_prompt: str = None,
                 add_dir: str = None, timeout: int = DEFAULT_TIMEOUT,
                 max_turns: int = DEFAULT_MAX_TURNS,
                 permission_mode: str = "acceptEdits",
                 model: str = None) -> dict:
    """Run a single `claude -p` invocation with a guaranteed wall-clock bound.

    `model` pins the invocation to a specific model (e.g. "claude-sonnet-4-6");
    when None the CLI uses the user's configured default.
    Returns {returncode, stdout, stderr, timed_out, duration}. Never raises for a
    timeout — `timed_out=True` is set and the process group is SIGKILLed."""
    cmd = ["claude", "-p", prompt,
           "--permission-mode", permission_mode,
           "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    if add_dir:
        cmd += ["--add-dir", str(add_dir)]

    t0 = time.time()
    # start_new_session=True -> child becomes a process-group leader, so we can
    # kill the entire tree (claude + any node children) on timeout.
    proc = subprocess.Popen(cmd, cwd=str(cwd),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        # Drain whatever was buffered; bounded so a wedged pipe can't hang us.
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            out, err = "", ""
    return {
        "returncode": proc.returncode,
        "stdout": out or "",
        "stderr": err or "",
        "timed_out": timed_out,
        "duration": round(time.time() - t0, 1),
    }


def _kill_group(proc):
    """SIGKILL the child's whole process group; fall back to killing the child."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except Exception:
            pass


def run_with_retries(prompt: str, cwd, success_check, *,
                     timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES,
                     backoff: int = DEFAULT_BACKOFF, max_turns: int = DEFAULT_MAX_TURNS,
                     append_system_prompt: str = None, add_dir: str = None,
                     before_attempt=None, log_path=None, model: str = None) -> dict:
    """Run `claude -p` up to `retries` times until `success_check(run)` is truthy.

    success_check receives the run dict and returns the success value (truthy) or
    None/falsy to trigger another attempt. `before_attempt()` (optional) runs
    before each attempt — used to clear stale output so success_check only sees
    the current attempt's work. Returns {ok, value, attempts, runs}."""
    runs = []
    for attempt in range(1, retries + 1):
        if before_attempt:
            before_attempt()
        run = run_claude_p(prompt, cwd, timeout=timeout, max_turns=max_turns,
                           append_system_prompt=append_system_prompt, add_dir=add_dir,
                           model=model)
        runs.append(run)
        if log_path:
            _append_attempt_log(log_path, attempt, retries, run)
        value = success_check(run)
        if value:
            return {"ok": True, "value": value, "attempts": attempt, "runs": runs}
        if attempt < retries:
            time.sleep(backoff * attempt)
    return {"ok": False, "value": None, "attempts": retries, "runs": runs}


def classify_failure(runs: list) -> tuple:
    """Map captured claude -p output to (code, human_message) — the probable
    cause, suitable for a Telegram alert."""
    if not runs:
        return ("unknown", "No claude -p run was recorded.")
    last = runs[-1]
    blob = ((last.get("stdout", "") or "") + "\n" + (last.get("stderr", "") or "")).lower()
    n_timeout = sum(1 for r in runs if r.get("timed_out"))

    if n_timeout:
        return ("timeout",
                f"claude -p exceeded its {DEFAULT_TIMEOUT}s limit on {n_timeout}/{len(runs)} "
                "attempt(s) and was force-killed. Most likely a stalled or very slow "
                "Anthropic API request or a network hiccup (this is exactly what broke "
                "the 2026-06-05 run).")
    if "overloaded" in blob:
        return ("api_overloaded",
                "Anthropic API replied 'overloaded' — transient server-side load. "
                "A later retry should succeed.")
    if "rate limit" in blob or "rate_limit" in blob or "429" in blob:
        return ("rate_limit", "Hit an API rate limit (429). Back off and retry later.")
    if any(k in blob for k in ("request timed out", "etimedout", "econnreset",
                               "enotfound", "socket hang up", "network")):
        return ("network",
                "A network/API request timed out or the connection dropped mid-request.")
    if any(k in blob for k in ("authentication", "unauthorized", "401",
                               "invalid api key", "please run", "/login",
                               "not logged in", "oauth", "token has expired",
                               "credentials")):
        return ("auth",
                "Claude CLI authentication problem — the login/OAuth token has likely "
                "expired. Run `claude` once interactively to re-login, then it'll work "
                "unattended again.")
    if last.get("returncode") not in (0, None):
        return ("claude_error",
                f"claude -p exited with code {last.get('returncode')}. "
                "See the attempt log for the full stderr.")
    return ("no_output",
            "claude -p finished without producing the expected output — it may have "
            "refused, hit the --max-turns cap, or written to the wrong path. "
            "See the attempt log.")


def _append_attempt_log(log_path, attempt: int, retries: int, run: dict):
    try:
        with open(log_path, "a") as f:
            f.write(f"\n{'='*70}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] attempt {attempt}/{retries}"
                    f"  rc={run.get('returncode')}  timed_out={run.get('timed_out')}"
                    f"  duration={run.get('duration')}s\n")
            f.write("--- stdout ---\n" + (run.get("stdout", "") or "") + "\n")
            f.write("--- stderr ---\n" + (run.get("stderr", "") or "") + "\n")
    except Exception:
        pass
