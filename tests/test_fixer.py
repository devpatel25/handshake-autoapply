#!/usr/bin/env python3
"""Committed unit tests for fixer.py pure logic: signature determinism, the ledger
upsert/dedup + state machine, and the harvest routing (with Telegram mocked). Offline.

Run: python3 -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fixer as F  # noqa: E402


class TestSignature(unittest.TestCase):
    def test_deterministic_and_distinct(self):
        a = F._signature("11", "cause text")
        self.assertEqual(a, F._signature("11", "cause text"))
        self.assertNotEqual(a, F._signature("11", "different cause"))
        self.assertNotEqual(a, F._signature("22", "cause text"))
        self.assertEqual(len(a), 12)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._led = F.LEDGER_FILE
        F.LEDGER_FILE = self.tmp / "ledger.json"
        self._conf, F.tg.configured = F.tg.configured, (lambda: True)
        self.sent = []
        self._send, F.tg.send_message = F.tg.send_message, (lambda m, *a, **k: self.sent.append(m))
        # harvest tests cover the ledger transition only; the engine (run_one_fix) is
        # exercised in test_fixer_integration.py, so stub the run loop here.
        self._rar, F._run_approved = F._run_approved, (lambda now: None)
        self._dis, F.tg.disable_card_buttons = F.tg.disable_card_buttons, (lambda *a, **k: None)
        self.now = datetime(2026, 6, 22, 21, 0)

    def tearDown(self):
        F.LEDGER_FILE = self._led
        F.tg.configured, F.tg.send_message = self._conf, self._send
        F._run_approved = self._rar
        F.tg.disable_card_buttons = self._dis
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestLedger(_Tmp):
    DIAG = {"root_cause": "resume picker drift", "proposed_fix": "fix it", "confidence": 0.9}

    def test_upsert_creates_proposed(self):
        fid = F.upsert_proposed("11091190", "Data Eng", self.DIAG, "nonce1")
        self.assertTrue(fid)
        e = F.load_ledger()[fid]
        self.assertEqual(e["status"], "proposed")
        self.assertEqual(e["job_id"], "11091190")
        self.assertEqual(e["nonce"], "nonce1")
        self.assertEqual(e["attempts"], 0)

    def test_dedup_same_signature(self):
        F.upsert_proposed("11091190", "Data Eng", self.DIAG, "n1")
        self.assertIsNone(F.upsert_proposed("11091190", "Data Eng", self.DIAG, "n2"))
        self.assertEqual(len(F.load_ledger()), 1)

    def test_set_status_terminal_stamps_resolved(self):
        fid = F.upsert_proposed("5", "T", self.DIAG, "n")
        F.set_status(fid, "rejected")
        e = F.load_ledger()[fid]
        self.assertEqual(e["status"], "rejected")
        self.assertIsNotNone(e["resolved_at"])


class TestHarvest(_Tmp):
    DIAG = {"root_cause": "c", "proposed_fix": "f", "confidence": 0.9}

    def _seed(self):
        return F.upsert_proposed("11", "Job", self.DIAG, "nonce1")

    def test_harvest_approve_moves_to_approved(self):
        fid = self._seed()
        F.tg.harvest_fix_decisions = lambda pending, timeout=20: {fid: "approve"}
        F.harvest_and_run(now=self.now)
        self.assertEqual(F.load_ledger()[fid]["status"], "approved")

    def test_harvest_reject_moves_to_rejected(self):
        fid = self._seed()
        F.tg.harvest_fix_decisions = lambda pending, timeout=20: {fid: "reject"}
        F.harvest_and_run(now=self.now)
        self.assertEqual(F.load_ledger()[fid]["status"], "rejected")

    def test_harvest_noop_when_no_proposed(self):
        called = []
        F.tg.harvest_fix_decisions = lambda *a, **k: (called.append(1), {})[1]
        F.harvest_and_run(now=self.now)
        self.assertEqual(called, [], "must not poll Telegram when there's nothing pending")

    def test_harvest_disables_acted_card(self):
        fid = self._seed()
        F.set_status(fid, "proposed", card_msg_id=12345)   # card has a stored message id
        disabled = []
        F.tg.disable_card_buttons = lambda mid: disabled.append(mid)
        F.tg.harvest_fix_decisions = lambda pending, timeout=20: {fid: "approve"}
        F.harvest_and_run(now=self.now)
        self.assertEqual(F.load_ledger()[fid]["status"], "approved")
        self.assertIn(12345, disabled)                     # the acted-on card's buttons were stripped


if __name__ == "__main__":
    unittest.main()
