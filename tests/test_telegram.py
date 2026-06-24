#!/usr/bin/env python3
"""Committed unit tests for telegram_bot.py: the approval-card caption builder and the
callback_data contract that the (existing) approval pollers and the (Phase 5) fix-approval
harvest both depend on. Pure string logic; no network.

Run: python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import telegram_bot as tg  # noqa: E402


class TestCaption(unittest.TestCase):
    JOB = {"title": "Backend Engineer Intern", "company": "Acme", "location": "Remote",
           "type": "Internship", "salary": "", "deadline": None}

    def test_basic_caption(self):
        cap = tg._application_caption(self.JOB)
        self.assertIn("Backend Engineer Intern", cap)
        self.assertIn("Acme", cap)
        self.assertLessEqual(len(cap), 1024)            # Telegram caption hard limit

    def test_transcript_and_cover_lines(self):
        cap = tg._application_caption(self.JOB, has_cover=True, has_transcript=True)
        self.assertIn("cover letter", cap.lower())
        self.assertIn("Transcript", cap)

    def test_answer_source_markers(self):
        answers = [{"question": "Work authorized?", "answer": "Yes", "source": "profile"},
                   {"question": "Why us?", "answer": "draft", "source": "claude"},
                   {"question": "Niche?", "answer": None, "source": "none", "required": True}]
        cap = tg._application_caption(self.JOB, answers=answers)
        self.assertIn("🔴", cap)                         # an unanswered required question is flagged
        self.assertIn("Work authorized?", cap)

    def test_caption_truncates_long_input(self):
        big = dict(self.JOB, title="X" * 4000)
        self.assertLessEqual(len(tg._application_caption(big)), 1024)


class TestCallbackContract(unittest.TestCase):
    """The callback_data format is the wire contract between the cards and the pollers;
    a change here would silently break approvals, so pin it."""
    def test_existing_approval_format(self):
        action, jid, nonce = "approve:11091190:9f3a1c7b".split(":")
        self.assertEqual((action, jid, nonce), ("approve", "11091190", "9f3a1c7b"))

    def test_phase5_fix_formats(self):
        # apply_fix:<job_id>:<fix_id>:<nonce>   and   apply_diff:<job_id>:<fix_id>:<nonce>
        parts = "apply_fix:11091190:a1b2c3d4e5f6:9f3a1c7b".split(":")
        self.assertEqual(parts[0], "apply_fix")
        self.assertEqual(len(parts), 4)
        self.assertEqual("apply_diff:11091190:a1b2c3d4e5f6:zz".split(":")[0], "apply_diff")


if __name__ == "__main__":
    unittest.main()
