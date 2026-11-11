import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reconcile import BELLHAVEN, build_proposals
from storage import latest_snapshot, save_review, save_snapshot
from test_review import account, facility


class QAIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.qa = Path(directory.name) / "qa.sqlite3"
        self.assessment = Path(directory.name) / "assessment.sqlite3"
        self.site = facility()
        self.parent = account(BELLHAVEN, parent="")
        self.record = account("fixture-account", "Bellhaven at Town", "10 W Main St")

    def proposals(self, *records):
        return build_proposals([self.site], [self.parent, *records])

    def test_env_selects_separate_database_and_rejects_default_alias(self):
        rows = self.proposals(self.record)
        with patch("storage.DB_PATH", self.assessment), patch.dict(
            os.environ, CLIPBOARD_REVIEW_DB_PATH=str(self.qa)
        ):
            save_snapshot([self.site], 2, rows)
            self.assertEqual(latest_snapshot()["account_count"], 2)
            self.assertFalse(self.assessment.exists())
            self.assessment.write_bytes(b"untouched assessment file")
            self.qa.with_name("alias.sqlite3").symlink_to(self.assessment)
            for alias in (self.assessment, self.qa.with_name("alias.sqlite3")):
                with patch.dict(os.environ, CLIPBOARD_REVIEW_DB_PATH=str(alias)):
                    with self.assertRaises(sqlite3.OperationalError):
                        latest_snapshot()
            self.assertEqual(self.assessment.read_bytes(), b"untouched assessment file")

    def test_rejection_survives_fresh_process_and_repeat_run(self):
        rows = self.proposals(self.record)
        with patch.dict(os.environ, CLIPBOARD_REVIEW_DB_PATH=str(self.qa)):
            first = save_snapshot([self.site], 2, rows)
            save_review(first, rows[0]["id"], "rejected")
            later = dict(self.site, observation_date="2026-11-11")
            unchanged = build_proposals([later], [self.parent, self.record])
            save_snapshot([later], 2, unchanged)
            code = (
                "from storage import latest_snapshot; "
                "rows = latest_snapshot()['proposals']; "
                "assert len(rows) == 1 and rows[0]['review_state'] == 'rejected' "
                "and rows[0]['reviewed_at']"
            )
            result = subprocess.run([sys.executable, "-c", code],
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            changed = self.proposals(dict(self.record, outstanding_ar=1))
            self.assertNotEqual(changed[0]["id"], rows[0]["id"])
            save_snapshot([self.site], 2, changed)
            self.assertEqual(latest_snapshot()["proposals"][0]["review_state"], "pending")

    def test_ambiguous_approval_is_disabled_and_blocked_in_qa(self):
        from streamlit.testing.v1 import AppTest

        rows = self.proposals(self.record, dict(self.record, account_id="competing", parent_id="other"))
        self.assertEqual(rows[0]["category"], "Ambiguous address overlap")
        with patch.dict(os.environ, CLIPBOARD_REVIEW_DB_PATH=str(self.qa)):
            snapshot = save_snapshot([self.site], 3, rows)
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.warning[0].value, "TEST MODE — NO CRM WRITES")
            self.assertTrue(next(button for button in app.button if button.label == "Approve").disabled)
            with self.assertRaisesRegex(ValueError, "requires investigation"):
                save_review(snapshot, rows[0]["id"], "approved", ownership_confirmed=True)
            self.assertEqual(latest_snapshot()["proposals"][0]["review_state"], "pending")
