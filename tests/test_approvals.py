from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reconcile import BELLHAVEN, build_proposals
from simulation import SimulatedCRM, simulate
from sources import SourceError
from storage import latest_snapshot, save_review, save_snapshot
from test_review import account, facility


class StartupTests(unittest.TestCase):
    def test_streamlit_starts_in_fresh_python_process(self):
        root = Path(__file__).resolve().parents[1]
        code = """
from unittest.mock import patch
from streamlit.testing.v1 import AppTest

with patch("storage.latest_snapshot", return_value=None):
    app = AppTest.from_file("app.py").run()
    assert not app.exception, app.exception
    assert app.title[0].value == "Bellhaven CRM review"
"""
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=root, capture_output=True,
            text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "review.sqlite3"
        self.site = facility()
        self.parent = account(BELLHAVEN, parent="")
        self.old = account("fake-old", self.site["name"], "10 W Main St",
                           parent="fake-old-parent", revenue=10, ar=3)

    def collect(self, *records, site=None):
        site = site or self.site
        rows = build_proposals([site], [self.parent, *records])
        snapshot = save_snapshot([site], len(records) + 1, rows, self.path)
        return snapshot, rows[0]

    def decide(self, snapshot, item, state, confirm=False):
        save_review(snapshot, item["id"], state, ownership_confirmed=confirm, path=self.path)
        return latest_snapshot(self.path)["proposals"][0]

    def test_approval_requires_confirmation_and_is_deliberate(self):
        snapshot, item = self.collect(self.old)
        with self.assertRaisesRegex(ValueError, "Confirm the legal operator"):
            self.decide(snapshot, item, "approved")
        self.assertEqual(latest_snapshot(self.path)["proposals"][0]["review_state"], "pending")
        saved = self.decide(snapshot, item, "approved", True)
        self.assertEqual(saved["review_state"], "approved")
        self.assertTrue(saved["reviewed_at"])
        self.assertTrue(saved["ownership_confirmed"])
        with self.assertRaisesRegex(ValueError, "already been recorded"):
            self.decide(snapshot, item, "rejected")

    def test_identical_daily_rerun_and_material_change(self):
        snapshot, item = self.collect(self.old)
        approved = self.decide(snapshot, item, "approved", True)
        next_day = dict(self.site, observation_date="2026-11-11")
        self.collect(self.old, site=next_day)
        current = latest_snapshot(self.path)["proposals"][0]
        self.assertEqual(current["id"], approved["id"])
        self.assertEqual(current["reviewed_at"], approved["reviewed_at"])
        self.assertEqual(current["review_state"], "approved")
        self.collect(dict(self.old, outstanding_ar=0), site=next_day)
        current = latest_snapshot(self.path)["proposals"][0]
        self.assertNotEqual(current["id"], approved["id"])
        self.assertEqual(current["review_state"], "pending")

    def test_outdated_snapshot_cannot_be_reviewed(self):
        first, item = self.collect(self.old)
        self.collect(dict(self.old, outstanding_ar=0))
        with self.assertRaisesRegex(ValueError, "latest snapshot"):
            self.decide(first, item, "approved", True)

    def test_duplicate_requires_canonical_account_investigation(self):
        snapshot, item = self.collect(self.old, dict(self.old, account_id="fake-other"))
        self.assertEqual(item["category"], "Duplicate candidate")
        with self.assertRaisesRegex(ValueError, "requires investigation"):
            self.decide(snapshot, item, "approved", True)
        crm = SimulatedCRM(item["crm"])
        self.assertEqual(simulate(dict(item, review_state="approved"), crm).state, "failed")
        self.assertEqual(len(crm.records), 2)

    def test_rejected_proposal_never_executes(self):
        snapshot, item = self.collect(self.old)
        rejected = self.decide(snapshot, item, "rejected")
        crm = SimulatedCRM([self.old])
        self.assertEqual(simulate(rejected, crm).state, "rejected")
        self.assertEqual(crm.read("fake-old"), self.old)

    def test_positive_revenue_and_ar_retry_links_one_new_account(self):
        snapshot, item = self.collect(self.old)
        approved = self.decide(snapshot, item, "approved", True)
        crm = SimulatedCRM([self.old], fail_first_link=True)
        failed = simulate(approved, crm)
        self.assertEqual(failed.state, "failed")
        self.assertEqual(crm.read("fake-old"), self.old)
        self.assertEqual(len(crm.records), 2)
        retried = simulate(approved, crm)
        self.assertEqual(retried.state, "applied")
        self.assertEqual(failed.created_account_id, retried.created_account_id)
        self.assertEqual(crm.read("fake-old"),
                         dict(self.old, chow_current_account=retried.created_account_id))
        self.assertEqual(crm.read(retried.created_account_id),
                         dict(item["proposed_change"]["new_account"], account_id=retried.created_account_id))
        self.assertEqual(simulate(approved, crm).state, "applied")
        self.assertEqual(len(crm.records), 2)
        self.assertEqual(latest_snapshot(self.path)["proposals"][0]["review_state"], "approved")

    def test_revenue_only_or_ar_only_reparents_directly(self):
        for revenue, ar in ((10, 0), (0, 3)):
            with self.subTest(revenue=revenue, ar=ar):
                old = dict(self.old, lifetime_revenue=revenue, outstanding_ar=ar)
                snapshot, item = self.collect(old)
                approved = self.decide(snapshot, item, "approved", True)
                crm = SimulatedCRM([old])
                self.assertEqual(simulate(approved, crm).state, "applied")
                self.assertEqual(crm.read("fake-old"), dict(old, parent_id=BELLHAVEN))
                self.assertEqual(len(crm.records), 1)

    def test_outdated_simulated_record_prevents_creation_or_update(self):
        for ar in (0, 3):
            old = dict(self.old, outstanding_ar=ar)
            snapshot, item = self.collect(old)
            approved = self.decide(snapshot, item, "approved", True)
            changed = dict(old, lifetime_revenue=99)
            crm = SimulatedCRM([changed])
            result = simulate(approved, crm)
            self.assertEqual(result.state, "failed")
            self.assertIn("evidence changed", result.message)
            self.assertEqual(crm.read("fake-old"), changed)
            self.assertEqual(len(crm.records), 1)

    def test_incomplete_scrape_keeps_previous_snapshot_in_ui(self):
        from streamlit.testing.v1 import AppTest

        original, _ = self.collect(self.old)
        with (
            patch("sources.scrape_facilities", side_effect=SourceError("Incomplete website scrape.")),
            patch("sources.read_accounts") as read,
            patch("storage.latest_snapshot", side_effect=lambda: latest_snapshot(self.path)),
            patch("storage.save_snapshot") as save,
        ):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.error[0].value, "Incomplete website scrape.")
            read.assert_not_called()
            save.assert_not_called()
        self.assertEqual(latest_snapshot(self.path)["id"], original)


    def test_streamlit_requires_confirmation_and_records_local_approval(self):
        from streamlit.testing.v1 import AppTest

        self.collect(self.old)
        with (
            patch("storage.latest_snapshot", side_effect=lambda: latest_snapshot(self.path)),
            patch("storage.save_review", side_effect=lambda *args, **kwargs:
                  save_review(*args, **kwargs, path=self.path)),
        ):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            approve = next(button for button in app.button if button.label == "Approve")
            approve.click().run()
            self.assertIn("Confirm the legal operator", app.error[0].value)
            app.checkbox[0].check()
            next(button for button in app.button if button.label == "Approve").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(latest_snapshot(self.path)["proposals"][0]["review_state"], "approved")
            app.selectbox[1].select("Approved").run()
            self.assertIn("Approved locally · Not applied to CRM", app.success[0].value)

    def test_stale_record_after_partial_failure_preserves_single_replacement(self):
        snapshot, item = self.collect(self.old)
        approved = self.decide(snapshot, item, "approved", True)
        crm = SimulatedCRM([self.old], fail_first_link=True)
        first = simulate(approved, crm)
        self.assertEqual(first.state, "failed")
        crm.update_if_current(self.old, {"outstanding_ar": 100})
        retry = simulate(approved, crm)
        self.assertEqual(retry.state, "failed")
        self.assertEqual(first.created_account_id, retry.created_account_id)
        self.assertEqual(len(crm.records), 2)
        self.assertNotIn("chow_current_account", crm.read("fake-old"))
