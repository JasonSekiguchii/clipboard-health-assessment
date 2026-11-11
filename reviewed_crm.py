"""Reusable reviewed transport. New API field contracts default to unverified."""

from copy import deepcopy
from dataclasses import dataclass, field
import os
from threading import Lock
from time import monotonic

from crm_write import ACCOUNT_PATH, WriteBlocked, valid_id
from live_crm import LiveCRMTransport
from sources import CRM_FIELDS, read_accounts

# These are contract gates, not claims about writable API fields.
# The request shapes for these two fields were verified by the saved canaries.
VERIFIED_PATCH_FIELDS = frozenset({"name", "parent_id"})
CREATION_CONTRACT_VERIFIED = False
BATAVIA_PROPOSAL_ID = "6d66a439528adfc944f84502"
BATAVIA_PAYLOAD = {
    "name": "Bellhaven of Batavia", "parent_id": "0015QAPLGS3FVYEEEM",
    "billing_street": "2000 Hospital Dr", "billing_city": "Batavia",
    "billing_state": "OH", "billing_zip": "45103",
    "care_type": "Skilled Nursing", "status": "Active",
}
TIFFIN_PROPOSAL_ID = "581e4f8b957a0db667512d92"
TIFFIN_ID = "001U6RW32TY0WSXZZB"
TIFFIN_PAYLOAD = {
    "name": "Bellhaven of Tiffin", "parent_id": "0015QAPLGS3FVYEEEM",
    "billing_street": "45 St Lawrence Dr", "billing_city": "Tiffin",
    "billing_state": "OH", "billing_zip": "44883", "care_type": "Skilled Nursing",
    "status": "Active",
}


TIFFIN_CONTRACT_SWITCHES = ("CLIPBOARD_TIFFIN_CREATE_CONTRACT", "CLIPBOARD_TIFFIN_LINK_CONTRACT")


def tiffin_contracts_enabled():
    return all(os.environ.get(key) == TIFFIN_PROPOSAL_ID for key in TIFFIN_CONTRACT_SWITCHES)


def is_tiffin_plan(proposal_id, expected, changes, creation):
    baseline = dict(TIFFIN_PAYLOAD, account_id=TIFFIN_ID, parent_id="001FWSQ30SFW6S7604",
                    parent_name="Cedar Trail Communities (Parent Account)",
                    lifetime_revenue=84000, outstanding_ar=12400,
                    chow_current_account="", duplicate_of_account="")
    return (proposal_id == TIFFIN_PROPOSAL_ID and isinstance(expected, dict)
            and all(expected.get(k) == v for k, v in baseline.items())
            and changes == {"chow_current_account": "<new account ID>"} and creation == TIFFIN_PAYLOAD)


@dataclass
class ReviewedAuthorization:
    proposal_id: str
    expected: dict | None
    changes: dict
    creation: dict | None
    expires_at: float
    sent: set = field(default_factory=set)
    lock: Lock = field(default_factory=Lock, repr=False)

    @classmethod
    def confirm(cls, proposal_id, expected, changes, creation, *, accepted, typed):
        if accepted is not True or typed != "APPLY":
            raise WriteBlocked("Accept the exact plan and type APPLY.")
        return cls(proposal_id, deepcopy(expected), deepcopy(changes), deepcopy(creation), monotonic() + 300)

    def verify(self, proposal_id, expected, changes, creation):
        if monotonic() >= self.expires_at or (
            proposal_id, expected, changes, creation
        ) != (self.proposal_id, self.expected, self.changes, self.creation):
            raise WriteBlocked("The authorization expired or differs from the reviewed plan.")

    def consume(self, operation):
        with self.lock:
            if monotonic() >= self.expires_at or operation in self.sent:
                raise WriteBlocked("A fresh explicit authorization is required.")
            self.sent.add(operation)


class ReviewedCRMTransport(LiveCRMTransport):
    """JSON request preparation is exercised with mock responses only for new fields."""

    def __init__(self, authorization=None, *, opener=None):
        super().__init__(authorization, opener=opener)
        self.created_id = None
        self.creation_verified = False

    @property
    def general_write_enabled(self):
        return (
            os.environ.get("CLIPBOARD_REVIEWED_WRITES") == "1"
            and "CLIPBOARD_REVIEW_DB_PATH" not in os.environ
            and bool(VERIFIED_PATCH_FIELDS or CREATION_CONTRACT_VERIFIED)
        )

    @property
    def allow_creation(self):
        permit = self.authorization
        return self.general_write_enabled and bool(
            permit and self.creation_allowed(permit.proposal_id, permit.expected, permit.changes, permit.creation)
        )

    def creation_allowed(self, proposal_id, expected, changes, creation):
        # This exception authorizes a first-use contract test, not a verified POST contract.
        if any(key in os.environ for key in TIFFIN_CONTRACT_SWITCHES):
            return tiffin_contracts_enabled() and is_tiffin_plan(proposal_id, expected, changes, creation)
        return bool(CREATION_CONTRACT_VERIFIED or (
            os.environ.get("CLIPBOARD_BATAVIA_CREATION") == BATAVIA_PROPOSAL_ID
            and proposal_id == BATAVIA_PROPOSAL_ID and expected is None and changes == {}
            and creation == BATAVIA_PAYLOAD
        ))

    def general_patch_allowed(self, changes):
        permit = self.authorization
        tiffin_link = bool(permit and tiffin_contracts_enabled()
                           and is_tiffin_plan(permit.proposal_id, permit.expected, permit.changes, permit.creation)
                           and self.creation_verified and valid_id(self.created_id)
                           and self.created_id not in (TIFFIN_ID, TIFFIN_PAYLOAD["parent_id"])
                           and changes == {"chow_current_account": self.created_id})
        if any(key in os.environ for key in TIFFIN_CONTRACT_SWITCHES):
            return self.general_write_enabled and tiffin_link
        return self.general_write_enabled and changes.keys() <= VERIFIED_PATCH_FIELDS

    def supports_plan(self, expected, changes, creation, proposal_id=None):
        if creation:
            return (self.general_write_enabled and self.creation_allowed(proposal_id, expected, changes, creation)
                    and (expected is None or "chow_current_account" in VERIFIED_PATCH_FIELDS
                         or tiffin_contracts_enabled() and is_tiffin_plan(proposal_id, expected, changes, creation)))
        return bool(changes) and self.general_patch_allowed(changes)

    def verify_live_plan(self, proposal_id, expected, changes, creation):
        if not self.authorization:
            raise WriteBlocked("A fresh reviewed authorization is required.")
        self.authorization.verify(proposal_id, expected, changes, creation)
        if not self.supports_plan(expected, changes, creation, proposal_id):
            raise WriteBlocked("The complete operation requires verified API contracts before execution.")

    def use_created_account(self, account_id):
        if not self.authorization or not self.authorization.creation:
            raise WriteBlocked("This authorization has no account creation.")
        self.created_id = account_id
        self.creation_verified = True

    def list_accounts(self):
        return read_accounts()

    def request(self, method, path, *, data=None, operation_id=None):
        if method == "GET" and data is None:
            return super().request(method, path)
        permit = self.authorization
        if not self.general_write_enabled or not permit:
            raise WriteBlocked("The general CRM write contract is unverified or disabled.")
        if method == "POST":
            if not self.allow_creation or path != ACCOUNT_PATH or data != permit.creation or operation_id != permit.proposal_id:
                raise WriteBlocked("Creation differs from the authorized plan or contract.")
            permit.consume("POST")
            result = self._send("POST", path, data)
            if isinstance(result, dict):
                self.created_id = result.get("account_id")
            return result
        if method != "PATCH" or not self.general_patch_allowed(data):
            raise WriteBlocked("The requested PATCH field contract is unverified.")
        expected = permit.expected
        if not expected or path != f"{ACCOUNT_PATH}/{expected['account_id']}" or operation_id != permit.proposal_id:
            raise WriteBlocked("The PATCH target differs from the approved account.")
        changes = {"chow_current_account": self.created_id} if permit.creation else permit.changes
        if data != changes:
            raise WriteBlocked("The PATCH differs from the authorized change.")
        current = self._send("GET", path)
        if not isinstance(current, dict) or any(current.get(key) != expected[key] for key in CRM_FIELDS):
            raise WriteBlocked("The account changed before dispatch.")
        permit.consume("PATCH")
        return self._send("PATCH", path, data)

    def find_created_account(self, proposal_id):
        # No published authoritative correlation API. Never guess by address.
        return None
