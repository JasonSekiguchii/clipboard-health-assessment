"""Narrow HTTPS adapter for the Ashland and Findlay corrections."""

from dataclasses import dataclass, field
import json
import os
from threading import Lock
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from crm_write import ACCOUNT_PATH, CANARY_ID, WriteBlocked, WriteUncertain, live_canary_armed, valid_id
from reconcile import BELLHAVEN, BELLHAVEN_PARENT_NAME, FINDLAY_ID, FINDLAY_NAME, address
from sources import BASE_URL, CRM_FIELDS, CRM_FIELD_TYPES, NoRedirect

CANARY_OLD_NAME = "Bellhaven Health Care Center of Ashland"
CANARY_NEW_NAME = "Bellhaven Healthcare Centre of Ashland"


def is_ashland_canary(item):
    records = item.get("crm", [])
    plan = item.get("proposed_change", {})
    return (
        item.get("category") == "Name correction" and item.get("confidence") == "Strong site match"
        and len(records) == 1 and plan.get("account_id") == CANARY_ID
        and plan.get("candidate_name") == CANARY_NEW_NAME
        and records[0].get("account_id") == CANARY_ID and records[0].get("name") == CANARY_OLD_NAME
        and all(type(records[0].get(key)) is CRM_FIELD_TYPES[key] for key in CRM_FIELDS)
        and records[0]["parent_id"] == BELLHAVEN and records[0]["status"] == "Active"
        and records[0]["lifetime_revenue"] == records[0]["outstanding_ar"] == 0
        and not records[0]["duplicate_of_account"] and not records[0]["chow_current_account"]
        and address(records[0], "billing_") == ("1631 claremont ave", "ashland", "OH", "44805")
    )


@dataclass
class LiveAuthorization:
    """One explicit, short-lived authorization, held only in process memory."""

    proposal_id: str
    reviewed: dict
    expires_at: float
    used: bool = field(default=False, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @classmethod
    def confirm(cls, proposal_id, reviewed, phrase):
        target = reviewed.get("account_id")
        if not valid_id(proposal_id) or phrase != f"APPLY {proposal_id} {target}":
            raise WriteBlocked("Type the exact proposal and account authorization.")
        if target not in (CANARY_ID, FINDLAY_ID):
            raise WriteBlocked("The reviewed account is outside the supported canaries.")
        if any(type(reviewed.get(key)) is not CRM_FIELD_TYPES[key] for key in CRM_FIELDS):
            raise WriteBlocked("The canary authorization requires the complete reviewed account.")
        if reviewed["status"] != "Active" or reviewed["duplicate_of_account"] or reviewed["chow_current_account"]:
            raise WriteBlocked("The account no longer meets the reviewed low-risk conditions.")
        if target == CANARY_ID and (
            reviewed["name"] != CANARY_OLD_NAME or reviewed["parent_id"] != BELLHAVEN
            or reviewed["lifetime_revenue"] != 0 or reviewed["outstanding_ar"] != 0
            or address(reviewed, "billing_") != ("1631 claremont ave", "ashland", "OH", "44805")
        ):
            raise WriteBlocked("The Ashland canary no longer meets the reviewed low-risk conditions.")
        if target == FINDLAY_ID and (
            reviewed["name"] != FINDLAY_NAME or reviewed["parent_id"] or reviewed["parent_name"]
            or reviewed["lifetime_revenue"] != 22000 or reviewed["outstanding_ar"] != 0
            or address(reviewed, "billing_") != ("1800 n blanchard st", "findlay", "OH", "45840")
        ):
            raise WriteBlocked("The Findlay canary no longer meets the reviewed low-risk conditions.")
        return cls(proposal_id, {key: reviewed[key] for key in CRM_FIELDS}, monotonic() + 300)

    def verify(self, proposal_id, expected, changes, creation=None):
        if monotonic() >= self.expires_at:
            raise WriteBlocked("The live authorization expired.")
        if (proposal_id != self.proposal_id or expected != self.reviewed or creation is not None
                or changes != ({"name": CANARY_NEW_NAME} if self.reviewed["account_id"] == CANARY_ID
                               else {"parent_id": BELLHAVEN})):
            raise WriteBlocked("The authorization does not match the exact reviewed canary change.")


    def consume(self, proposal_id, changes):
        with self._lock:
            self.verify(proposal_id, self.reviewed, changes)
            if self.used:
                raise WriteBlocked("A fresh, unused live reviewer authorization is required.")
            self.used = True


class LiveCRMTransport:
    """Use authenticated GET and one precisely scoped PATCH after authorization."""

    is_live = True

    def __init__(self, authorization=None, *, opener=None):
        self.authorization = authorization
        self._opener = opener if opener is not None else build_opener(NoRedirect)

    def verify_live_plan(self, proposal_id, expected, changes, creation):
        if self.authorization is None:
            raise WriteBlocked("A fresh live reviewer authorization is required.")
        self.authorization.verify(proposal_id, expected, changes, creation)

    def request(self, method, path, *, data=None, operation_id=None):
        detail = path.removeprefix(f"{ACCOUNT_PATH}/")
        if path != f"{ACCOUNT_PATH}/{detail}" or not valid_id(detail):
            raise WriteBlocked("Live adapter accepts account detail routes only.")
        if method == "GET" and data is None:
            return self._send(method, path)
        if method != "PATCH" or detail not in (CANARY_ID, FINDLAY_ID):
            raise WriteBlocked("Live adapter permits only the scoped Ashland or Findlay PATCH.")
        if not live_canary_armed(detail):
            raise WriteBlocked("The live canary is disabled.")
        permit = self.authorization
        if permit is None or permit.used or permit.reviewed["account_id"] != detail:
            raise WriteBlocked("A fresh, unused live reviewer authorization is required.")
        permit.verify(operation_id, permit.reviewed, data)
        if detail == FINDLAY_ID:
            parent = self._send("GET", f"{ACCOUNT_PATH}/{BELLHAVEN}")
            if not isinstance(parent, dict) or (
                parent.get("account_id"), parent.get("name"), parent.get("parent_id"), parent.get("status")
            ) != (BELLHAVEN, BELLHAVEN_PARENT_NAME, "", "Active"):
                raise WriteBlocked("The target Bellhaven parent changed before dispatch.")
        current = self._send("GET", path)
        if not isinstance(current, dict) or any(
            type(current.get(key)) is not CRM_FIELD_TYPES[key] or current[key] != permit.reviewed[key]
            for key in CRM_FIELDS
        ):
            raise WriteBlocked("The canary changed since authorization.")
        permit.consume(operation_id, data)
        self._send("PATCH", path, data)

    def find_created_account(self, proposal_id):
        # No verified live creation or authoritative operation lookup.
        return None

    def _send(self, method, path, data=None):
        token = os.environ.get("CLIPBOARD_API_TOKEN", "").strip()
        if not token or any(char.isspace() for char in token):
            raise WriteBlocked("A valid CRM token is required.")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        payload = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(data).encode()
        try:
            with self._opener.open(Request(BASE_URL + path, data=payload, headers=headers, method=method),
                                   timeout=20) as response:
                if response.status != (201 if method == "POST" else 200):
                    raise WriteUncertain(f"{method} returned an undocumented status.")
                if method in {"GET", "POST"}:
                    return json.load(response)
        except HTTPError as error:
            status = error.code
            error.close()
            raise WriteUncertain(f"{method} returned HTTP {status}. Verify by GET.") from None
        except (URLError, OSError, ValueError):
            raise WriteUncertain(f"{method} outcome could not be established.") from None
