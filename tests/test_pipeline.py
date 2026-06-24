#!/usr/bin/env python3
"""Committed unit tests for pipeline.py pure helpers: the add_pending UPSERT (retry-queue
schema), the _result builder (job_id plumbing), and load_wontfix. File-touching helpers
are redirected to a temp dir so the real data/ is never modified. Offline.

Run: python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pipeline as PL  # noqa: E402


class TestResult(unittest.TestCase):
    def test_result_carries_job_id_and_filters_extra(self):
        job = {"job_id": "999", "title": "Test Job"}
        r = PL._result(job, "submitted_unverified", "detail", system_failure=True,
                       extra={"screenshot": "/x.png", "attached_resume": "R", "junk": "drop"})
        self.assertEqual(r["job_id"], "999")
        self.assertEqual(r["job"], "Test Job")
        self.assertTrue(r["system_failure"])
        self.assertEqual(r["screenshot"], "/x.png")
        self.assertEqual(r["attached_resume"], "R")
        self.assertNotIn("junk", r)

    def test_result_success_has_no_system_failure_key(self):
        r = PL._result({"job_id": "1", "title": "T"}, "applied", "ok")
        self.assertNotIn("system_failure", r)
        self.assertEqual(r["job_id"], "1")


class TestPendingQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {k: getattr(PL, k) for k in ("PENDING_FILE", "WONTFIX_FILE")}
        PL.PENDING_FILE = self.tmp / "pending.json"
        PL.WONTFIX_FILE = self.tmp / "wontfix.json"

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(PL, k, v)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_pending_upsert(self):
        job = {"job_id": "999", "title": "Test", "url": "u"}
        PL.add_pending(job, "verify_gap", "unconfirmed")
        items = json.loads(PL.PENDING_FILE.read_text())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["_attempts"], 1)
        self.assertEqual(items[0]["_last_class"], "verify_gap")
        first_queued = items[0]["_first_queued"]

        PL.add_pending(job, "structural", "unpaid")     # re-queue same job -> upsert
        items = json.loads(PL.PENDING_FILE.read_text())
        self.assertEqual(len(items), 1, "must upsert, not duplicate")
        self.assertEqual(items[0]["_attempts"], 2)
        self.assertEqual(items[0]["_last_class"], "structural")
        self.assertEqual(items[0]["_first_queued"], first_queued, "first_queued must not change")

    def test_add_pending_default_class(self):
        PL.add_pending({"job_id": "1000", "title": "Other"})
        e = json.loads(PL.PENDING_FILE.read_text())[0]
        self.assertEqual(e["_last_class"], "transient")

    def test_load_wontfix(self):
        self.assertEqual(PL.load_wontfix(), set())
        PL.WONTFIX_FILE.write_text(json.dumps({"11": {"title": "X"}, "22": {"title": "Y"}}))
        self.assertEqual(PL.load_wontfix(), {"11", "22"})


if __name__ == "__main__":
    unittest.main()
