"""Evidence and conditional proposals. This module has no CRM client."""

from collections import defaultdict
import hashlib
import json
import re

from sources import BASE_URL, FINDLAY

BELLHAVEN = "0015QAPLGS3FVYEEEM"
BELLHAVEN_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"
FINDLAY_ID = "001UKEFGADQ8YCZ4YM"
FINDLAY_NAME = "Bellhaven Meadows of Findlay"
ADDRESS_FIELDS = ("street", "city", "state", "zip")
CRM_BUSINESS_FIELDS = (
    "account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city",
    "billing_state", "billing_zip", "care_type", "status", "lifetime_revenue",
    "outstanding_ar", "duplicate_of_account", "chow_current_account", "note",
)
STREET_ALIASES = dict(zip(
    "street road drive avenue boulevard lane pike west east north south northwest northeast southwest southeast saint court parkway".split(),
    "st rd dr ave blvd ln pk w e n s nw ne sw se st ct pkwy".split(),
))
NAME_ALIASES = {
    "centre": "center", "rehab": "rehabilitation", "healthcare": "health care",
}


def words(value):
    return re.findall(r"[^\W_]+", value.casefold().replace("&", " and "))


def address(row, prefix=""):
    street, city, state, zip_code = (row[prefix + key] for key in ADDRESS_FIELDS)
    return (
        " ".join(STREET_ALIASES.get(word, word) for word in words(street)),
        " ".join(words(city)), state.strip().upper(), zip_code.strip(),
    )


def name_tokens(value):
    return set(words(" ".join(NAME_ALIASES.get(word, word) for word in words(value))))


def strong_name(left, right):
    ignored = {"the", "at", "of", "and"}
    left, right = name_tokens(left) - ignored, name_tokens(right) - ignored
    return bool(left and left == right)


def parent_plan(account, target_parent, facility=None):
    """Describe the financial SOP for a parent move, conditional on confirmation."""
    if account["lifetime_revenue"] > 0 and account["outstanding_ar"] > 0:
        if facility is None:
            raise ValueError("Website evidence is required to prepare a replacement account.")
        return {
            "strategy": "preserve_create_and_link",
            "requires_confirmation": True,
            "existing_account_id": account["account_id"],
            "new_account": {
                "name": facility["name"],
                "parent_id": target_parent,
                **{f"billing_{field}": facility[field] for field in ADDRESS_FIELDS},
                "care_type": account.get("care_type", ""),
                "status": "Active",
                "lifetime_revenue": 0,
                "outstanding_ar": 0,
                "chow_current_account": "",
                "duplicate_of_account": "",
            },
            "existing_account_changes": {"chow_current_account": "<new account ID>"},
        }
    if account["lifetime_revenue"] != 0 and account["outstanding_ar"] != 0:
        return {"strategy": "needs_investigation", "review": "Verify the nonzero financial balances."}
    return {
        "strategy": "reparent_existing",
        "requires_confirmation": True,
        "existing_account_id": account["account_id"],
        "existing_account_changes": {"parent_id": target_parent},
    }


def business_id(item):
    """Hash reviewed facts and requested changes independently of collection metadata."""
    facility = item["website"]
    site_fields = ("name", *ADDRESS_FIELDS, "care_offerings", "source_url")
    site = {key: facility[key] for key in site_fields if key in facility} if facility else None
    if site:
        site["care_offerings"] = sorted(site["care_offerings"])
    accounts = []
    for record in sorted(item["crm"], key=lambda account: account["account_id"]):
        facts = {key: record[key] for key in CRM_BUSINESS_FIELDS if key in record}
        if not facts.get("note"):
            facts.pop("note", None)
        accounts.append(facts)
    identity = [item["category"], site, accounts, item["proposed_change"]]
    if "review_resolution" in item:
        resolution = dict(item["review_resolution"])
        resolution["related"] = [
            {key: row.get(key, "") for key in CRM_BUSINESS_FIELDS}
            for row in resolution.get("related", [])
        ]
        identity.append(resolution)
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def proposal(category, facility, accounts, confidence, reason, change):
    item = {
        "category": category, "website": facility,
        "crm": sorted(accounts, key=lambda account: account["account_id"]),
        "confidence": confidence, "reason": reason, "proposed_change": change,
    }
    return dict(item, id=business_id(item))


def build_proposals(facilities, accounts):
    if not any(account["account_id"] == BELLHAVEN for account in accounts):
        raise ValueError("The Bellhaven parent account is missing from the CRM snapshot.")
    excluded = completed_relationship_sources(accounts)
    by_address = defaultdict(list)
    for account in accounts:
        if account["account_id"] in excluded:
            continue
        key = address(account, "billing_")
        if all(key):
            by_address[key].append(account)

    linked = set()
    results = []
    for account in accounts:
        if (account["account_id"] not in excluded
                and (account.get("duplicate_of_account") or account.get("chow_current_account"))):
            site = next((row for row in facilities if address(row) == address(account, "billing_")), None)
            results.append(proposal(
                "Relationship investigation", site, [account], "Manual review",
                "The recorded duplicate or CHOW relationship is missing, cyclic, or inconsistent.",
                {"account_id": account["account_id"], "review": "Verify the link target and account relationship."},
            ))
    for facility in facilities:
        key = address(facility)
        exact = by_address.get(key, [])
        if exact:
            linked.update(account["account_id"] for account in exact)
            results.extend(full_address_proposals(facility, exact))
            continue

        near, weak = [], []
        for account in accounts:
            if account["account_id"] in excluded:
                continue
            billing = address(account, "billing_")
            same_name = strong_name(facility["name"], account["name"])
            same_locality = key[1:3] == billing[1:3]
            if same_name and same_locality and (key[0] == billing[0] or key[3] == billing[3]):
                near.append(account)
            elif same_name or (same_locality and len(
                (name_tokens(facility["name"]) & name_tokens(account["name"]))
                - {"the", "at", "of", "and", "bellhaven"}
            ) >= 2):
                weak.append(account)

        if near:
            linked.update(account["account_id"] for account in near)
            results.append(proposal(
                "Address discrepancy", facility, near, "Manual review",
                "Name and locality agree, with a street or ZIP discrepancy. CRM billing may differ from the public location.",
                {"review": "Verify facility identity and billing address."},
            ))
        else:
            results.append(proposal(
                "Missing CRM account", facility, weak, "Manual review",
                "No supported address match across CRM. Any displayed name or locality overlaps are weak evidence.",
                {"review": "Rule out aliases and alternate billing addresses before considering a new account.",
                 "candidate_parent_id": BELLHAVEN},
            ))

    for account in accounts:
        if (account["parent_id"] == BELLHAVEN and account["account_id"] not in linked
                and account["account_id"] not in excluded and account.get("status", "Active") == "Active"):
            neighbors = [row for row in by_address.get(address(account, "billing_"), [])
                         if row["account_id"] != account["account_id"]]
            results.append(proposal(
                "Stale Bellhaven account", None, [account, *neighbors], "Manual review",
                "No supported match in the complete website roster. Omission does not establish closure or ownership.",
                {"account_id": account["account_id"],
                 "review": "Verify current operations, legal operator, and billing history before changing status or parent."},
            ))
    return sorted(results, key=lambda row: (row["category"], row["id"]))


def full_address_proposals(facility, accounts):
    if len(accounts) > 1:
        duplicate = (
            len({row["parent_id"] for row in accounts}) == 1
            and all(strong_name(facility["name"], row["name"]) for row in accounts)
        )
        return [proposal(
            "Duplicate candidate" if duplicate else "Ambiguous address overlap", facility, accounts,
            "Strong site match" if duplicate else "Manual review",
            "Name, full address and parent agree across multiple rows." if duplicate
            else "Multiple CRM rows share the full address. Verify which legal operator and billing relationship each row represents.",
            {"review": "Verify canonical account and downstream links." if duplicate
             else "Resolve account identity and operator before proposing a parent or duplicate change."},
        )]

    account = accounts[0]
    strong = strong_name(facility["name"], account["name"])
    confidence = "Strong site match" if strong else "Manual review"
    reason = ("Full address and normalized names agree." if strong
              else "Full address agrees and names differ materially. Verify facility identity.")
    results = []
    if facility["name"] != account["name"]:
        results.append(proposal(
            "Name correction", facility, accounts, confidence, reason,
            {"account_id": account["account_id"], "candidate_name": facility["name"],
             "requires_confirmation": True},
        ))
    if account["parent_id"] != BELLHAVEN:
        results.append(proposal(
            "Possible wrong parent", facility, accounts, confidence,
            reason + " CRM parent differs from the website affiliation. Confirm the legal operator and correct billing parent.",
            parent_plan(account, BELLHAVEN, facility),
        ))
    return results


def is_actionable(item):
    """Leave ambiguous identity and unselected duplicate targets for investigation."""
    if "review_resolution" in item:
        from review_plans import resolution_plan
        try:
            resolution_plan(item, item["review_resolution"].get("ownership_confirmed", False))
            return True
        except (ValueError, RuntimeError, KeyError, TypeError):
            return False
    replacement = item["proposed_change"].get("new_account")
    return (
        item["proposed_change"].get("strategy") != "needs_investigation"
        and (replacement is None or bool(replacement.get("name") and replacement.get("billing_street")))
        and item["category"] in {"Name correction", "Possible wrong parent"}
        and item["confidence"] == "Strong site match"
        and len(item["crm"]) == 1
        and not any(item["crm"][0].get(field) for field in
                    ("duplicate_of_account", "chow_current_account"))
    )


def is_findlay_direct_candidate(item):
    """Restrict the first parent preview to the verified Findlay evidence."""
    if item["category"] != "Possible wrong parent" or not is_actionable(item):
        return False
    crm = item["crm"][0]
    site = item.get("website") or {}
    expected_address = ("1800 n blanchard st", "findlay", "OH", "45840")
    return (
        crm["account_id"] == FINDLAY_ID and crm["name"] == site.get("name") == FINDLAY_NAME
        and crm["parent_id"] == "" and crm["status"] == "Active"
        and crm["lifetime_revenue"] == 22000 and crm["outstanding_ar"] == 0
        and site.get("source_url") == BASE_URL + FINDLAY
        and all(isinstance(site.get(field), str) for field in ADDRESS_FIELDS)
        and address(site) == address(crm, "billing_") == expected_address
        and item["proposed_change"] == parent_plan(crm, BELLHAVEN, site)
    )


def completed_relationship_sources(accounts):
    """Honor explicit acyclic CRM links to a canonical/current row."""
    by_id = {row["account_id"]: row for row in accounts}
    excluded = set()
    for original in accounts:
        current = original
        seen = {original["account_id"]}
        while True:
            duplicate = current.get("duplicate_of_account", "")
            chow = current.get("chow_current_account", "")
            if duplicate and chow:
                break
            linked_id = duplicate or chow
            if not linked_id:
                if current is not original:
                    excluded.add(original["account_id"])
                break
            target = by_id.get(linked_id)
            if not target or linked_id in seen:
                break
            if chow and (target["parent_id"] != BELLHAVEN or target["status"] != "Active"):
                break
            seen.add(linked_id)
            current = target
    return excluded
