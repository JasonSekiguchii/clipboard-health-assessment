"""Write boundary. The live API write contract is intentionally unconfigured."""

import os
import re

from reconcile import BELLHAVEN, FINDLAY_ID
from sources import CRM_FIELDS, CRM_FIELD_TYPES

LIVE_SWITCH = "CLIPBOARD_LIVE_WRITES"
LIVE_CONTRACT_VERIFIED = False
CANARY_ACK_SWITCH = "CLIPBOARD_ASHLAND_CANARY"
FINDLAY_ACK_SWITCH = "CLIPBOARD_FINDLAY_CANARY"
CANARY_ID = "001RJ0D7Z5MAZN5HX5"
ACCOUNT_PATH = "/api/v1/accounts"


class WriteBlocked(RuntimeError):
    pass


class WriteRejected(RuntimeError):
    """The transport has definitive evidence that no mutation was accepted."""


class WriteUncertain(RuntimeError):
    """The transport cannot establish the mutation outcome."""


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None


def validate_creation(data):
    """Validate the local review model, not an undocumented API request schema."""
    required = {"name", "parent_id", "billing_street", "billing_city", "billing_state", "billing_zip",
                "care_type", "status"}
    optional = {"note", "phone", "lifetime_revenue", "outstanding_ar",
                "duplicate_of_account", "chow_current_account"}
    if (
        not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional
        or any(not isinstance(data[key], str) or not data[key].strip() for key in required)
        or not valid_id(data["parent_id"]) or data["status"] not in {"Active", "Inactive"}
        or not re.fullmatch(r"[A-Z]{2}", data["billing_state"])
        or not re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", data["billing_zip"])
        or any(type(data[key]) is not CRM_FIELD_TYPES[key] for key in optional & data.keys())
        or any(data.get(key, 0) != 0 for key in ("lifetime_revenue", "outstanding_ar"))
        or any(data.get(key, "") for key in ("duplicate_of_account", "chow_current_account"))
    ):
        raise WriteBlocked("The replacement account is missing valid care or billing fields.")


def validate_patch(changes, account_id):
    from review_plans import PATCH_FIELDS

    if not valid_id(account_id) or not isinstance(changes, dict) or len(changes) != 1 or not changes.keys() <= PATCH_FIELDS:
        raise WriteBlocked("Invalid account ID or patch scope.")
    field, value = next(iter(changes.items()))
    if (
        not isinstance(value, str)
        or field == "name" and not value.strip()
        or field == "status" and value not in {"Active", "Inactive"}
        or field in {"parent_id", "duplicate_of_account", "chow_current_account"}
        and (not valid_id(value) or value == account_id)
    ):
        raise WriteBlocked("Invalid value for the approved patch field.")


def live_canary_armed(account_id=CANARY_ID):
    if os.environ.get(LIVE_SWITCH) != "1" or "CLIPBOARD_REVIEW_DB_PATH" in os.environ:
        return False
    if FINDLAY_ACK_SWITCH in os.environ:
        return account_id == FINDLAY_ID and os.environ[FINDLAY_ACK_SWITCH] == FINDLAY_ID
    return account_id == CANARY_ID and (
        LIVE_CONTRACT_VERIFIED or os.environ.get(CANARY_ACK_SWITCH) == CANARY_ID
    )


def allowed_live_patch(account_id, changes):
    return (
        (account_id == CANARY_ID and set(changes) == {"name"})
        or (account_id == FINDLAY_ID and changes == {"parent_id": BELLHAVEN})
    )


class CRMWriteClient:
    """Use an explicitly supplied adapter. Tests use only an in-memory adapter.

    The adapter returns normalized account records and mutation receipts. Its
    request encoding, correlation and server concurrency guarantees must be
    verified separately before enabling any live adapter.
    """

    def __init__(self, transport, *, mock=False):
        if mock and getattr(transport, "is_live", False):
            raise WriteBlocked("A live transport cannot be used as a mock.")
        self._transport = transport
        self._mock = mock

    @property
    def mode(self):
        return "mock" if self._mock else "live"

    @property
    def general_write_enabled(self):
        return getattr(self._transport, "general_write_enabled", False)

    def authorize(self, proposal_id, confirmation):
        if confirmation != proposal_id or not proposal_id:
            raise WriteBlocked("Explicit execution confirmation for this proposal is required.")
        if self.mode == "live":
            if os.environ.get(LIVE_SWITCH) != "1":
                raise WriteBlocked("Live CRM writes are disabled.")
            if "CLIPBOARD_REVIEW_DB_PATH" in os.environ:
                raise WriteBlocked("Live writes are prohibited in test mode.")
            if not (live_canary_armed() or live_canary_armed(FINDLAY_ID) or self.general_write_enabled):
                raise WriteBlocked("The live CRM write contract has not been verified or acknowledged for the canary.")

    def verify_live_plan(self, proposal_id, expected, changes, creation):
        if not getattr(self._transport, "is_live", False):
            raise WriteBlocked("A verified live adapter is required.")
        self._transport.verify_live_plan(proposal_id, expected, changes, creation)

    def read(self, account_id):
        if not valid_id(account_id):
            raise WriteBlocked("Invalid account ID.")
        account = self._transport.request("GET", f"{ACCOUNT_PATH}/{account_id}")
        if not isinstance(account, dict) or account.get("account_id") != account_id:
            raise WriteUncertain("Account read-back did not identify the expected record.")
        if any(type(account.get(field)) is not CRM_FIELD_TYPES[field]
               for field in CRM_FIELDS):
            raise WriteUncertain("Account read-back is incomplete.")
        return {field: account[field] for field in CRM_FIELDS}

    def use_created_account(self, account_id):
        restore = getattr(self._transport, "use_created_account", None)
        if restore:
            restore(account_id)

    def list_accounts(self):
        rows = self._transport.list_accounts()
        if not isinstance(rows, list) or len({row.get("account_id") for row in rows}) != len(rows):
            raise WriteUncertain("The absence check requires all unique CRM accounts.")
        if any(any(type(row.get(field)) is not CRM_FIELD_TYPES[field] for field in CRM_FIELDS) for row in rows):
            raise WriteUncertain("The absence check returned incomplete CRM records.")
        return rows

    def create(self, data, *, proposal_id, confirmation):
        self.authorize(proposal_id, confirmation)
        if self.mode == "live" and not getattr(self._transport, "allow_creation", False):
            raise WriteBlocked("Live account creation is disabled.")
        validate_creation(data)
        receipt = self._transport.request("POST", ACCOUNT_PATH, data=data, operation_id=proposal_id)
        account_id = receipt.get("account_id") if isinstance(receipt, dict) else None
        if not valid_id(account_id):
            raise WriteUncertain("Creation did not return a usable account ID.")
        return account_id

    def reconcile_creation(self, proposal_id):
        """Return an ID only with authoritative operation correlation from the adapter."""
        return self._transport.find_created_account(proposal_id)

    def patch(self, account_id, changes, *, proposal_id, confirmation):
        self.authorize(proposal_id, confirmation)
        validate_patch(changes, account_id)
        if self.mode == "live" and not (
            allowed_live_patch(account_id, changes) and live_canary_armed(account_id)
            or getattr(self._transport, "general_patch_allowed", lambda _: False)(changes)
        ):
            raise WriteBlocked("Live parent and CHOW changes outside the armed canary are disabled.")
        options = {"operation_id": proposal_id} if self.mode == "live" else {}
        self._transport.request("PATCH", f"{ACCOUNT_PATH}/{account_id}", data=changes, **options)
