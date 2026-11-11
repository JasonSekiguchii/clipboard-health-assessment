from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crm_write import ACCOUNT_PATH, CRMWriteClient, WriteBlocked
from execution import apply_approved, reconcile_live_canary
from live_crm import CANARY_ID, CANARY_NEW_NAME, CANARY_OLD_NAME, LiveAuthorization, LiveCRMTransport
from reconcile import BELLHAVEN, build_proposals
from sources import BASE_URL
from storage import latest_snapshot, save_review, save_snapshot
from test_execution import complete
from test_review import account, facility


class CanaryOpener:
    def __init__(self, record):
        self.record = deepcopy(record)
        self.requests = []
        self.timeout = False
        self.ignore_patch = False

    def open(self, request, *, timeout):
        payload = json.loads(request.data) if request.data else None
        method = request.get_method()
        self.requests.append((method, request.full_url, payload))
        if method == "PATCH":
            assert request.get_header("Content-type") == "application/json"
            if not self.ignore_patch:
                self.record.update(payload, updated_at="new timestamp")
            if self.timeout:
                raise TimeoutError()
        response = BytesIO(json.dumps(self.record).encode())
        response.status = 200
        return response


class CanaryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "assessment.sqlite3"
        self.site = dict(facility(CANARY_NEW_NAME), street="1631 Claremont Ave",
                         city="Ashland", zip="44805",
                         source_url=BASE_URL + "/communities/bellhaven-healthcare-centre-of-ashland")
        self.row = complete(dict(account(CANARY_ID, CANARY_OLD_NAME, "1631 Claremont Ave",
                                        city="Ashland", zip_code="44805"),
                                 status="Active", updated_at="old timestamp"))
        self.parent = complete(account(BELLHAVEN, parent=""))

    def proposal(self, row=None, site=None):
        return build_proposals([site or self.site], [self.parent, row or self.row])[0]

    def save(self, item, decision=None, *, live=True):
        snapshot = save_snapshot([self.site], 2, [item], self.path)
        current = latest_snapshot(self.path)["proposals"][0]
        if decision:
            with patch("storage.DB_PATH", self.path), patch.dict("os.environ", {}, clear=True):
                save_review(snapshot, current["id"], decision, live_review=live and decision == "approved",
                            path=self.path)
        return current["id"]

    def test_incidental_metadata_preserves_identity_material_changes_do_not(self):
        old = self.proposal()
        site = dict(self.site, observation_date="2026-11-11", collection_run="ignored")
        current = self.proposal(dict(self.row, updated_at="later", phone="555-0100",
                                     created_by_candidate=True, fetch_time="ignored"), site)
        self.assertEqual(old["id"], current["id"])
        for change in ({"note": "Operator changed"}, {"outstanding_ar": 1}, {"status": "Inactive"}):
            self.assertNotEqual(old["id"], self.proposal(dict(self.row, **change))["id"])

    def test_historical_phase6a_id_and_saved_decision_are_retained(self):
        item = self.proposal()
        old_site = dict(self.site)
        old_site.pop("observation_date")
        legacy_id = hashlib.sha256(json.dumps(
            [item["category"], old_site, [self.row], item["proposed_change"]], sort_keys=True
        ).encode()).hexdigest()[:24]
        self.assertNotEqual(legacy_id, item["id"])
        self.save(dict(item, id=legacy_id), "rejected")
        before = latest_snapshot(self.path)["proposals"][0]
        self.save(self.proposal(dict(self.row, updated_at="new collection", phone="555-0100")))
        after = latest_snapshot(self.path)["proposals"][0]
        self.assertEqual(after["id"], legacy_id)
        self.assertEqual(after["review_state"], "rejected")
        self.assertEqual(after["reviewed_at"], before["reviewed_at"])
        self.save(self.proposal(dict(self.row, note="material new evidence")))
        self.assertEqual(latest_snapshot(self.path)["proposals"][0]["review_state"], "pending")

    def authorize(self, proposal_id, row=None):
        return LiveAuthorization.confirm(proposal_id, row or self.row, f"APPLY {proposal_id} {CANARY_ID}")

    def run_canary(self, proposal_id, opener, authorization, path=None):
        client = CRMWriteClient(LiveCRMTransport(authorization, opener=opener))
        # Only the fixture opener is reachable in these live-adapter tests.
        with (
            patch("crm_write.LIVE_CONTRACT_VERIFIED", True),
            patch("execution.DB_PATH", self.path),
            patch.dict("os.environ", {"CLIPBOARD_LIVE_WRITES": "1", "CLIPBOARD_API_TOKEN": "fixture"}, clear=True),
        ):
            return apply_approved(client, proposal_id, confirmation=proposal_id, path=path or self.path)

    def test_fresh_authorization_and_minimal_patch_followed_by_readback(self):
        proposal_id = self.save(self.proposal(), "approved")
        opener = CanaryOpener(self.row)
        with self.assertRaisesRegex(WriteBlocked, "fresh live reviewer"):
            self.run_canary(proposal_id, opener, None)
        self.assertEqual(opener.requests, [])
        permit = self.authorize(proposal_id)
        self.assertEqual(self.run_canary(proposal_id, opener, permit)["state"], "applied")
        self.assertTrue(permit.used)
        route = BASE_URL + ACCOUNT_PATH + "/" + CANARY_ID
        self.assertEqual(opener.requests, [
            ("GET", route, None), ("GET", route, None),
            ("PATCH", route, {"name": CANARY_NEW_NAME}), ("GET", route, None),
        ])

    def test_separate_qa_database_is_blocked_even_with_permit(self):
        proposal_id = self.save(self.proposal(), "approved")
        opener = CanaryOpener(self.row)
        with self.assertRaisesRegex(WriteBlocked, "assessment database"):
            self.run_canary(proposal_id, opener, self.authorize(proposal_id),
                            self.path.with_name("qa.sqlite3"))
        self.assertEqual(opener.requests, [])

    def test_metadata_refresh_preserves_approval_and_uses_latest_preflight(self):
        proposal_id = self.save(self.proposal(), "approved")
        current = dict(self.row, updated_at="collected again")
        self.assertEqual(self.save(self.proposal(current), "approved"), proposal_id)
        opener = CanaryOpener(current)
        with self.assertRaisesRegex(WriteBlocked, "exact reviewed"):
            self.run_canary(proposal_id, opener, self.authorize(proposal_id))
        self.assertEqual(self.run_canary(proposal_id, opener, self.authorize(proposal_id, current))["state"],
                         "applied")

    def test_stale_live_read_blocks_patch(self):
        proposal_id = self.save(self.proposal(), "approved")
        opener = CanaryOpener(dict(self.row, note="Different operator evidence"))
        result = self.run_canary(proposal_id, opener, self.authorize(proposal_id))
        self.assertEqual(result["state"], "needs_investigation")
        self.assertTrue(all(method == "GET" for method, *_ in opener.requests))

    def test_timeout_verifies_with_get_and_never_resends_patch(self):
        proposal_id = self.save(self.proposal(), "approved")
        opener = CanaryOpener(self.row)
        opener.timeout = True
        self.assertEqual(self.run_canary(proposal_id, opener, self.authorize(proposal_id))["state"],
                         "needs_investigation")
        with self.assertRaisesRegex(WriteBlocked, "Verify by GET only"):
            self.run_canary(proposal_id, opener, self.authorize(proposal_id))
        save_snapshot([self.site], 2, [], self.path)
        with (
            patch("execution.DB_PATH", self.path), patch("storage.DB_PATH", self.path),
            patch.dict("os.environ", {"CLIPBOARD_API_TOKEN": "fixture"}, clear=True),
        ):
            self.assertEqual(reconcile_live_canary(
                CRMWriteClient(LiveCRMTransport(opener=opener)), proposal_id, self.path), "applied")
        self.assertEqual(sum(method == "PATCH" for method, *_ in opener.requests), 1)

    def test_expired_grant_and_live_transport_misuse_are_blocked(self):
        proposal_id = self.save(self.proposal(), "approved")
        opener = CanaryOpener(self.row)
        permit = self.authorize(proposal_id)
        permit.expires_at = 0
        with self.assertRaisesRegex(WriteBlocked, "expired"):
            self.run_canary(proposal_id, opener, permit)
        with self.assertRaisesRegex(WriteBlocked, "cannot be used as a mock"):
            CRMWriteClient(LiveCRMTransport(permit, opener=opener), mock=True)
        self.assertEqual(opener.requests, [])


    def test_adapter_switch_and_single_use_authorization(self):
        proposal_id = self.save(self.proposal(), "approved")
        opener = CanaryOpener(self.row)
        permit = self.authorize(proposal_id)
        transport = LiveCRMTransport(permit, opener=opener)
        route = f"{ACCOUNT_PATH}/{CANARY_ID}"
        with patch.dict("os.environ", {"CLIPBOARD_LIVE_WRITES": "1", "CLIPBOARD_API_TOKEN": "fixture"}, clear=True):
            with self.assertRaisesRegex(WriteBlocked, "disabled"):
                transport.request("PATCH", route, data={"name": CANARY_NEW_NAME}, operation_id=proposal_id)
        self.assertEqual(opener.requests, [])
        with (
            patch("crm_write.LIVE_CONTRACT_VERIFIED", True),
            patch.dict("os.environ", {"CLIPBOARD_LIVE_WRITES": "1", "CLIPBOARD_API_TOKEN": "fixture"}, clear=True),
        ):
            transport.request("PATCH", route, data={"name": CANARY_NEW_NAME}, operation_id=proposal_id)
            with self.assertRaisesRegex(WriteBlocked, "unused"):
                transport.request("PATCH", route, data={"name": CANARY_NEW_NAME}, operation_id=proposal_id)
        self.assertEqual(sum(method == "PATCH" for method, *_ in opener.requests), 1)


    def test_old_local_approval_and_new_snapshot_need_explicit_live_review(self):
        proposal_id = self.save(self.proposal(), "approved", live=False)
        opener = CanaryOpener(self.row)
        with self.assertRaisesRegex(WriteBlocked, "Fresh local live review"):
            self.run_canary(proposal_id, opener, self.authorize(proposal_id))
        self.save(self.proposal(), "approved")
        self.save(self.proposal())
        with self.assertRaisesRegex(WriteBlocked, "Fresh local live review"):
            self.run_canary(proposal_id, opener, self.authorize(proposal_id))
        self.assertEqual(opener.requests, [])

    def test_duplicate_execution_keeps_single_patch_and_unrelated_fields(self):
        proposal_id = self.save(self.proposal(), "approved")
        opener = CanaryOpener(self.row)
        for _ in range(2):
            self.assertEqual(self.run_canary(proposal_id, opener, self.authorize(proposal_id))["state"],
                             "applied")
        self.assertEqual(sum(method == "PATCH" for method, *_ in opener.requests), 1)
        self.assertEqual(opener.record, dict(self.row, name=CANARY_NEW_NAME, updated_at="new timestamp"))

    def test_failed_patch_with_unchanged_readback_stays_investigative(self):
        proposal_id = self.save(self.proposal(), "approved")
        opener = CanaryOpener(self.row)
        opener.ignore_patch = True
        opener.timeout = True
        self.assertEqual(self.run_canary(proposal_id, opener, self.authorize(proposal_id))["state"],
                         "needs_investigation")
        with (
            patch("execution.DB_PATH", self.path), patch("storage.DB_PATH", self.path),
            patch.dict("os.environ", {"CLIPBOARD_API_TOKEN": "fixture"}, clear=True),
        ):
            client = CRMWriteClient(LiveCRMTransport(opener=opener))
            self.assertEqual(reconcile_live_canary(client, proposal_id, self.path), "needs_investigation")
        self.assertEqual(sum(method == "PATCH" for method, *_ in opener.requests), 1)

    def test_readback_with_unrelated_drift_is_never_verified(self):
        class DriftingOpener(CanaryOpener):
            def open(self, request, *, timeout):
                response = super().open(request, timeout=timeout)
                if request.get_method() == "PATCH":
                    self.record["note"] = "unexpected change"
                return response

        proposal_id = self.save(self.proposal(), "approved")
        opener = DriftingOpener(self.row)
        self.assertEqual(self.run_canary(proposal_id, opener, self.authorize(proposal_id))["state"],
                         "needs_investigation")

    def test_streamlit_live_approval_then_exact_apply(self):
        from streamlit.testing.v1 import AppTest

        proposal_id = self.save(self.proposal())
        opener = CanaryOpener(self.row)
        make_transport = lambda permit=None: LiveCRMTransport(permit, opener=opener)
        with (
            patch("storage.DB_PATH", self.path), patch("execution.DB_PATH", self.path),
            patch("live_crm.LiveCRMTransport", side_effect=make_transport),
            patch.dict("os.environ", {
                "CLIPBOARD_LIVE_WRITES": "1", "CLIPBOARD_ASHLAND_CANARY": CANARY_ID,
                "CLIPBOARD_API_TOKEN": "fixture",
            }, clear=True),
        ):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            self.assertIn("LIVE REVIEW", app.info[0].value)
            next(button for button in app.button if button.label == "Approve locally for live review").click().run()
            self.assertEqual(opener.requests, [])
            app.selectbox[1].select("Approved").run()
            label = "Apply Ashland name change to CRM"
            self.assertTrue(next(button for button in app.button if button.label == label).disabled)
            app.checkbox[0].check().run()
            app.text_input[0].set_value("wrong phrase").run()
            self.assertTrue(next(button for button in app.button if button.label == label).disabled)
            self.assertEqual(opener.requests, [])
            app.text_input[0].set_value(f"APPLY {proposal_id} {CANARY_ID}").run()
            next(button for button in app.button if button.label == label).click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("Applied and verified" in element.value for element in app.markdown))
            self.assertEqual(sum(method == "PATCH" for method, *_ in opener.requests), 1)

    def test_qa_mode_cannot_record_or_show_live_approval(self):
        from streamlit.testing.v1 import AppTest

        proposal_id = self.save(self.proposal(), "approved", live=False)
        opener = CanaryOpener(self.row)
        with (
            patch("live_crm.LiveCRMTransport", side_effect=lambda permit=None: LiveCRMTransport(permit, opener=opener)),
            patch.dict("os.environ", {"CLIPBOARD_REVIEW_DB_PATH": str(self.path), "CLIPBOARD_LIVE_WRITES": "1",
                                     "CLIPBOARD_ASHLAND_CANARY": CANARY_ID}, clear=True),
        ):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            app.selectbox[1].select("Approved").run()
            self.assertEqual(app.warning[0].value, "TEST MODE — NO CRM WRITES")
            self.assertFalse(any("Apply" in button.label for button in app.button))
            snapshot = latest_snapshot()
            with self.assertRaisesRegex(ValueError, "assessment database"):
                save_review(snapshot["id"], proposal_id, "approved", live_review=True)
        self.assertEqual(opener.requests, [])
