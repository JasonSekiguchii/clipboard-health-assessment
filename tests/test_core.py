from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from crm_write import ACCOUNT_PATH, CRMWriteClient, WriteBlocked
from execution import apply_approved
from pipeline import run_pipeline
from reconcile import BELLHAVEN, build_proposals, business_id
from sources import SourceError
from storage import connect, latest_snapshot, save_resolution, save_review, save_snapshot
from test_execution import FakeTransport, complete
from test_review import account, facility


class CoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "core.sqlite3"
        self.site = facility()
        self.parent = complete(dict(account(BELLHAVEN, "Bellhaven Senior Living", parent=""), status="Active"))
        self.old = complete(dict(account("old", self.site["name"], self.site["street"], parent="previous",
                                         revenue=100, ar=20), status="Active", care_type="Memory Care"))
        self.details = {
            "name": self.site["name"], "parent_id": BELLHAVEN,
            **{f"billing_{key}": self.site[key] for key in ("street", "city", "state", "zip")},
            "care_type": "Memory Care", "status": "Active",
        }

    def seed(self, records, *, site=None, facilities=None):
        sites = [site or self.site] if facilities is None else facilities
        rows = [self.parent, *records]
        proposals = build_proposals(sites, rows)
        snapshot = save_snapshot(sites, len(rows), proposals, self.path, accounts=rows)
        return snapshot, latest_snapshot(self.path)["proposals"]

    def resolved(self, records, *, operation="update", field="note", value="Reviewed note",
                 details=None, target_id="old", sites=None):
        snapshot, proposals = self.seed(records, facilities=sites)
        source = proposals[0]
        proposal_id = save_resolution(snapshot, source["id"], reason="Reviewer verified facility and source records.",
                                     operation=operation, target_id=target_id, field=field, value=value,
                                     details=details, ownership_confirmed=True, path=self.path)
        return snapshot, proposal_id, source

    def approve(self, snapshot, proposal_id):
        save_review(snapshot, proposal_id, "approved", ownership_confirmed=True,
                    reason="Reviewed the exact payload.", path=self.path)

    def execute(self, transport, proposal_id, confirmation=None):
        return apply_approved(CRMWriteClient(transport, mock=True), proposal_id,
                              confirmation=proposal_id if confirmation is None else confirmation, path=self.path)

    def test_creation_and_get_verification_with_no_existing_account(self):
        snapshot, proposal_id, _ = self.resolved([], operation="create", details=self.details)
        self.approve(snapshot, proposal_id)
        transport = FakeTransport(self.parent)
        self.assertEqual(self.execute(transport, proposal_id)["state"], "applied")
        self.assertEqual(transport.mutations(), [("POST", ACCOUNT_PATH, self.details)])
        self.assertEqual(transport.requests[-1], ("GET", f"{ACCOUNT_PATH}/fake-created", None))
        self.assertEqual(self.execute(transport, proposal_id)["state"], "applied")
        self.assertEqual(len(transport.mutations()), 1)
        self.assertEqual(build_proposals([self.site], list(transport.rows.values())), [])

    def test_absence_check_blocks_matching_account_created_since_review(self):
        snapshot, proposal_id, _ = self.resolved([], operation="create", details=self.details)
        self.approve(snapshot, proposal_id)
        transport = FakeTransport(self.parent, self.old)
        self.assertEqual(self.execute(transport, proposal_id)["state"], "needs_investigation")
        self.assertEqual(transport.mutations(), [])

    def test_unknown_creation_never_reposts_and_authoritative_receipt_recovers(self):
        snapshot, proposal_id, _ = self.resolved([], operation="create", details=self.details)
        self.approve(snapshot, proposal_id)
        transport = FakeTransport(self.parent)
        transport.timeout_after.add("POST")
        self.assertEqual(self.execute(transport, proposal_id)["state"], "needs_investigation")
        transport.find_receipts = False
        self.assertEqual(self.execute(transport, proposal_id)["state"], "needs_investigation")
        transport.find_receipts = True
        self.assertEqual(self.execute(transport, proposal_id)["state"], "applied")
        self.assertEqual(len(transport.mutations()), 1)

    def test_chow_created_id_survives_refresh_and_failed_link(self):
        snapshot, proposal_id, source = self.resolved([self.old], field="parent_id", value=BELLHAVEN)
        self.approve(snapshot, proposal_id)
        transport = FakeTransport(self.parent, self.old)
        transport.reject.add("PATCH")
        result = self.execute(transport, proposal_id)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(transport.rows["old"], self.old)
        sites = [dict(self.site, observation_date="2026-11-12")]
        rows = list(transport.rows.values())
        save_snapshot(sites, len(rows), build_proposals(sites, rows), self.path, accounts=rows)
        transport.reject.clear()
        self.assertEqual(self.execute(transport, proposal_id)["state"], "applied")
        self.assertEqual(sum(method == "POST" for method, *_ in transport.mutations()), 1)
        expected = dict(self.old, chow_current_account="fake-created", updated_at="fake-updated-after-patch")
        self.assertEqual(transport.rows["old"], expected)
        self.assertEqual(build_proposals(sites, list(transport.rows.values())), [])

    def test_direct_parent_branch_for_either_zero_balance(self):
        for revenue, ar in ((100, 0), (0, 20), (0, 0)):
            old = dict(self.old, lifetime_revenue=revenue, outstanding_ar=ar)
            snapshot, proposal_id, _ = self.resolved([old], field="parent_id", value=BELLHAVEN)
            self.approve(snapshot, proposal_id)
            transport = FakeTransport(self.parent, old)
            self.assertEqual(self.execute(transport, proposal_id)["state"], "applied")
            self.assertEqual(transport.mutations(), [("PATCH", f"{ACCOUNT_PATH}/old", {"parent_id": BELLHAVEN})])
            self.assertEqual(transport.rows["old"]["lifetime_revenue"], revenue)
            self.assertEqual(transport.rows["old"]["outstanding_ar"], ar)

    def test_status_note_and_name_minimal_patches_preserve_other_values(self):
        for field, value in (("status", "Inactive"), ("note", "Confirmed operator review."),
                             ("name", "Verified revised facility name")):
            snapshot, proposal_id, _ = self.resolved([self.old], field=field, value=value)
            self.approve(snapshot, proposal_id)
            transport = FakeTransport(self.parent, self.old)
            self.assertEqual(self.execute(transport, proposal_id)["state"], "applied")
            self.assertEqual(transport.mutations(), [("PATCH", f"{ACCOUNT_PATH}/old", {field: value})])
            self.assertEqual(transport.rows["old"],
                             dict(self.old, **{field: value}, updated_at="fake-updated-after-patch"))

    def test_duplicate_link_requires_review_and_suppresses_completed_duplicate(self):
        old = dict(self.old, parent_id=BELLHAVEN)
        canonical = dict(old, account_id="canonical")
        snapshot, proposal_id, _ = self.resolved([old, canonical], field="duplicate_of_account", value="canonical")
        transport = FakeTransport(self.parent, old, canonical)
        with self.assertRaises(WriteBlocked):
            self.execute(transport, proposal_id)
        self.approve(snapshot, proposal_id)
        self.assertEqual(self.execute(transport, proposal_id)["state"], "applied")
        self.assertEqual(build_proposals([self.site], list(transport.rows.values())), [])
        cyclic = dict(canonical, duplicate_of_account="old")
        # A separate reviewed state must not create a relationship cycle.
        snapshot, proposal_id, _ = self.resolved([dict(old, name="Legacy facility"), cyclic], field="duplicate_of_account", value="canonical")
        self.approve(snapshot, proposal_id)
        transport = FakeTransport(self.parent, dict(old, name="Legacy facility"), cyclic)
        self.assertEqual(self.execute(transport, proposal_id)["state"], "needs_investigation")
        self.assertEqual(transport.mutations(), [])

    def test_manual_chow_link_preserves_existing_parent_and_finances(self):
        replacement = complete(dict(self.details, account_id="replacement"))
        snapshot, proposal_id, _ = self.resolved([self.old, replacement],
                                               field="chow_current_account", value="replacement")
        self.approve(snapshot, proposal_id)
        transport = FakeTransport(self.parent, self.old, replacement)
        self.assertEqual(self.execute(transport, proposal_id)["state"], "applied")
        self.assertEqual(transport.rows["old"], dict(self.old, chow_current_account="replacement",
                                                  updated_at="fake-updated-after-patch"))

    def test_rejection_and_unresolved_reasons_persist_without_mutations(self):
        snapshot, proposals = self.seed([self.old])
        proposal_id = proposals[0]["id"]
        save_review(snapshot, proposal_id, "unresolved", reason="Need operator evidence.", path=self.path)
        self.assertEqual(latest_snapshot(self.path)["proposals"][0]["review_state"], "unresolved")
        self.assertEqual(latest_snapshot(self.path)["proposals"][0]["decision_reason"], "Need operator evidence.")
        save_review(snapshot, proposal_id, "rejected", reason="Retain current parent.", path=self.path)
        transport = FakeTransport(self.parent, self.old)
        with self.assertRaises(WriteBlocked):
            self.execute(transport, proposal_id)
        self.assertEqual(transport.requests, [])

    def test_resolution_requires_reason_and_is_not_auto_approved(self):
        snapshot, proposals = self.seed([self.old])
        with self.assertRaisesRegex(ValueError, "reason"):
            save_resolution(snapshot, proposals[0]["id"], reason="", operation="update",
                            target_id="old", field="note", value="A", path=self.path)
        _, proposal_id, _ = self.resolved([self.old])
        self.assertEqual(next(p for p in latest_snapshot(self.path)["proposals"]
                              if p["id"] == proposal_id)["review_state"], "pending")

    def test_pipeline_preserves_resolution_and_decision_until_business_changes(self):
        snapshot, proposal_id, source = self.resolved([self.old], field="note", value="Verified")
        self.approve(snapshot, proposal_id)
        metadata_changed = dict(self.old, updated_at="new observation", phone="555-0100")
        sites = [dict(self.site, observation_date="2026-11-12")]
        run_pipeline(self.path, scrape=lambda: sites, read=lambda: [self.parent, metadata_changed])
        current = latest_snapshot(self.path)["proposals"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["id"], proposal_id)
        self.assertEqual(current[0]["review_state"], "approved")
        run_pipeline(self.path, scrape=lambda: sites, read=lambda: [self.parent, dict(metadata_changed, outstanding_ar=8)])
        current = latest_snapshot(self.path)["proposals"]
        self.assertFalse(any(row["id"] == proposal_id for row in current))
        self.assertTrue(all(row["review_state"] == "pending" for row in current))

    def test_incomplete_daily_scrape_does_not_replace_complete_snapshot(self):
        first, _ = self.seed([self.old])
        reader = Mock(side_effect=AssertionError("Must not read CRM after incomplete scrape"))
        def incomplete():
            raise SourceError("Incomplete fixture scrape.")
        with self.assertRaises(SourceError):
            run_pipeline(self.path, scrape=incomplete, read=reader)
        self.assertEqual(latest_snapshot(self.path)["id"], first)
        reader.assert_not_called()

    def test_daily_pipeline_cannot_approve_or_write(self):
        with patch("crm_write.CRMWriteClient.patch", side_effect=AssertionError("Unexpected PATCH")), patch(
            "crm_write.CRMWriteClient.create", side_effect=AssertionError("Unexpected POST")
        ):
            run_pipeline(self.path, scrape=lambda: [self.site], read=lambda: [self.parent, self.old])
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)


    def test_resolution_ui_records_creation_without_approving_or_writing(self):
        from streamlit.testing.v1 import AppTest

        self.seed([])
        with patch.dict("os.environ", {"CLIPBOARD_REVIEW_DB_PATH": str(self.path)}):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            next(widget for widget in app.selectbox if widget.label == "Operation").select("create").run()
            next(widget for widget in app.text_area if widget.label == "Complete account details for creation (JSON)").set_value(
                json.dumps(self.details)
            )
            next(widget for widget in app.text_area if widget.label == "Resolution reason and supporting evidence").set_value(
                "Checked complete CRM list and public facility details."
            )
            next(button for button in app.button if button.label == "Save resolved proposal for review").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
        proposals = latest_snapshot(self.path)["proposals"]
        created = next(row for row in proposals if row.get("review_resolution"))
        self.assertEqual(created["proposed_change"]["new_account"], self.details)
        self.assertEqual(created["review_state"], "pending")
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)

    def test_generic_adapter_is_blocked_by_contract_without_any_http(self):
        from reviewed_crm import ReviewedAuthorization, ReviewedCRMTransport
        from test_canary import CanaryOpener

        snapshot, proposal_id, _ = self.resolved([self.old], field="note", value="Verified")
        permit = ReviewedAuthorization.confirm(proposal_id, self.old, {"note": "Verified"}, None,
                                                accepted=True, typed="APPLY")
        opener = CanaryOpener(self.old)
        with patch.dict("os.environ", {"CLIPBOARD_REVIEWED_WRITES": "1", "CLIPBOARD_LIVE_WRITES": "1"}, clear=True):
            transport = ReviewedCRMTransport(permit, opener=opener)
            with self.assertRaises(WriteBlocked):
                transport.request("PATCH", f"{ACCOUNT_PATH}/old", data={"note": "Verified"}, operation_id=proposal_id)
        self.assertEqual(opener.requests, [])

    def test_generic_adapter_note_patch_and_read_only_recovery_use_mock_http(self):
        from execution import reconcile_live_canary
        from reviewed_crm import ReviewedAuthorization, ReviewedCRMTransport
        from test_canary import CanaryOpener

        snapshot, proposal_id, _ = self.resolved([self.old], field="note", value="Verified")
        opener = CanaryOpener(self.old)
        opener.timeout = True
        permit = ReviewedAuthorization.confirm(proposal_id, self.old, {"note": "Verified"}, None,
                                                accepted=True, typed="APPLY")
        with (
            patch("storage.DB_PATH", self.path), patch("execution.DB_PATH", self.path),
            patch("reviewed_crm.VERIFIED_PATCH_FIELDS", frozenset({"note"})),
            patch.dict("os.environ", {"CLIPBOARD_REVIEWED_WRITES": "1", "CLIPBOARD_LIVE_WRITES": "1",
                                     "CLIPBOARD_API_TOKEN": "fixture"}, clear=True),
        ):
            save_review(snapshot, proposal_id, "approved", reason="Explicit note correction",
                        live_review=True, path=self.path)
            client = CRMWriteClient(ReviewedCRMTransport(permit, opener=opener))
            result = apply_approved(client, proposal_id, confirmation=proposal_id, path=self.path)
            self.assertEqual(result["state"], "needs_investigation")
            self.assertEqual(reconcile_live_canary(client, proposal_id, self.path), "applied")
        self.assertEqual(sum(method == "PATCH" for method, *_ in opener.requests), 1)
        self.assertEqual(opener.record["note"], "Verified")
        self.assertEqual(opener.record["lifetime_revenue"], 100)

    def test_creation_contract_must_cover_both_post_and_chow_link_before_dispatch(self):
        from reviewed_crm import ReviewedAuthorization, ReviewedCRMTransport

        snapshot, proposal_id, _ = self.resolved([self.old], field="parent_id", value=BELLHAVEN)
        item = next(row for row in latest_snapshot(self.path)["proposals"] if row["id"] == proposal_id)
        plan = item["proposed_change"]
        permit = ReviewedAuthorization.confirm(proposal_id, self.old, plan["existing_account_changes"],
                                                plan["new_account"], accepted=True, typed="APPLY")
        with (
            patch("reviewed_crm.CREATION_CONTRACT_VERIFIED", True),
            patch.dict("os.environ", {"CLIPBOARD_REVIEWED_WRITES": "1"}, clear=True),
        ):
            transport = ReviewedCRMTransport(permit)
            with self.assertRaisesRegex(WriteBlocked, "complete operation"):
                transport.verify_live_plan(proposal_id, self.old, plan["existing_account_changes"], plan["new_account"])


    def test_related_account_drift_blocks_duplicate_change(self):
        old = dict(self.old, parent_id=BELLHAVEN)
        canonical = dict(old, account_id="canonical")
        snapshot, proposal_id, _ = self.resolved([old, canonical], field="duplicate_of_account", value="canonical")
        self.approve(snapshot, proposal_id)
        transport = FakeTransport(self.parent, old, dict(canonical, note="New canonical evidence"))
        self.assertEqual(self.execute(transport, proposal_id)["state"], "needs_investigation")
        self.assertEqual(transport.mutations(), [])

    def test_explicit_completed_chow_link_handles_alternate_billing_address(self):
        current = complete(dict(self.details, account_id="new-current"))
        original = dict(self.old, billing_street="PO Box 10", chow_current_account="new-current")
        self.assertEqual(build_proposals([self.site], [self.parent, original, current]), [])
        cycle = dict(current, chow_current_account="old")
        self.assertTrue(build_proposals([self.site], [self.parent, original, cycle]))

    def test_creation_requires_reviewed_details_and_never_accepts_parent_only(self):
        with self.assertRaises(WriteBlocked):
            self.resolved([], operation="create", details={"parent_id": BELLHAVEN})

    def test_unresolved_ui_reason_survives_rerun(self):
        from streamlit.testing.v1 import AppTest

        self.seed([self.old])
        with patch.dict("os.environ", {"CLIPBOARD_REVIEW_DB_PATH": str(self.path)}):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            next(widget for widget in app.text_area if widget.label == "Review reason").set_value(
                "Awaiting dated operator evidence."
            )
            next(button for button in app.button if button.label == "Keep unresolved").click().run()
            self.assertFalse(app.exception)
            app.selectbox[1].select("Unresolved").run()
            self.assertTrue(any("Awaiting dated operator evidence." in entry.value for entry in app.caption))
        item = latest_snapshot(self.path)["proposals"][0]
        self.assertEqual(item["review_state"], "unresolved")
        self.assertTrue(item["reviewed_at"])


    def test_sequential_distinct_reviewed_corrections_reuse_the_live_engine(self):
        from reviewed_crm import ReviewedAuthorization, ReviewedCRMTransport
        from test_canary import CanaryOpener

        current = self.old
        with (
            patch("storage.DB_PATH", self.path), patch("execution.DB_PATH", self.path),
            patch("reviewed_crm.VERIFIED_PATCH_FIELDS", frozenset({"note", "status"})),
            patch.dict("os.environ", {"CLIPBOARD_REVIEWED_WRITES": "1", "CLIPBOARD_LIVE_WRITES": "1",
                                     "CLIPBOARD_API_TOKEN": "fixture"}, clear=True),
        ):
            for field, value in (("note", "Verified"), ("status", "Inactive")):
                snapshot, proposal_id, _ = self.resolved([current], field=field, value=value)
                save_review(snapshot, proposal_id, "approved", reason="Reviewed field correction",
                            live_review=True, path=self.path)
                opener = CanaryOpener(current)
                permit = ReviewedAuthorization.confirm(proposal_id, current, {field: value}, None,
                                                        accepted=True, typed="APPLY")
                result = apply_approved(CRMWriteClient(ReviewedCRMTransport(permit, opener=opener)),
                                        proposal_id, confirmation=proposal_id, path=self.path)
                self.assertEqual(result["state"], "applied")
                self.assertEqual(sum(method == "PATCH" for method, *_ in opener.requests), 1)
                current = opener.record
        self.assertEqual(current["note"], "Verified")
        self.assertEqual(current["status"], "Inactive")
