from contextlib import closing
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from crm_write import ACCOUNT_PATH, CRMWriteClient, WriteBlocked
from execution import apply_approved, approve_and_apply_findlay, reconcile_live_canary
from live_crm import CANARY_ID, LiveAuthorization, LiveCRMTransport
from reconcile import BELLHAVEN, BELLHAVEN_PARENT_NAME, FINDLAY_ID, FINDLAY_NAME, build_proposals
from sources import BASE_URL, FINDLAY
from storage import connect, latest_snapshot, save_review, save_snapshot
from test_execution import complete
from test_review import account, facility


class ParentOpener:
    def __init__(self, *accounts):
        self.rows = {row["account_id"]: deepcopy(row) for row in accounts}
        self.requests = []
        self.failure = None

    def open(self, request, *, timeout):
        method = request.get_method()
        target = request.full_url.rsplit("/", 1)[-1]
        data = json.loads(request.data) if request.data else None
        self.requests.append((method, target, data))
        if method == "PATCH":
            assert target == FINDLAY_ID and data == {"parent_id": BELLHAVEN}
            if self.failure == "422":
                raise HTTPError(request.full_url, 422, "fixture rejection", {}, None)
            self.rows[target].update(data, parent_name=BELLHAVEN_PARENT_NAME, updated_at="new")
            if self.failure == "timeout":
                raise TimeoutError
            if self.failure == "unrelated drift":
                self.rows[target]["outstanding_ar"] = 9
        response = BytesIO(json.dumps(self.rows[target]).encode())
        response.status = 200
        return response

    def mutations(self):
        return [request for request in self.requests if request[0] != "GET"]


class FindlayLiveTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "assessment.sqlite3"
        self.site = dict(facility(FINDLAY_NAME, "1800 N Blanchard St", "45840"),
                         city="Findlay", source_url=BASE_URL + FINDLAY,
                         care_offerings=["Assisted Living", "Memory Support"])
        self.parent = complete(dict(account(BELLHAVEN, BELLHAVEN_PARENT_NAME, parent=""), status="Active"))
        self.old = complete(dict(account(FINDLAY_ID, FINDLAY_NAME, self.site["street"], city="Findlay",
                                         zip_code="45840", parent="", revenue=22000),
                                 status="Active", care_type="Assisted Living", updated_at="old"))
        self.opener = ParentOpener(self.parent, self.old)
        for context in (
            patch("storage.DB_PATH", self.path), patch("execution.DB_PATH", self.path),
            patch.dict("os.environ", {"CLIPBOARD_LIVE_WRITES": "1", "CLIPBOARD_FINDLAY_CANARY": FINDLAY_ID,
                                     "CLIPBOARD_API_TOKEN": "fixture"}, clear=True),
        ):
            context.start()
            self.addCleanup(context.stop)

    def save(self, decision=None, *, fresh=True):
        rows = build_proposals([self.site], [self.parent, self.old])
        snapshot = save_snapshot([self.site], 2, rows)
        proposal_id = rows[0]["id"]
        if decision:
            save_review(snapshot, proposal_id, decision, ownership_confirmed=True,
                        live_review=fresh and decision == "approved")
        return snapshot, proposal_id

    def phrase(self, proposal_id):
        return f"APPLY {proposal_id} {FINDLAY_ID}"

    def client(self, permit=None):
        return CRMWriteClient(LiveCRMTransport(permit, opener=self.opener))

    def apply(self, snapshot, proposal_id, *, permit=True):
        authorization = LiveAuthorization.confirm(proposal_id, self.old, self.phrase(proposal_id)) if permit else None
        return apply_approved(self.client(authorization), proposal_id, confirmation=proposal_id,
                              required_snapshot_id=snapshot)

    def test_success_and_duplicate_click_preserve_unrelated_fields(self):
        snapshot, proposal_id = self.save("approved")
        for _ in range(2):
            self.assertEqual(self.apply(snapshot, proposal_id)["state"], "applied")
        self.assertEqual(self.opener.mutations(), [("PATCH", FINDLAY_ID, {"parent_id": BELLHAVEN})])
        self.assertEqual(self.opener.rows[FINDLAY_ID], dict(
            self.old, parent_id=BELLHAVEN, parent_name=BELLHAVEN_PARENT_NAME, updated_at="new"
        ))
        self.assertEqual(self.opener.requests[-1], ("GET", FINDLAY_ID, None))

    def test_rejected_and_missing_explicit_authorization_never_dispatch(self):
        snapshot, proposal_id = self.save("rejected")
        with self.assertRaisesRegex(WriteBlocked, "current local approval"):
            self.apply(snapshot, proposal_id)
        self.assertEqual(self.opener.requests, [])

    def test_local_approval_alone_and_old_snapshot_cannot_dispatch(self):
        snapshot, proposal_id = self.save("approved")
        with self.assertRaisesRegex(WriteBlocked, "fresh live reviewer"):
            self.apply(snapshot, proposal_id, permit=False)
        newer, _ = self.save()
        with self.assertRaisesRegex(WriteBlocked, "no longer current"):
            self.apply(snapshot, proposal_id)
        with self.assertRaisesRegex(WriteBlocked, "Fresh local"):
            self.apply(newer, proposal_id)
        self.assertEqual(self.opener.requests, [])

    def test_stale_financials_or_parent_abort_before_dispatch(self):
        snapshot, proposal_id = self.save("approved")
        self.opener.rows[FINDLAY_ID]["outstanding_ar"] = 1
        self.assertEqual(self.apply(snapshot, proposal_id)["state"], "needs_investigation")
        self.opener.rows[FINDLAY_ID]["outstanding_ar"] = 0
        self.opener.rows[BELLHAVEN]["status"] = "Inactive"
        self.assertEqual(self.apply(snapshot, proposal_id)["state"], "needs_investigation")
        self.assertEqual(self.opener.mutations(), [])

    def test_server_rejection_is_investigated_by_get_without_resend(self):
        snapshot, proposal_id = self.save("approved")
        self.opener.failure = "422"
        self.assertEqual(self.apply(snapshot, proposal_id)["state"], "needs_investigation")
        self.assertEqual(reconcile_live_canary(self.client(), proposal_id), "needs_investigation")
        with self.assertRaisesRegex(WriteBlocked, "Verify by GET only"):
            self.apply(snapshot, proposal_id)
        self.assertEqual(len(self.opener.mutations()), 1)
        self.assertEqual(self.opener.rows[FINDLAY_ID], self.old)

    def test_timeout_reconciles_original_baseline_after_proposal_disappears(self):
        snapshot, proposal_id = self.save("approved")
        self.opener.failure = "timeout"
        self.assertEqual(self.apply(snapshot, proposal_id)["state"], "needs_investigation")
        save_snapshot([self.site], 2, [])
        self.assertEqual(reconcile_live_canary(self.client(), proposal_id), "applied")
        self.assertEqual(len(self.opener.mutations()), 1)
        self.assertEqual(latest_snapshot()["findlay_execution"]["state"], "applied")

    def test_readback_drift_is_not_verified(self):
        snapshot, proposal_id = self.save("approved")
        self.opener.failure = "unrelated drift"
        self.assertEqual(self.apply(snapshot, proposal_id)["state"], "needs_investigation")
        self.assertEqual(reconcile_live_canary(self.client(), proposal_id), "needs_investigation")
        self.assertEqual(len(self.opener.mutations()), 1)

    def test_live_scope_blocks_wrong_parent_and_ashland_while_findlay_is_armed(self):
        _, proposal_id = self.save("approved")
        permit = LiveAuthorization.confirm(proposal_id, self.old, self.phrase(proposal_id))
        transport = LiveCRMTransport(permit, opener=self.opener)
        with self.assertRaises(WriteBlocked):
            transport.request("PATCH", f"{ACCOUNT_PATH}/{FINDLAY_ID}", data={"parent_id": "other"},
                              operation_id=proposal_id)
        with self.assertRaisesRegex(WriteBlocked, "disabled"):
            transport.request("PATCH", f"{ACCOUNT_PATH}/{CANARY_ID}", data={"name": "other"},
                              operation_id=proposal_id)
        self.assertEqual(self.opener.requests, [])

    def test_streamlit_dialog_refresh_and_exact_apply_with_fixture_transport(self):
        from streamlit.testing.v1 import AppTest

        self.save()
        with (
            patch("execution.LiveCRMTransport", side_effect=lambda permit=None: LiveCRMTransport(permit, opener=self.opener)),
            patch("sources.scrape_facilities", return_value=[self.site]),
            patch("sources.read_accounts", return_value=[self.parent, self.old]),
        ):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            next(button for button in app.button if button.label == "Refresh from website and CRM").click().run()
            next(button for button in app.button if button.label == "Approve & Apply").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(self.opener.requests, [])
            label = "Confirm & Apply"
            self.assertTrue(next(button for button in app.button if button.label == label).disabled)
            app.checkbox[0].check().run()
            app.text_input[0].set_value("wrong confirmation").run()
            self.assertTrue(next(button for button in app.button if button.label == label).disabled)
            self.assertEqual(self.opener.requests, [])
            app.text_input[0].set_value("APPLY").run()
            next(button for button in app.button if button.label == label).click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("Findlay execution · Applied and verified" in entry.value for entry in app.markdown))
            self.assertEqual(len(self.opener.mutations()), 1)

    def test_separate_unsupported_proposal_can_be_rejected_without_crm(self):
        from streamlit.testing.v1 import AppTest

        competing = dict(self.old, account_id="competing", parent_id=BELLHAVEN)
        rows = build_proposals([self.site], [self.parent, self.old, competing])
        self.assertEqual(rows[0]["category"], "Ambiguous address overlap")
        save_snapshot([self.site], 3, rows)
        with patch("live_crm.LiveCRMTransport", side_effect=AssertionError("Unexpected transport")):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            self.assertTrue(next(button for button in app.button if button.label == "Approve").disabled)
            next(button for button in app.button if button.label == "Reject").click().run()
        self.assertEqual(latest_snapshot()["proposals"][0]["review_state"], "rejected")
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)


    def test_dialog_cancel_and_reopen_requires_new_acceptance(self):
        from streamlit.testing.v1 import AppTest

        self.save()
        with patch("execution.LiveCRMTransport", side_effect=AssertionError("Unexpected transport")):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            next(button for button in app.button if button.label == "Approve & Apply").click().run()
            app.checkbox[0].check().run()
            app.text_input[0].set_value("APPLY").run()
            next(button for button in app.button if button.label == "Cancel").click().run()
            self.assertFalse(app.exception)
            next(button for button in app.button if button.label == "Approve & Apply").click().run()
            self.assertFalse(app.checkbox[0].value)
            self.assertEqual(app.text_input[0].value, "")
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)

    def test_existing_approval_is_preserved_and_duplicate_final_confirmation_is_safe(self):
        snapshot, proposal_id = self.save("approved")
        with closing(connect(self.path)) as db:
            before = db.execute("SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        with patch("execution.LiveCRMTransport", side_effect=lambda permit=None: LiveCRMTransport(permit, opener=self.opener)):
            for _ in range(2):
                self.assertEqual(approve_and_apply_findlay(
                    snapshot, proposal_id, self.old, accepted=True, typed="APPLY"
                )["state"], "applied")
        with closing(connect(self.path)) as db:
            after = db.execute("SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        self.assertEqual(before, after)
        self.assertEqual(len(self.opener.mutations()), 1)
        self.assertEqual(latest_snapshot()["findlay_execution"]["state"], "applied")

    def test_final_confirmation_guards_and_rejected_proposal(self):
        snapshot, proposal_id = self.save()
        with patch("execution.LiveCRMTransport", side_effect=AssertionError("Unexpected transport")):
            for accepted, typed in ((False, "APPLY"), (True, ""), (True, "apply")):
                with self.assertRaisesRegex(WriteBlocked, "type APPLY"):
                    approve_and_apply_findlay(snapshot, proposal_id, self.old, accepted=accepted, typed=typed)
            save_review(snapshot, proposal_id, "rejected")
            with self.assertRaisesRegex(WriteBlocked, "unavailable or rejected"):
                approve_and_apply_findlay(snapshot, proposal_id, self.old, accepted=True, typed="APPLY")
        self.assertEqual(self.opener.requests, [])

    def test_final_confirmation_stale_evidence_stops_dispatch(self):
        snapshot, proposal_id = self.save()
        self.opener.rows[FINDLAY_ID]["outstanding_ar"] = 7
        with patch("execution.LiveCRMTransport", side_effect=lambda permit=None: LiveCRMTransport(permit, opener=self.opener)):
            self.assertEqual(approve_and_apply_findlay(
                snapshot, proposal_id, self.old, accepted=True, typed="APPLY"
            )["state"], "needs_investigation")
        self.assertEqual(self.opener.mutations(), [])
        self.assertEqual(latest_snapshot()["proposals"][0]["review_state"], "approved")

    def test_partial_failure_final_confirmation_reconciles_and_never_retries(self):
        snapshot, proposal_id = self.save()
        self.opener.failure = "422"
        with patch("execution.LiveCRMTransport", side_effect=lambda permit=None: LiveCRMTransport(permit, opener=self.opener)):
            self.assertEqual(approve_and_apply_findlay(
                snapshot, proposal_id, self.old, accepted=True, typed="APPLY"
            )["state"], "needs_investigation")
            with self.assertRaisesRegex(WriteBlocked, "execution already exists"):
                approve_and_apply_findlay(snapshot, proposal_id, self.old, accepted=True, typed="APPLY")
        self.assertEqual(len(self.opener.mutations()), 1)

    def test_approval_without_final_confirmation_and_disabled_mode_do_not_dispatch(self):
        snapshot, proposal_id = self.save("approved")
        self.assertEqual(self.opener.requests, [])
        with patch.dict("os.environ", {"CLIPBOARD_LIVE_WRITES": "0"}):
            with self.assertRaisesRegex(WriteBlocked, "disabled"):
                approve_and_apply_findlay(snapshot, proposal_id, self.old, accepted=True, typed="APPLY")
        self.assertEqual(self.opener.requests, [])


    def test_existing_approval_dialog_cancel_preserves_decision(self):
        from streamlit.testing.v1 import AppTest

        _, proposal_id = self.save("approved")
        with closing(connect(self.path)) as db:
            before = db.execute("SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        with patch("execution.LiveCRMTransport", side_effect=AssertionError("Unexpected transport")):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            app.selectbox[1].select("Approved").run()
            next(button for button in app.button if button.label == "Approve & Apply").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.checkbox[0].value)
            next(button for button in app.button if button.label == "Cancel").click().run()
        with closing(connect(self.path)) as db:
            after = db.execute("SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)).fetchone()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)
        self.assertEqual(before, after)

    def test_refresh_closes_accepted_dialog_and_never_executes(self):
        from streamlit.testing.v1 import AppTest

        self.save()
        with (
            patch("execution.LiveCRMTransport", side_effect=AssertionError("Unexpected transport")),
            patch("sources.scrape_facilities", return_value=[self.site]),
            patch("sources.read_accounts", return_value=[self.parent, self.old]),
        ):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run()
            next(button for button in app.button if button.label == "Approve & Apply").click().run()
            app.checkbox[0].check().run()
            app.text_input[0].set_value("APPLY").run()
            next(button for button in app.button if button.label == "Refresh from website and CRM").click().run()
            self.assertFalse(any(button.label == "Confirm & Apply" for button in app.button))
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 0)
