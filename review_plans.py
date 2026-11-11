"""Explicit reviewer resolutions, separate from inferred matches."""

from copy import deepcopy

from reconcile import BELLHAVEN, business_id, parent_plan, proposal
from sources import CRM_FIELDS, CRM_FIELD_TYPES

PATCH_FIELDS = {"name", "parent_id", "status", "note", "duplicate_of_account", "chow_current_account"}
CHOW_NEW_ACCOUNT_CHECKS = {
    "lifetime_revenue": 0, "outstanding_ar": 0,
    "chow_current_account": "", "duplicate_of_account": "",
}


def chow_v2_plan(plan):
    result = deepcopy(plan)
    if result.get("strategy") != "preserve_create_and_link" or result.get("existing_account_id") != "001U6RW32TY0WSXZZB":
        raise ValueError("Version 2 is scoped to the Tiffin CHOW plan.")
    for key in CHOW_NEW_ACCOUNT_CHECKS:
        result["new_account"].pop(key)
    result.update(version=2, new_account_checks=deepcopy(CHOW_NEW_ACCOUNT_CHECKS))
    return result


def version_chow_proposal(source):
    """Create new review identity; never edit or inherit the original approval."""
    resolution_plan(source, True)
    item = deepcopy(source)
    item["proposed_change"] = chow_v2_plan(source["proposed_change"])
    reason = source["reason"] + (
        " Version 2 sends only the eight Batavia POST fields. Zero financial balances and blank links "
        "are mandatory GET verification conditions before linking; fresh approval is required."
    )
    item["reason"] = reason
    item["review_resolution"].update(source_id=source["id"], reason=reason)
    item["id"] = business_id(item)
    resolution_plan(item, True)
    return item


def validate_record(record):
    if any(type(record.get(key)) is not kind for key, kind in CRM_FIELD_TYPES.items()):
        raise ValueError("Refresh to obtain complete CRM evidence for the selected account.")


def resolve(source, accounts, *, reason, operation, target_id="", field="", value="",
            details=None, ownership_confirmed=False):
    """Produce a new pending plan. Resolving identity never approves execution."""
    from crm_write import validate_creation, validate_patch

    if not reason.strip():
        raise ValueError("Record the evidence and reason supporting this resolution.")
    by_id = {record["account_id"]: record for record in accounts}
    site = deepcopy(source["website"])
    related = []
    if operation == "create":
        if not site:
            raise ValueError("Creation requires a public facility record.")
        validate_creation(details)
        plan = {"strategy": "create_account", "new_account": deepcopy(details)}
        records = []
        parent = by_id.get(details["parent_id"])
        if not parent:
            raise ValueError("Select an existing reviewed parent account.")
        related.append(parent)
    elif operation == "update":
        target = by_id.get(target_id)
        if not target:
            raise ValueError("Select an account from the complete CRM snapshot.")
        validate_record(target)
        validate_patch({field: value}, target_id)
        if field in target and target[field] == value:
            raise ValueError("The proposed value already matches the reviewed account.")
        records = [deepcopy(target)]
        if field == "parent_id":
            if not ownership_confirmed:
                raise ValueError("Explicit parent and ownership confirmation is required.")
            if not site or value == target["parent_id"]:
                raise ValueError("A parent correction requires facility evidence and a different parent.")
            plan = parent_plan(target, value, site)
        else:
            plan = {"strategy": "reviewed_update", "account_id": target_id, "changes": {field: value}}
        if field in {"parent_id", "duplicate_of_account", "chow_current_account"}:
            reference = by_id.get(value)
            if not reference:
                raise ValueError("The related account must exist in the reviewed CRM snapshot.")
            validate_record(reference)
            related.append(reference)
    else:
        raise ValueError("Choose update or create.")
    for record in related:
        validate_record(record)
    item = proposal("Resolved correction", site, records, "Reviewer resolved", reason, plan)
    item["review_resolution"] = {
        "source_id": source["id"],
        "source_key": source.get("review_resolution", {}).get("source_key", business_id(source)),
        "reason": reason.strip(),
        "ownership_confirmed": ownership_confirmed, "related": related,
    }
    item["id"] = business_id(item)
    resolution_plan(item, ownership_confirmed)
    return item


def resolution_plan(item, ownership_confirmed):
    """Validate the explicit plan again at approval and execution."""
    from crm_write import validate_creation, validate_patch

    resolution = item.get("review_resolution", {})
    if not resolution.get("reason", "").strip() or not resolution.get("source_key"):
        raise ValueError("The resolution requires recorded supporting evidence.")
    for related in resolution.get("related", []):
        validate_record(related)
    plan = item["proposed_change"]
    creation = plan.get("new_account")
    if plan["strategy"] == "create_account":
        if item["crm"] or not item["website"]:
            raise ValueError("Invalid creation evidence.")
        validate_creation(creation)
        related = resolution.get("related", [])
        if (len(related) != 1 or related[0]["account_id"] != creation["parent_id"]
                or related[0]["parent_id"] or related[0]["status"] != "Active"):
            raise ValueError("Creation requires a reviewed active parent account.")
        return None, {}, creation
    if len(item["crm"]) != 1:
        raise ValueError("A correction must identify one reviewed target.")
    record = item["crm"][0]
    validate_record(record)
    if record["account_id"] == BELLHAVEN:
        raise ValueError("Facility corrections cannot target the Bellhaven parent itself.")
    if plan["strategy"] == "reviewed_update":
        if plan.get("account_id") != record["account_id"]:
            raise ValueError("The displayed account ID differs from the reviewed target.")
        changes = plan["changes"]
        validate_patch(changes, record["account_id"])
        if "parent_id" in changes:
            raise ValueError("Parent changes must use the financial SOP.")
        references = changes.keys() & {"duplicate_of_account", "chow_current_account"}
        related = resolution.get("related", [])
        if references:
            field = next(iter(references))
            if len(related) != 1 or related[0]["account_id"] != changes[field]:
                raise ValueError("The reviewed relationship target differs from the proposed link.")
        return record, changes, None
    related = resolution.get("related", [])
    if len(related) != 1 or not ownership_confirmed or not resolution.get("ownership_confirmed"):
        raise ValueError("The reviewed parent change requires ownership confirmation.")
    expected = parent_plan(record, related[0]["account_id"], item["website"])
    if plan.get("version") == 2:
        expected = chow_v2_plan(expected)
    if plan != expected or expected["strategy"] == "needs_investigation":
        raise ValueError("The financial plan differs from the reviewed account.")
    if creation:
        validate_creation(creation)
    return record, plan["existing_account_changes"], creation
