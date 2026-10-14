#!/usr/bin/env python3
"""Committed unit tests for auto_apply.py's pure helpers behind the generic
'Attach other required documents' handling (the 2026-06-28 transcript-attach gap:
employers that request the transcript via the catch-all section, not the dedicated one).
No browser, no network: deterministic and offline.

Run: python3 -m unittest discover -s tests
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auto_apply as A  # noqa: E402


REG = {
    "_note": "ignored",
    # standard-section transcript (type 3) — NOT eligible for the generic section
    "transcript": {"doc_type": "transcript", "picker_query": "transcript"},
    # the 'Other Document' (type 5) copy the generic section requires
    "other_document": {"doc_type": "other", "picker_query": "Transcript", "match": "Other Document",
                       "instruction_keywords": ["transcript", "transcipt", "transc"]},
}


class _FakeModal:
    """Minimal stand-in: the readback helpers only call modal.inner_text()."""
    def __init__(self, text):
        self._text = text
    def inner_text(self):
        return self._text


class TestMatchOtherDoc(unittest.TestCase):
    def test_matches_employer_typo(self):
        # the real Global Radio Outreach note — 'Transcipt' is the employer's typo
        m = A._match_other_doc("Transcipt if available", REG)
        self.assertIsNotNone(m)
        self.assertEqual(m[0], "other_document")        # the type-5 entry, not the type-3 one

    def test_matches_correct_spelling(self):
        self.assertEqual(A._match_other_doc("Please attach your transcript", REG)[0], "other_document")

    def test_only_other_type_eligible(self):
        # a transcript-typed entry must NOT satisfy the generic section (Handshake rejects it)
        reg = {"transcript": {"doc_type": "transcript", "instruction_keywords": ["transc"]}}
        self.assertIsNone(A._match_other_doc("transcript", reg))

    def test_no_match_for_portfolio(self):
        # we have no portfolio doc -> must NOT match (so the fail-safe blocks, not bad-submits)
        self.assertIsNone(A._match_other_doc("Portfolio of previous work", REG))

    def test_empty_instruction(self):
        self.assertIsNone(A._match_other_doc("", REG))

    def test_default_stem_when_no_keywords(self):
        # no instruction_keywords -> falls back to a 6-char stem of the entry NAME ('transc')
        reg = {"transcript_other": {"doc_type": "other", "picker_query": "transcript"}}
        self.assertEqual(A._match_other_doc("transcipt if available", reg)[0], "transcript_other")

    def test_skips_underscore_entries(self):
        reg = {"_note": {"doc_type": "other", "instruction_keywords": ["transc"]}}
        self.assertIsNone(A._match_other_doc("transcript", reg))


class TestInstructionExtraction(unittest.TestCase):
    def test_extracts_scoped_instruction(self):
        modal = ("Attach your resume\nResume.pdf\n"
                 "Attach other required documents\n"
                 "Instructions from employer:Transcipt if available\n"
                 "Search your other documents")
        self.assertEqual(A._other_docs_instruction(modal), "Transcipt if available")

    def test_no_instruction(self):
        self.assertEqual(A._other_docs_instruction("Attach your resume\nResume.pdf"), "")


class TestAttachedOtherDocName(unittest.TestCase):
    def test_empty_section_returns_none(self):
        modal = _FakeModal(
            "Attach other required documents\n"
            "Instructions from employer:Transcipt if available\n"
            "Search your other documents\nor\nUpload new\nPlease enter a valid response")
        self.assertIsNone(A._attached_other_doc_name(modal))

    def test_attached_returns_doc_name(self):
        modal = _FakeModal(
            "Attach other required documents\n"
            "Instructions from employer:Transcipt if available\n"
            "Transcript\nPreview document")
        self.assertEqual(A._attached_other_doc_name(modal), "Transcript")


if __name__ == "__main__":
    unittest.main()
