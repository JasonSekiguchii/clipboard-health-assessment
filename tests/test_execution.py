from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crm_write import ACCOUNT_PATH, CRMWriteClient, WriteBlocked, WriteRejected
from execution import apply_approved
from reconcile import BELLHAVEN, build_proposals
from sources import CRM_FIELD_TYPES
from storage import latest_snapshot, save_review, save_snapshot
from test_review import account, facility


def complete(record):
    return {field: record.get(field, kind()) for field, kind in CRM_FIELD_TYPES.items()}


class FakeTransport:
    def __init__(self, *records):
        self.rows = {row["account_id"]: deepcopy(row) for row in records}
        self.requests = []
        self.receipts = {}
        self.reject = set()
        self.timeout_after = set()
        self.timeout_before = set()
        self.find_receipts = True
        self.ignore_patch = False
        self.interrupt_post = False

    def request(self, method, path, *, data=None, operation_id=None):
        self.requests.append((method, path, deepcopy(data)))
        if method in self.reject:
            raise WriteRejected()
        if method in self.timeout_before:
            raise TimeoutError()
        if method == "POST":
            account_id = "fake-created"
            assert account_id not in self.rows, "Duplicate POST attempted"
            self.rows[account_id] = complete(dict(data, account_id=account_id))
            self.receipts[operation_id] = account_id
            if self.interrupt_post:
                raise KeyboardInterrupt()
            result = {"account_id": account_id}
        elif method == "PATCH":
            row = self.rows[path.rsplit("/", 1)[-1]]
            if not self.ignore_patch:
                row.update(data)
                row["updated_at"] = "fake-updated-after-patch"
                if "parent_id" in data:
                    row["parent_name"] = self.rows[data["parent_id"]]["name"]
            result = {}
        else:
            assert method == "GET"
            result = self.rows[path.rsplit("/", 1)[-1]]
        if method in self.timeout_after:
            raise TimeoutError()
        return deepcopy(result)

    def find_created_account(self, operation_id):
        return self.receipts.get(operation_id) if self.find_receipts else None

    def list_accounts(self):
        return deepcopy(list(self.rows.values()))

    def mutations(self):
        return [request for request in self.requests if request[0] != "GET"]


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "execution.sqlite3"
        self.site = facility()
        self.parent = complete(dict(account(BELLHAVEN, "Bellhaven Senior Living", parent=""), status="Active"))
        self.old = complete(dict(account("fake-old", self.site["name"], "10 W Main St",
                                         parent="fake-previous", revenue=84, ar=12),
                                 parent_name="Previous operator", care_type="Memory Care", status="Active"))

    def collect(self, old, category="Possible wrong parent", decision="approved"):
        rows = build_proposals([self.site], [self.parent, old])
        item = next(row for row in rows if row["category"] == category)
        snapshot = save_snapshot([self.site], 2, rows, self.path)
        if decision:
            save_review(snapshot, item["id"], decision, ownership_confirmed=True, path=self.path)
        return item["id"]

    def execute(self, transport, proposal_id):
        return apply_approved(CRMWriteClient(transport, mock=True), proposal_id,
                              confirmation=proposal_id, path=self.path)

    def test_live_switch_and_confirmation_block_before_transport(self):
        target = self.collect(self.old)
        transport = FakeTransport(self.parent, self.old)
        client = CRMWriteClient(transport)
        with patch.dict("os.environ", {"CLIPBOARD_LIVE_WRITES": ""}, clear=True):
            with self.assertRaisesRegex(WriteBlocked, "disabled"):
                apply_approved(client, target, confirmation=target, path=self.path)
        with patch.dict("os.environ", {"CLIPBOARD_LIVE_WRITES": "1"}, clear=True):
            with self.assertRaisesRegex(WriteBlocked, "not been verified"):
                apply_approved(client, target, confirmation=target, path=self.path)
        with self.assertRaisesRegex(WriteBlocked, "confirmation"):
            apply_approved(CRMWriteClient(transport, mock=True), target, confirmation="", path=self.path)
        self.assertEqual(transport.requests, [])

    def test_simple_name_patch_sends_only_approved_field_and_verifies(self):
        old = dict(self.old, name="Bellhaven at Town", parent_id=BELLHAVEN, parent_name=self.parent["name"])
        target = self.collect(old, "Name correction")
        transport = FakeTransport(self.parent, old)
        self.assertEqual(self.execute(transport, target)["state"], "applied")
        self.assertEqual(transport.requests, [
            ("GET", f"{ACCOUNT_PATH}/fake-old", None),
            ("PATCH", f"{ACCOUNT_PATH}/fake-old", {"name": self.site["name"]}),
            ("GET", f"{ACCOUNT_PATH}/fake-old", None),
        ])
        self.assertEqual(latest_snapshot(self.path)["proposals"][0]["review_state"], "approved")
        self.assertIsNone(latest_snapshot(self.path)["proposals"][0]["execution_state"])

    def test_rejected_or_pending_proposal_does_not_execute(self):
        transport = FakeTransport(self.parent, self.old)
        target = self.collect(self.old, decision=None)
        with self.assertRaisesRegex(WriteBlocked, "local approval"):
            self.execute(transport, target)
        self.collect(self.old, decision="rejected")
        with self.assertRaisesRegex(WriteBlocked, "local approval"):
            self.execute(transport, target)
        self.assertEqual(transport.requests, [])

    def test_current_financial_fields_must_match_reviewed_snapshot(self):
        target = self.collect(self.old)
        transport = FakeTransport(self.parent, dict(self.old, outstanding_ar=0))
        self.assertEqual(self.execute(transport, target)["state"], "needs_investigation")
        self.assertEqual(transport.mutations(), [])

    def test_replacement_requires_complete_fields(self):
        old = dict(self.old, care_type="")
        target = self.collect(old)
        transport = FakeTransport(self.parent, old)
        with self.assertRaisesRegex(WriteBlocked, "missing valid care"):
            self.execute(transport, target)
        self.assertEqual(transport.requests, [])

    def test_both_positive_create_and_link_preserve_old_account(self):
        target = self.collect(self.old)
        transport = FakeTransport(self.parent, self.old)
        outcome = self.execute(transport, target)
        self.assertEqual(outcome["state"], "applied")
        replacement = transport.rows[outcome["created_account_id"]]
        self.assertEqual(replacement["parent_id"], BELLHAVEN)
        self.assertEqual(replacement["billing_street"], self.site["street"])
        self.assertEqual(replacement["care_type"], "Memory Care")
        self.assertEqual(transport.rows["fake-old"], dict(self.old, chow_current_account="fake-created", updated_at="fake-updated-after-patch"))
        self.assertEqual([method for method, *_ in transport.mutations()], ["POST", "PATCH"])
        self.assertEqual(self.execute(transport, target)["state"], "applied")
        self.assertEqual(len(transport.mutations()), 2)

    def test_one_positive_financial_field_uses_direct_parent_patch(self):
        for revenue, ar in ((84, 0), (0, 12)):
            old = dict(self.old, lifetime_revenue=revenue, outstanding_ar=ar)
            target = self.collect(old)
            transport = FakeTransport(self.parent, old)
            self.assertEqual(self.execute(transport, target)["state"], "applied")
            self.assertEqual(transport.mutations(), [
                ("PATCH", f"{ACCOUNT_PATH}/fake-old", {"parent_id": BELLHAVEN})
            ])

    def test_definitively_rejected_post_records_failure_without_patch(self):
        target = self.collect(self.old)
        transport = FakeTransport(self.parent, self.old)
        transport.reject.add("POST")
        self.assertEqual(self.execute(transport, target)["state"], "failed")
        self.assertEqual([method for method, *_ in transport.mutations()], ["POST"])
        self.assertEqual(transport.rows["fake-old"], self.old)

    def test_link_failure_and_new_execution_client_reuse_durable_created_id(self):
        target = self.collect(self.old)
        transport = FakeTransport(self.parent, self.old)
        transport.reject.add("PATCH")
        self.assertEqual(self.execute(transport, target)["state"], "failed")
        rows = build_proposals([self.site], [self.parent, self.old])
        save_snapshot([self.site], 2, rows, self.path)
        transport.reject.clear()
        self.assertEqual(self.execute(transport, target)["state"], "applied")
        self.assertEqual([method for method, *_ in transport.mutations()], ["POST", "PATCH", "PATCH"])

    def test_post_timeout_reconciles_receipt_and_never_reposts(self):
        target = self.collect(self.old)
        transport = FakeTransport(self.parent, self.old)
        transport.timeout_after.add("POST")
        self.assertEqual(self.execute(transport, target)["state"], "needs_investigation")
        transport.find_receipts = False
        self.assertEqual(self.execute(transport, target)["state"], "needs_investigation")
        self.assertEqual([method for method, *_ in transport.mutations()], ["POST"])
        transport.find_receipts = True
        self.assertEqual(self.execute(transport, target)["state"], "applied")
        self.assertEqual([method for method, *_ in transport.mutations()], ["POST", "PATCH"])

    def test_unknown_post_blocks_new_proposal_for_same_target(self):
        target = self.collect(self.old)
        transport = FakeTransport(self.parent, self.old)
        transport.timeout_before.add("POST")
        self.assertEqual(self.execute(transport, target)["state"], "needs_investigation")
        changed = self.collect(dict(self.old, outstanding_ar=13))
        with self.assertRaisesRegex(WriteBlocked, "unresolved write"):
            self.execute(transport, changed)
        self.assertEqual([method for method, *_ in transport.mutations()], ["POST"])

    def test_patch_timeout_can_be_verified_after_refresh_hides_proposal(self):
        old = dict(self.old, outstanding_ar=0)
        target = self.collect(old)
        transport = FakeTransport(self.parent, old)
        transport.timeout_after.add("PATCH")
        self.assertEqual(self.execute(transport, target)["state"], "needs_investigation")
        save_snapshot([self.site], 2, [], self.path)
        self.assertEqual(self.execute(transport, target)["state"], "applied")
        self.assertEqual([method for method, *_ in transport.mutations()], ["PATCH"])

    def test_patch_success_without_matching_readback_never_reports_applied(self):
        old = dict(self.old, outstanding_ar=0)
        target = self.collect(old)
        transport = FakeTransport(self.parent, old)
        transport.ignore_patch = True
        self.assertEqual(self.execute(transport, target)["state"], "needs_investigation")
        self.assertEqual(self.execute(transport, target)["state"], "needs_investigation")
        self.assertEqual([method for method, *_ in transport.mutations()], ["PATCH"])


    def test_interruption_after_post_keeps_intent_durable(self):
        target = self.collect(self.old)
        transport = FakeTransport(self.parent, self.old)
        transport.interrupt_post = True
        with self.assertRaises(KeyboardInterrupt):
            self.execute(transport, target)
        # A new executor and SQLite connection recover the committed intent.
        self.assertEqual(self.execute(transport, target)["state"], "applied")
        self.assertEqual([method for method, *_ in transport.mutations()], ["POST", "PATCH"])

    def test_stale_note_or_timestamp_blocks_patch(self):
        old = dict(self.old, outstanding_ar=0)
        target = self.collect(old)
        for changes in ({"note": "new note"}, {"updated_at": "updated since review"}):
            transport = FakeTransport(self.parent, dict(old, **changes))
            self.assertEqual(self.execute(transport, target)["state"], "needs_investigation")
            self.assertEqual(transport.mutations(), [])


    def test_client_blocks_parent_only_creation_and_overbroad_patch(self):
        transport = FakeTransport(self.parent, self.old)
        client = CRMWriteClient(transport, mock=True)
        with self.assertRaises(WriteBlocked):
            client.create({"parent_id": BELLHAVEN}, proposal_id="fixture", confirmation="fixture")
        with self.assertRaises(WriteBlocked):
            client.patch("fake-old", {"name": "New", "parent_id": BELLHAVEN},
                         proposal_id="fixture", confirmation="fixture")
        self.assertEqual(transport.requests, [])
