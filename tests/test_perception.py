#!/usr/bin/env python3
"""Committed unit tests for perception.py pure logic (the pre-submit audit's parsing +
satisfy-or-halt reconcile). Offline; no browser, no `claude -p`.

Run: python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import perception as P  # noqa: E402


class TestParseVerdict(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(P.parse_verdict('{"requirements":[{"id":"t"}]}')["requirements"][0]["id"], "t")
        self.assertEqual(P.parse_verdict('```json\n{"requirements": []}\n```'), {"requirements": []})
        self.assertEqual(P.parse_verdict('audit:\n{"requirements":[{"id":"x"}]}\ndone')["requirements"][0]["id"], "x")
        self.assertIsNone(P.parse_verdict("not json"))
        self.assertIsNone(P.parse_verdict('{"foo":1}'))   # no requirements list
        self.assertIsNone(P.parse_verdict(""))

    def test_first_json_object_handles_braces_in_strings(self):
        self.assertEqual(P._first_json_object('{"s": "a } b", "n": 1}'), {"s": "a } b", "n": 1})


class TestAssets(unittest.TestCase):
    def test_registry_and_assets(self):
        self.assertIn("transcript", P.load_registry())
        a = P.available_assets()
        self.assertTrue(a.get("resume"))
        self.assertTrue(a.get("cover_letter"))
        self.assertTrue(a.get("transcript"))   # documents/transcript.pdf exists in the repo


class TestReconcile(unittest.TestCase):
    def test_proceed_when_unavailable_or_ready(self):
        self.assertEqual(P.reconcile({"available": False}, {})["action"], "proceed")
        self.assertEqual(P.reconcile(None, {})["action"], "proceed")
        self.assertEqual(P.reconcile({"available": True, "ready_to_submit": True, "requirements": []}, {})["action"], "proceed")

    def test_block_on_genuine_unmet(self):
        v = {"available": True, "ready_to_submit": False, "requirements": [
            {"id": "transcript", "label": "Attach your transcript", "kind": "document",
             "required": True, "satisfied": False, "asset": "transcript", "evidence": "empty"}]}
        d = P.reconcile(v, {"transcript_attached": None})
        self.assertEqual(d["action"], "block")
        self.assertEqual(d["missing"][0]["id"], "transcript")
        # same requirement, but deterministically satisfied -> proceed (brain can't override)
        self.assertEqual(P.reconcile(v, {"transcript_attached": "Transcript"})["action"], "proceed")

    def test_resume_and_question_gating(self):
        rv = {"available": True, "ready_to_submit": False, "requirements": [
            {"id": "resume", "label": "Resume", "kind": "document", "required": True, "satisfied": False, "asset": "resume"}]}
        self.assertEqual(P.reconcile(rv, {"resume_ok": True})["action"], "proceed")
        self.assertEqual(P.reconcile(rv, {"resume_ok": False})["action"], "block")

        qv = {"available": True, "ready_to_submit": False, "requirements": [
            {"id": "q", "label": "Why this role?", "kind": "question", "required": True, "satisfied": False}]}
        self.assertEqual(P.reconcile(qv, {"unanswered_required": []})["action"], "proceed")
        self.assertEqual(P.reconcile(qv, {"unanswered_required": ["Why this role?"]})["action"], "block")

    def test_optional_ignored(self):
        v = {"available": True, "ready_to_submit": False, "requirements": [
            {"id": "o", "label": "Optional", "required": False, "satisfied": False}]}
        self.assertEqual(P.reconcile(v, {})["action"], "proceed")


if __name__ == "__main__":
    unittest.main()
