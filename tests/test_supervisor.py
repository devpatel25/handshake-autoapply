#!/usr/bin/env python3
"""Committed unit tests for supervisor.py pure logic — the validation oracle the
auto-fix engine (fixer.py) runs before letting any autonomous fix go live. No browser,
no network, no `claude -p`: every test is deterministic and offline.

Run: python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import supervisor as S  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_pre_classify(self):
        self.assertEqual(S.pre_classify("submitted_unverified", 1), "verify_gap")
        self.assertEqual(S.pre_classify("submitted_unverified", 2), "code_break")
        self.assertEqual(S.pre_classify("crashed", 0), "transient")
        self.assertEqual(S.pre_classify("crashed", 1), "code_break")
        self.assertEqual(S.pre_classify("error", 0), "transient")
        self.assertEqual(S.pre_classify("tailor_failed", 0), "transient")
        self.assertEqual(S.pre_classify("cap_reached", 0), "transient")
        self.assertEqual(S.pre_classify("challenge_abort", 0), "auth_challenge")
        self.assertEqual(S.pre_classify("blocked_missing_documents", 1), "data_gap")
        self.assertEqual(S.pre_classify("blocked_unanswered_question", 1), "data_gap")
        self.assertEqual(S.pre_classify("blocked_unmet_requirement", 1), "structural")
        self.assertEqual(S.pre_classify("blocked_wrong_resume", 0), "code_break")

    def test_route(self):
        self.assertEqual(S.route("applied", "x", 0), "success")
        self.assertEqual(S.route("reject", "x", 0), "info")
        self.assertEqual(S.route("challenge_abort", "auth_challenge", 0), "session")
        self.assertEqual(S.route("submitted_unverified", "verify_gap", 1), "keep_retry")
        self.assertEqual(S.route("submitted_unverified", "code_break", 2), "evict")
        self.assertEqual(S.route("blocked_missing_documents", "data_gap", 1), "evict")
        self.assertEqual(S.route("error", "transient", 0), "keep_retry")
        self.assertEqual(S.route("error", "transient", S.MAX_RETRY_ATTEMPTS), "evict")
        self.assertEqual(S.route("timeout", "transient", 99), "keep_retry")  # deferral never evicts on cap


class TestParse(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(S._extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(S._extract_json('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(S._extract_json('text {"a": 3} tail'), {"a": 3})
        self.assertIsNone(S._extract_json("not json"))
        self.assertEqual(S._extract_json('{"s": "has } brace"}'), {"s": "has } brace"})

    def test_parse_diag(self):
        self.assertEqual(S._parse_diag('{"failure_class":"code_break","root_cause":"x"}')["failure_class"], "code_break")
        self.assertIsNone(S._parse_diag('{"failure_class":"bogus"}'))
        self.assertIsNone(S._parse_diag(""))


class TestReason(unittest.TestCase):
    def test_reason_strings(self):
        self.assertIn("re-queued", S._reason("submitted_unverified", "verify_gap"))
        self.assertIn("manually", S._reason("blocked_missing_documents", "data_gap", "transcript"))
        self.assertIn("auto-retry", S._reason("blocked_unmet_requirement", "structural"))
        self.assertTrue(S._reason("blocked_wrong_resume", "code_break"))


class TestLoggedOut(unittest.TestCase):
    def test_looks_logged_out(self):
        self.assertTrue(S._looks_logged_out("... Hello, job seeker ...", "https://x/job-search"))
        self.assertTrue(S._looks_logged_out("Enter your Northeastern University email", "https://x"))
        self.assertTrue(S._looks_logged_out("ok", "https://login.example.com/x", login_host="login.example.com"))
        self.assertFalse(S._looks_logged_out("Backend Intern ... Withdraw", "https://northeastern.joinhandshake.com/jobs/1", login_host="login.example.com"))
        self.assertFalse(S._looks_logged_out("", ""))


class _TempState(unittest.TestCase):
    """Base: redirect supervisor's file paths to a temp dir and silence Telegram."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {k: getattr(S, k) for k in
                       ("DATA", "LOGS", "PENDING_FILE", "WONTFIX_FILE", "PROPOSED_FIXES_FILE", "APPLIED_FILE")}
        S.DATA, S.LOGS = self.tmp, self.tmp / "logs"
        S.PENDING_FILE = self.tmp / "pending.json"
        S.WONTFIX_FILE = self.tmp / "wontfix.json"
        S.PROPOSED_FIXES_FILE = self.tmp / "fixes.json"
        S.APPLIED_FILE = self.tmp / "applied.json"
        self._tg_conf, S.tg.configured = S.tg.configured, (lambda: True)
        self.sent = []
        self._tg_send, S.tg.send_message = S.tg.send_message, (lambda m, *a, **k: self.sent.append(m))
        self.now = datetime(2026, 6, 22, 21, 0)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(S, k, v)
        S.tg.configured, S.tg.send_message = self._tg_conf, self._tg_send
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def setp(self, items):
        S.PENDING_FILE.write_text(json.dumps(items))

    def pend(self):
        return json.loads(S.PENDING_FILE.read_text()) if S.PENDING_FILE.exists() else []

    def wf(self):
        return json.loads(S.WONTFIX_FILE.read_text()) if S.WONTFIX_FILE.exists() else {}


class TestRunSupervisor(_TempState):
    def _entry(self, jid, attempts, cls):
        return {"job_id": jid, "title": f"Job {jid}", "_attempts": attempts, "_last_class": cls,
                "_first_queued": self.now.isoformat(), "_queued_at": self.now.isoformat()}

    def test_verify_gap_first_kept_then_evicted(self):
        self.setp([self._entry("500", 1, "verify_gap")])
        S.run_supervisor([{"job": "Job 500", "job_id": "500", "result": "submitted_unverified"}],
                         now=self.now, use_brain=False)
        self.assertNotIn("500", self.wf())
        self.assertTrue(any(p["job_id"] == "500" for p in self.pend()))

        self.setp([self._entry("500", 2, "verify_gap")])
        if S.WONTFIX_FILE.exists():
            S.WONTFIX_FILE.unlink()
        S.run_supervisor([{"job": "Job 500", "job_id": "500", "result": "submitted_unverified"}],
                         now=self.now, use_brain=False)
        self.assertIn("500", self.wf())
        self.assertFalse(any(p["job_id"] == "500" for p in self.pend()))

    def test_data_gap_evicted(self):
        self.setp([self._entry("600", 1, "data_gap")])
        S.run_supervisor([{"job": "Job 600", "job_id": "600", "result": "blocked_missing_documents"}],
                         now=self.now, use_brain=False)
        self.assertIn("600", self.wf())

    def test_age_out(self):
        old = (self.now - timedelta(days=10)).isoformat()
        self.setp([{"job_id": "700", "title": "Stale", "_attempts": 1, "_last_class": "transient",
                    "_first_queued": old, "_queued_at": old}])
        S.run_supervisor([], now=self.now, use_brain=False)
        self.assertIn("700", self.wf())

    def test_applied_counted_benign_ignored(self):
        S.run_supervisor([{"job": "Good", "job_id": "1", "result": "applied"},
                          {"job": "Nope", "job_id": "2", "result": "reject"}], now=self.now, use_brain=False)
        self.assertTrue(self.sent and "Applied: 1" in self.sent[0])
        self.assertNotIn("Nope", self.sent[0])
        self.assertEqual(self.wf(), {})

    # ── discover-phase blind-spot escalations (2026-06-25 regression) ──────────
    def _sentinel(self):
        p = S.LOGS / f"supervisor_done_{self.now:%Y-%m-%d}.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def test_discover_failed_escalates_not_silent(self):
        # Empty results from a failed scrape used to report nothing; now it must surface.
        S.run_supervisor([], now=self.now, use_brain=False,
                         marked=False, new_postings=0, discover_failed=True)
        self.assertTrue(self.sent, "a failed discover must send a report, not stay silent")
        self.assertIn("Discovery failed", self.sent[0])
        self.assertEqual(self._sentinel().get("needs_you"), 1)

    def test_scraped_but_zero_applied_escalates(self):
        S.run_supervisor([], now=self.now, use_brain=False,
                         marked=False, new_postings=2, discover_failed=False)
        self.assertTrue(self.sent)
        self.assertIn("Scraped but applied 0", self.sent[0])
        self.assertIn("2 new posting", self.sent[0])

    def test_external_only_clean_day_no_false_alarm(self):
        # New postings found but all external -> 0 applied AND day cleanly marked: no alarm.
        S.run_supervisor([], now=self.now, use_brain=False,
                         marked=True, new_postings=2, discover_failed=False)
        self.assertEqual(self.sent, [])

    def test_generic_escalation_suppressed_when_real_failure_surfaced(self):
        # A real apply failure already fills needs_you -> don't add the generic gap line.
        self.setp([self._entry("800", 1, "data_gap")])
        S.run_supervisor([{"job": "Job 800", "job_id": "800", "result": "blocked_missing_documents"}],
                         now=self.now, use_brain=False,
                         marked=False, new_postings=2, discover_failed=False)
        joined = "\n".join(self.sent)
        self.assertNotIn("Scraped but applied 0", joined)
        self.assertIn("Needs you", joined)


class TestVerification(_TempState):
    def test_verify_targets(self):
        iso = self.now.isoformat()
        applied = {"1": {"title": "A", "url": "u1", "appliedAt": iso},
                   "2": {"title": "Old", "url": "u2", "appliedAt": "2020-01-01T00:00:00"},
                   "9": {"title": "NoURL", "url": "", "appliedAt": iso}}
        pending = [{"job_id": "3", "title": "VG", "url": "u3", "_last_class": "verify_gap"},
                   {"job_id": "4", "title": "DG", "url": "u4", "_last_class": "data_gap"}]
        ids = {(t["job_id"], t["kind"]) for t in S._verify_targets(applied, pending, self.now, 30)}
        self.assertIn(("1", "applied"), ids)
        self.assertIn(("3", "verify_gap"), ids)
        self.assertNotIn(("2", "applied"), ids)
        self.assertNotIn(("9", "applied"), ids)
        self.assertFalse(any(j == "4" for j, _ in ids))

    def test_reconcile_silent_escape_and_resolve(self):
        iso = self.now.isoformat()
        applied = {"1": {"title": "A", "url": "u1", "appliedAt": iso}}
        pending = [{"job_id": "3", "title": "VG", "url": "u3", "_last_class": "verify_gap", "_attempts": 1}]
        S.APPLIED_FILE.write_text(json.dumps(applied))
        S.PENDING_FILE.write_text(json.dumps(pending))
        checked = [{"job_id": "1", "kind": "applied", "title": "A", "shows_applied": False},
                   {"job_id": "3", "kind": "verify_gap", "title": "VG", "url": "u3", "shows_applied": True}]
        rep = S._reconcile_verification(checked, dict(applied), list(pending), self.now)
        saved = json.loads(S.APPLIED_FILE.read_text())
        self.assertIn("1", saved)                       # silent escape NOT auto-undone
        self.assertIn("3", saved)                       # verify_gap confirmed -> recorded
        self.assertTrue(saved["3"]["detail"].startswith("confirmed applied"))
        self.assertFalse(any(p["job_id"] == "3" for p in json.loads(S.PENDING_FILE.read_text())))
        self.assertIn("please check", rep.lower())
        self.assertIn("Confirmed applied", rep)

    def test_reconcile_none_is_noop(self):
        self.assertIsNone(S._reconcile_verification(
            [{"job_id": "5", "kind": "applied", "title": "B", "shows_applied": None}], {"5": {}}, [], self.now))


if __name__ == "__main__":
    unittest.main()
