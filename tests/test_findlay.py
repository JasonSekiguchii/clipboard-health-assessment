from contextlib import closing
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crm_write import ACCOUNT_PATH, CRMWriteClient, WriteBlocked
from execution import apply_findlay_mock, confirm_findlay_dry_run
from reconcile import BELLHAVEN, BELLHAVEN_PARENT_NAME, FINDLAY_ID, FINDLAY_NAME, build_proposals, is_actionable
from sources import BASE_URL, FINDLAY
from storage import connect, latest_snapshot, save_review, save_snapshot
from test_execution import FakeTransport, complete
from test_review import account, facility


class FindlayTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "assessment.sqlite3"
        env = patch.dict("os.environ", {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        storage = patch("storage.DB_PATH", self.path)
        storage.start()
        self.addCleanup(storage.stop)
        self.site = dict(facility(FINDLAY_NAME, "1800 N Blanchard St", "45840"),
                         city="Findlay", source_url=BASE_URL + FINDLAY,
                         care_offerings=["Assisted Living", "Memory Support"])
        self.parent = complete(dict(account(BELLHAVEN, BELLHAVEN_PARENT_NAME, parent=""), status="Active"))
        self.old = complete(dict(account(FINDLAY_ID, FINDLAY_NAME, self.site["street"], city="Findlay",
                                         zip_code="45840", parent="", revenue=22000),
                                 status="Active", care_type="Assisted Living"))

    def plan(self, row=None, site=None):
        return build_proposals([site or self.site], [self.parent, row or self.old])[0]

    def save(self, item=None, *, approve=False):
        snapshot = save_snapshot([self.site], 2, [item or self.plan()], self.path)
        proposal_id = latest_snapshot(self.path)["proposals"][0]["id"]
        if approve:
            save_review(snapshot, proposal_id, "approved", ownership_confirmed=True, live_review=True, path=self.path)
        return snapshot, proposal_id

    def phrase(self, proposal_id):
        return f"APPLY {proposal_id} {FINDLAY_ID}"

    def execute(self, transport, snapshot, proposal_id):
        return apply_findlay_mock(CRMWriteClient(transport, mock=True), snapshot,
                                  proposal_id, self.phrase(proposal_id), self.path)

    def test_direct_patch_preserves_financial_fields_and_duplicate_click_is_safe(self):
        snapshot, proposal_id = self.save(approve=True)
        transport = FakeTransport(self.parent, self.old)
        transport.rows["ashland-fixture"] = {"name": "Bellhaven Healthcare Centre of Ashland"}
        ashland = deepcopy(transport.rows["ashland-fixture"])
        for _ in range(2):
            self.assertEqual(self.execute(transport, snapshot, proposal_id)["state"], "applied")
        self.assertEqual(transport.mutations(), [
            ("PATCH", f"{ACCOUNT_PATH}/{FINDLAY_ID}", {"parent_id": BELLHAVEN})
        ])
        self.assertEqual(transport.rows[FINDLAY_ID], dict(
            self.old, parent_id=BELLHAVEN, parent_name=BELLHAVEN_PARENT_NAME,
            updated_at="fake-updated-after-patch",
        ))
        self.assertEqual(transport.rows["ashland-fixture"], ashland)

    def test_positive_revenue_and_ar_stays_chow_and_nonzero_negative_balances_block(self):
        chow = self.plan(dict(self.old, outstanding_ar=5))
        self.assertEqual(chow["proposed_change"]["strategy"], "preserve_create_and_link")
        snapshot, proposal_id = self.save(chow)
        with self.assertRaisesRegex(ValueError, "outside the scoped"):
            save_review(snapshot, proposal_id, "approved", ownership_confirmed=True, live_review=True, path=self.path)
        negative = self.plan(dict(self.old, outstanding_ar=-5))
        self.assertFalse(is_actionable(negative))

    def test_wrong_account_or_parent_plan_cannot_receive_scoped_approval(self):
        wrong_account = self.plan(dict(self.old, account_id="different-account"))
        wrong_parent = self.plan()
        wrong_parent["proposed_change"]["existing_account_changes"] = {"parent_id": "different-parent"}
        for item in (wrong_account, wrong_parent):
            snapshot, proposal_id = self.save(item)
            with self.assertRaisesRegex(ValueError, "outside the scoped"):
                save_review(snapshot, proposal_id, "approved", ownership_confirmed=True,
                            live_review=True, path=self.path)
            with self.assertRaisesRegex(WriteBlocked, "exact Findlay"):
                confirm_findlay_dry_run(snapshot, proposal_id, self.phrase(proposal_id), self.path)

    def test_stale_account_or_wrong_parent_readback_blocks_patch(self):
        snapshot, proposal_id = self.save(approve=True)
        for parent, account_row in (
            (self.parent, dict(self.old, outstanding_ar=1)),
            (dict(self.parent, name="Unrelated Parent"), self.old),
        ):
            transport = FakeTransport(parent, account_row)
            self.assertEqual(self.execute(transport, snapshot, proposal_id)["state"], "needs_investigation")
            self.assertEqual(transport.mutations(), [])

    def test_refresh_preserves_decision_and_requires_fresh_scoped_confirmation(self):
        snapshot, proposal_id = self.save(approve=True)
        transport = FakeTransport(self.parent, self.old)
        self.assertEqual(self.execute(transport, snapshot, proposal_id)["state"], "applied")
        newer, same_id = self.save(self.plan(site=dict(self.site, observation_date="2026-11-11")))
        self.assertEqual(same_id, proposal_id)
        self.assertEqual(latest_snapshot(self.path)["proposals"][0]["review_state"], "approved")
        with self.assertRaisesRegex(WriteBlocked, "Fresh local"):
            self.execute(transport, newer, proposal_id)
        save_review(newer, proposal_id, "approved", ownership_confirmed=True, live_review=True, path=self.path)
        self.assertEqual(self.execute(transport, newer, proposal_id)["state"], "applied")
        self.assertEqual(len(transport.mutations()), 1)

    def test_unknown_patch_reconciles_without_resend_and_rejected_patch_stays_failed(self):
        snapshot, proposal_id = self.save(approve=True)
        transport = FakeTransport(self.parent, self.old)
        transport.reject.add("PATCH")
        self.assertEqual(self.execute(transport, snapshot, proposal_id)["state"], "failed")
        self.assertEqual(transport.rows[FINDLAY_ID], self.old)
        transport.reject.clear()
        transport.timeout_after.add("PATCH")
        self.assertEqual(self.execute(transport, snapshot, proposal_id)["state"], "needs_investigation")
        self.assertEqual(self.execute(transport, snapshot, proposal_id)["state"], "applied")
        self.assertEqual([method for method, *_ in transport.mutations()], ["PATCH", "PATCH"])

    def test_real_parent_patch_is_blocked_even_with_ashland_switches(self):
        snapshot, proposal_id = self.save(approve=True)
        transport = FakeTransport(self.parent, self.old)
        client = CRMWriteClient(transport)
        with patch.dict("os.environ", {"CLIPBOARD_LIVE_WRITES": "1",
                                       "CLIPBOARD_ASHLAND_CANARY": "001RJ0D7Z5MAZN5HX5"}):
            with self.assertRaisesRegex(WriteBlocked, "mock-only"):
                apply_findlay_mock(client, snapshot, proposal_id, self.phrase(proposal_id), self.path)
            with self.assertRaisesRegex(WriteBlocked, "Live parent"):
                client.patch(FINDLAY_ID, {"parent_id": BELLHAVEN}, proposal_id=proposal_id,
                             confirmation=proposal_id)
        self.assertEqual(transport.requests, [])

    def test_findlay_live_mode_disabled_cannot_open_confirmation(self):
        from streamlit.testing.v1 import AppTest

        self.save()
        with patch("execution.LiveCRMTransport", side_effect=AssertionError("Unexpected CRM transport")):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            self.assertFalse(app.exception)
            self.assertTrue(next(button for button in app.button if button.label == "Approve & Apply").disabled)
            self.assertFalse(any(button.label == "Confirm Findlay dry run" for button in app.button))
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)
