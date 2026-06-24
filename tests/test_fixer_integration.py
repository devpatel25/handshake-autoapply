#!/usr/bin/env python3
"""Integration tests for the auto-fix engine's apply path — the delicate git logic —
WITHOUT any real coding pass, claude, browser, or Telegram. A temp git repo stands in for
the project; the coding pass + validation are mocked so each tier/outcome can be driven
deterministically, and the REAL git operations (commit, revert, patch capture/apply) run
against the temp repo.

Run: python3 -m unittest discover -s tests
"""
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fixer as F  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


class TestApplyPath(unittest.TestCase):
    def setUp(self):
        # temp git repo with tier-representative files, committed clean
        self.repo = Path(tempfile.mkdtemp())
        self.state = Path(tempfile.mkdtemp())   # ledger/pending/wontfix OUTSIDE the repo
        for name in ("stats.py", "auto_apply.py", "supervisor.py"):
            (self.repo / name).write_text("X = 1\n")
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")

        self._saved = {k: getattr(F, k) for k in
                       ("ROOT", "LEDGER_FILE", "PENDING_FILE", "WONTFIX_FILE",
                        "_coding_pass", "validate", "_cfg")}
        F.ROOT = self.repo
        F.LEDGER_FILE = self.state / "ledger.json"
        F.PENDING_FILE = self.state / "pending.json"
        F.WONTFIX_FILE = self.state / "wontfix.json"

        self.flag = {"apply_enabled": True}
        F._cfg = lambda: {"apply_enabled": self.flag["apply_enabled"], "fix_cap_per_run": 1,
                          "tier1_files": ["supervisor.py", "fixer.py", "claude_runner.py", "config.json"],
                          "tier2_files": ["auto_apply.py", "pipeline.py", "telegram_bot.py"],
                          "harvest_poll_seconds": 1, "coding_pass_timeout": 5}
        self.target = None
        F._coding_pass = self._mock_pass
        F.validate = lambda changed: (True, "ok")

        self._tgc, F.tg.configured = F.tg.configured, (lambda: True)
        self.sent = []
        self._tgs, F.tg.send_message = F.tg.send_message, (lambda m, *a, **k: self.sent.append(m))
        self.diffcards = []
        self._tgd = getattr(F.tg, "send_diff_review_card", None)
        F.tg.send_diff_review_card = lambda *a, **k: self.diffcards.append(a)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(F, k, v)
        F.tg.configured, F.tg.send_message = self._tgc, self._tgs
        if self._tgd:
            F.tg.send_diff_review_card = self._tgd
        import shutil
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def _mock_pass(self, e):
        if self.target:
            p = self.repo / self.target
            p.write_text(p.read_text() + "\n# autofix change\n")
        return {"returncode": 0, "timed_out": False}

    def _commits(self):
        return int(_git(self.repo, "rev-list", "--count", "HEAD").stdout.strip() or "0")

    def _run(self, apply_enabled, target, validate_ok=True):
        self.flag["apply_enabled"] = apply_enabled
        self.target = target
        F.validate = lambda changed: (validate_ok, "ok" if validate_ok else "boom error")
        fid = F.upsert_proposed("11", "Job", {"root_cause": "c", "proposed_fix": "f", "confidence": 0.9}, "n")
        F.set_status(fid, "approved")
        F.run_one_fix(F.load_ledger()[fid], datetime.now())
        return fid

    # ── Tier 3 ────────────────────────────────────────────────────────────
    def test_tier3_live_commit_and_requeue(self):
        fid = self._run(True, "stats.py")
        e = F.load_ledger()[fid]
        self.assertEqual(e["status"], "applied")
        self.assertEqual(self._commits(), 2)            # baseline + the autofix commit
        self.assertTrue(F._clean_tree())
        self.assertTrue(any(p["job_id"] == "11" for p in F._load(F.PENDING_FILE, [])))

    def test_tier3_dry_reverts_no_commit(self):
        fid = self._run(False, "stats.py")              # apply_enabled=false (5c)
        e = F.load_ledger()[fid]
        self.assertEqual(e["status"], "approved")       # stays approved (non-terminal)
        self.assertTrue(e["dry_checked"])
        self.assertEqual(self._commits(), 1)            # nothing committed
        self.assertTrue(F._clean_tree())

    # ── Tier 1 (protected) ──────────────────────────────────────────────────
    def test_tier1_rejected_and_reverted(self):
        fid = self._run(True, "supervisor.py")
        e = F.load_ledger()[fid]
        self.assertEqual(e["status"], "failed")
        self.assertEqual(e["fail_step"], "tier1_protected")
        self.assertEqual(self._commits(), 1)
        self.assertTrue(F._clean_tree())

    # ── Tier 2 (diff review) ─────────────────────────────────────────────────
    def test_tier2_routes_to_diff_review(self):
        fid = self._run(True, "auto_apply.py")
        e = F.load_ledger()[fid]
        self.assertEqual(e["status"], "awaiting_diff_review")
        self.assertTrue(e["patch"])                     # diff captured for the 2nd-approval re-apply
        self.assertEqual(self._commits(), 1)            # NOT committed yet
        self.assertTrue(F._clean_tree())                # reverted while awaiting review
        self.assertTrue(self.diffcards)                 # diff sent to the user

    def test_diff_approved_applies(self):
        fid = self._run(True, "auto_apply.py")          # -> awaiting_diff_review w/ patch
        F.set_status(fid, "diff_approved")
        F._apply_approved_diff(F.load_ledger()[fid], datetime.now())
        e = F.load_ledger()[fid]
        self.assertEqual(e["status"], "applied")
        self.assertEqual(self._commits(), 2)            # patch re-applied + committed
        self.assertTrue(F._clean_tree())

    # ── failure modes ────────────────────────────────────────────────────────
    def test_validation_failure_reverts(self):
        fid = self._run(True, "stats.py", validate_ok=False)
        e = F.load_ledger()[fid]
        self.assertEqual(e["status"], "failed")
        self.assertEqual(e["fail_step"], "validation")
        self.assertEqual(self._commits(), 1)
        self.assertTrue(F._clean_tree())

    def test_noop_coding_pass_fails_safe(self):
        fid = self._run(True, None)                     # coding pass changes nothing
        e = F.load_ledger()[fid]
        self.assertEqual(e["status"], "failed")
        self.assertEqual(e["fail_step"], "coding_pass")
        self.assertEqual(self._commits(), 1)
        self.assertTrue(F._clean_tree())


if __name__ == "__main__":
    unittest.main()
