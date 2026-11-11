"""Apply locally approved proposals through a gated CRM adapter."""

from contextlib import closing
from datetime import datetime, timezone
import fcntl
import json
import hashlib
import re
from urllib.parse import urlsplit

from crm_write import CRMWriteClient, live_canary_armed, CANARY_ID, WriteBlocked, WriteRejected, WriteUncertain, valid_id, validate_creation
from live_crm import CANARY_NEW_NAME, LiveAuthorization, LiveCRMTransport, is_ashland_canary
from reconcile import (
    BELLHAVEN, BELLHAVEN_PARENT_NAME, FINDLAY_ID, address, is_actionable,
    is_findlay_direct_candidate, parent_plan, strong_name,
)
from sources import BASE_URL, CRM_FIELDS, CRM_FIELD_TYPES
from storage import DB_PATH, connect, database_path, latest_snapshot, save_review


def reviewed_plan(item, confirmed):
    """Validate evidence and derive the precise allowable mutation."""
    if not is_actionable(item):
        raise WriteBlocked("Account identity or operator needs investigation.")
    if "review_resolution" in item:
        from review_plans import resolution_plan
        try:
            return resolution_plan(item, confirmed)
        except (ValueError, KeyError, TypeError) as error:
            raise WriteBlocked(str(error)) from None
    record = item["crm"][0]
    site = item.get("website") or {}
    plan = item["proposed_change"]
    url = urlsplit(site.get("source_url", ""))
    origin = urlsplit(BASE_URL)
    if (
        not valid_id(record.get("account_id")) or record["account_id"] == BELLHAVEN
        or any(type(record.get(field)) is not CRM_FIELD_TYPES[field]
               for field in CRM_FIELDS)
        or (url.scheme, url.netloc) != (origin.scheme, origin.netloc)
        or not re.fullmatch(r"/communities/[a-z0-9-]+", url.path)
        or not all(isinstance(site.get(field), str) and site[field].strip()
                   for field in ("name", "street", "city", "state", "zip"))
        or not all(address(site)) or address(site) != address(record, "billing_")
        or not strong_name(site["name"], record["name"])
    ):
        raise WriteBlocked("Reviewed account identity or source evidence is incomplete.")
    if item["category"] == "Name correction":
        if plan.get("account_id") != record["account_id"] or plan.get("candidate_name") != site["name"]:
            raise WriteBlocked("The approved name does not match the reviewed evidence.")
        return record, {"name": plan["candidate_name"]}, None
    if (
        not confirmed or not plan.get("requires_confirmation")
        or record["parent_id"] == BELLHAVEN
        or (record["parent_id"] and not valid_id(record["parent_id"]))
        or plan != parent_plan(record, BELLHAVEN, site)
    ):
        raise WriteBlocked("A confirmed operator and an unchanged financial plan are required.")
    creation = plan.get("new_account")
    if creation is not None:
        validate_creation(creation)
    return record, plan["existing_account_changes"], creation


def verified_change(current, desired):
    # The server may advance updated_at when applying the requested change.
    return all(current[field] == value for field, value in desired.items() if field != "updated_at")


def verified_creation(current, creation, item):
    checks = item["proposed_change"].get("new_account_checks", {})
    return all(current[field] == value for field, value in {**creation, **checks}.items())


def apply_approved(client, proposal_id, *, confirmation, path=None, required_snapshot_id=None):
    """Execute one current approval. A committed intent always precedes POST/PATCH."""
    client.authorize(proposal_id, confirmation)
    db_path = database_path(path)
    if client.mode == "live" and db_path != DB_PATH.resolve():
        raise WriteBlocked("Live execution requires the assessment database.")
    # flock is separate from SQLite's transaction locks. A process crash releases
    # this lock, while committed request intents remain available for recovery.
    with db_path.open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with closing(connect(db_path)) as db:
            def require_snapshot():
                if required_snapshot_id is not None and db.execute(
                    "SELECT MAX(id) FROM snapshots"
                ).fetchone()[0] != required_snapshot_id:
                    raise WriteBlocked("The confirmed Findlay snapshot is no longer current.")

            require_snapshot()
            key = (client.mode, proposal_id)
            stored = db.execute("""
                SELECT state, phase, created_account_id, message
                FROM executions WHERE mode = ? AND proposal_id = ?
            """, key).fetchone()
            row = db.execute("""
                SELECT original.evidence, a.ownership_confirmed, (
                    SELECT latest.evidence FROM proposals latest WHERE latest.id = a.proposal_id
                      AND latest.snapshot_id = (SELECT MAX(id) FROM snapshots)
                )
                FROM approvals a JOIN proposals original
                  ON original.id = a.proposal_id AND original.snapshot_id = a.snapshot_id
                WHERE a.proposal_id = ? AND a.state = 'approved'
            """, (proposal_id,)).fetchone()
            if not row or (not row[2] and not stored):
                raise WriteBlocked("A current local approval is required.")
            item = json.loads(row[2] or row[0])
            expected, changes, creation = reviewed_plan(item, bool(row[1]))
            if client.mode == "live":
                if not (is_ashland_canary(item) or is_findlay_direct_candidate(item)
                        or client.general_write_enabled):
                    raise WriteBlocked("The live proposal is outside the exact canary scope.")
                client.verify_live_plan(proposal_id, expected, changes, creation)
                if stored and stored[1] == "patch_intent" and stored[0] != "applied":
                    raise WriteBlocked("A live PATCH intent already exists. Verify by GET only.")
                if not stored or stored[0] != "applied":
                    require_live_approval(db, proposal_id)
            target = expected["account_id"] if expected else "new-" + hashlib.sha256(
                json.dumps(address(creation, "billing_")).encode()
            ).hexdigest()[:24]
            if stored and stored[0] == "applied":
                return {"state": stored[0], "created_account_id": stored[2], "message": stored[3]}
            if db.execute("""
                SELECT 1 FROM executions WHERE mode = ? AND target_id = ? AND proposal_id != ?
                  AND (state != 'applied' OR (mode = 'live' AND ? = 0))
                  AND phase IN ('post_intent', 'created', 'patch_intent')
            """, (client.mode, target, proposal_id, int(client.general_write_enabled))).fetchone():
                raise WriteBlocked("Another proposal has an unresolved write for this account.")
            phase, created_id = stored[1:3] if stored else ("ready", None)

            def checkpoint(state, message, *, dispatch=False):
                with db:
                    if client.mode == "live" and dispatch:
                        db.execute("BEGIN IMMEDIATE")
                        require_snapshot()
                        require_live_approval(db, proposal_id)
                        db.execute("""
                            INSERT INTO live_canary_baselines(proposal_id, evidence) VALUES (?, ?)
                            ON CONFLICT(proposal_id) DO UPDATE SET evidence = excluded.evidence
                        """, (proposal_id, json.dumps(item)))
                    db.execute("""
                        INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(mode, proposal_id) DO UPDATE SET state = excluded.state,
                            phase = excluded.phase, created_account_id = excluded.created_account_id,
                            message = excluded.message, updated_at = excluded.updated_at
                    """, (*key, target, state, phase, created_id, message, datetime.now(timezone.utc).isoformat()))
                return {"state": state, "phase": phase, "created_account_id": created_id, "message": message}

            def require_current_proposal():
                require_snapshot()
                current = db.execute("""
                    SELECT 1 FROM proposals WHERE id = ?
                      AND snapshot_id = (SELECT MAX(id) FROM snapshots)
                """, (proposal_id,)).fetchone()
                if not current:
                    facilities = json.loads(db.execute(
                        "SELECT facilities FROM snapshots ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0])
                    site = item.get("website")
                    same_site = site and any(
                        {k: v for k, v in row.items() if k != "observation_date"} ==
                        {k: v for k, v in site.items() if k != "observation_date"} for row in facilities
                    )
                    if phase != "created" or not same_site:
                        raise WriteBlocked("The proposal is absent from the latest review. No further mutation was sent.")
                if client.mode == "live":
                    require_live_approval(db, proposal_id)

            checkpoint("applying", "Checking approved account evidence.")
            try:
                if phase == "post_intent":
                    created_id = client.reconcile_creation(proposal_id)
                    if not valid_id(created_id):
                        raise WriteUncertain("Creation outcome is unknown. No second POST was sent.")
                    phase = "created"
                    checkpoint("applying", "Recovered replacement ID from the operation receipt.")
                for related in item.get("review_resolution", {}).get("related", []):
                    if client.read(related["account_id"]) != related:
                        raise WriteBlocked("Related account evidence changed after review.")
                desired = dict(expected, **changes) if expected else {}
                parent_id = creation["parent_id"] if creation else changes.get("parent_id")
                if parent_id:
                    parent = client.read(parent_id)
                    if parent["parent_id"] or parent["status"] != "Active" or not parent["name"].strip():
                        raise WriteBlocked("The target is not a valid active parent account.")
                    if target == FINDLAY_ID and parent["name"] != BELLHAVEN_PARENT_NAME:
                        raise WriteBlocked("The target parent name does not match Bellhaven's reviewed account.")
                    if not creation:
                        desired["parent_name"] = parent["name"]

                if creation and phase == "ready":
                    require_current_proposal()
                    if expected and client.read(target) != expected:
                        raise WriteBlocked("CRM evidence changed before creation.")
                    verify_absence(client, creation, exclude=target if expected else None)
                    with db:
                        site_key = json.dumps(address(creation, "billing_"))
                        owner = db.execute("SELECT proposal_id FROM creation_claims WHERE mode=? AND site_key=?",
                                           (client.mode, site_key)).fetchone()
                        if owner and owner[0] != proposal_id:
                            raise WriteBlocked("Another creation intent exists for this facility.")
                        db.execute("INSERT OR IGNORE INTO creation_claims VALUES (?,?,?)",
                                   (client.mode, site_key, proposal_id))
                    phase = "post_intent"
                    checkpoint("applying", "Creation intent recorded.", dispatch=True)
                    try:
                        created_id = client.create(creation, proposal_id=proposal_id, confirmation=confirmation)
                    except WriteRejected:
                        with db:
                            db.execute("DELETE FROM creation_claims WHERE mode=? AND proposal_id=?", key)
                        phase = "ready"
                        raise
                    phase = "created"
                    checkpoint("applying", "Replacement ID recorded.")

                if creation:
                    if created_id in {None, target, creation["parent_id"]}:
                        raise WriteBlocked("The replacement account ID is invalid.")
                    replacement = client.read(created_id)
                    if not verified_creation(replacement, creation, item):
                        raise WriteBlocked("Replacement read-back differs from the approved account data.")
                    if client.mode == "live" and replacement["parent_name"] != parent["name"]:
                        raise WriteBlocked("Created account parent name did not verify.")
                    client.use_created_account(created_id)
                    if expected is None:
                        return checkpoint("applied", "Created account verified by GET.")
                    changes = {"chow_current_account": created_id}
                    desired = dict(expected, **changes)

                if phase != "patch_intent":
                    require_current_proposal()
                current = client.read(target)
                if phase == "patch_intent":
                    if not verified_change(current, desired):
                        raise WriteUncertain("PATCH outcome is unverified. No second PATCH was sent.")
                else:
                    if current != expected:
                        raise WriteBlocked("CRM evidence changed before PATCH.")
                    for field in {"parent_id", "duplicate_of_account", "chow_current_account"} & changes.keys():
                        verify_reference(client, target, field, changes[field], expected,
                                         identity_confirmed=bool(item.get("review_resolution")))
                    if changes.keys() & {"parent_id", "duplicate_of_account", "chow_current_account"}:
                        if client.read(target) != expected:
                            raise WriteBlocked("CRM evidence changed before PATCH.")
                    previous = phase
                    phase = "patch_intent"
                    try:
                        checkpoint("applying", "Patch intent recorded.", dispatch=True)
                        client.patch(target, changes, proposal_id=proposal_id, confirmation=confirmation)
                    except (WriteRejected, WriteBlocked):
                        phase = previous
                        raise
                    if not verified_change(client.read(target), desired):
                        raise WriteUncertain("PATCH returned but read-back did not verify the approved change.")
                if creation:
                    replacement = client.read(created_id)
                    if (not verified_creation(replacement, creation, item)
                            or client.mode == "live" and replacement["parent_name"] != parent["name"]):
                        raise WriteUncertain("CHOW link exists but the new account no longer matches the approved data.")
                return checkpoint("applied", "Applied and verified by account read-back.")
            except WriteRejected:
                return checkpoint("failed", "The adapter confirmed that the request was rejected.")
            except (WriteBlocked, WriteUncertain) as error:
                return checkpoint("needs_investigation", str(error))
            except (TimeoutError, OSError):
                return checkpoint("needs_investigation", "Transport interrupted. Check the recorded request intent.")


def require_live_approval(db, proposal_id):
    row = db.execute("""
        SELECT 1 FROM live_review_approvals
        WHERE proposal_id = ? AND snapshot_id = (SELECT MAX(id) FROM snapshots)
    """, (proposal_id,)).fetchone()
    if not row:
        raise WriteBlocked("Fresh local live review approval is required for the latest assessment snapshot.")


def reconcile_live_canary(client, proposal_id, path=None):
    """Verify a recorded creation or PATCH by GET, including both CHOW accounts."""
    db_path = database_path(path)
    if db_path != DB_PATH.resolve() or database_path() != DB_PATH.resolve():
        raise WriteBlocked("Canary verification requires the assessment database.")
    with db_path.open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with closing(connect(db_path)) as db, db:
            row = db.execute("""
                SELECT e.state, b.evidence, e.target_id, e.created_account_id, a.ownership_confirmed FROM executions e
                JOIN live_canary_baselines b ON b.proposal_id = e.proposal_id
                JOIN approvals a ON a.proposal_id = e.proposal_id
                WHERE e.mode = 'live' AND e.proposal_id = ?
                  AND e.phase IN ('patch_intent', 'created')
            """, (proposal_id,)).fetchone()
            if not row:
                raise WriteBlocked("There is no recorded live canary dispatch to verify.")
            if row[0] == "applied":
                return "applied"
            item = json.loads(row[1])
            target = row[2]
            expected, changes, creation = reviewed_plan(item, bool(row[4]))
            if creation and expected is None:
                if not valid_id(row[3]) or row[3] == creation["parent_id"]:
                    raise WriteBlocked("No usable creation ID is recorded. Investigate; do not repeat POST.")
                try:
                    current = client.read(row[3])
                    parent = client.read(creation["parent_id"])
                    verified = (verified_creation(current, creation, item)
                                and current["parent_name"] == parent["name"])
                except (WriteBlocked, WriteUncertain, OSError):
                    verified = False
                state = "applied" if verified else "needs_investigation"
                db.execute("""UPDATE executions SET state=?, message=?, updated_at=?
                              WHERE mode='live' AND proposal_id=?""",
                           (state, "Created account verified by GET." if verified else
                            "Created account read-back remains unverified. No POST was sent.",
                            datetime.now(timezone.utc).isoformat(), proposal_id))
                return state
            if target == CANARY_ID and is_ashland_canary(item):
                desired = dict(item["crm"][0], name=CANARY_NEW_NAME)
            elif target == FINDLAY_ID and is_findlay_direct_candidate(item):
                desired = dict(item["crm"][0], parent_id=BELLHAVEN, parent_name=BELLHAVEN_PARENT_NAME)
            elif item.get("review_resolution") or item["category"] == "Name correction" or creation:
                expected, changes, creation = reviewed_plan(item, bool(row[4]))
                if creation:
                    if not row[3]:
                        raise WriteBlocked("The replacement account ID is unresolved.")
                    changes = {"chow_current_account": row[3]}
                desired = dict(expected, **changes)
                if "parent_id" in changes:
                    desired["parent_name"] = client.read(changes["parent_id"])["name"]
            else:
                raise WriteBlocked("The recorded dispatch baseline does not match an approved strategy.")
            try:
                verified = verified_change(client.read(target), desired)
                if creation:
                    replacement = client.read(row[3])
                    parent = client.read(creation["parent_id"])
                    verified = (verified and verified_creation(replacement, creation, item)
                                and replacement["parent_name"] == parent["name"])
            except (WriteBlocked, WriteUncertain, OSError):
                verified = False
            state = "applied" if verified else "needs_investigation"
            message = ("Applied and verified by account read-back." if verified
                       else "Read-back did not verify the exact change. No second PATCH was sent.")
            db.execute("""
                UPDATE executions SET state = ?, message = ?, updated_at = ?
                WHERE mode = 'live' AND proposal_id = ?
            """, (state, message, datetime.now(timezone.utc).isoformat(), proposal_id))
            return state


def confirm_findlay_dry_run(snapshot_id, proposal_id, phrase, path=None):
    """Check the fresh local decision and return a plan without contacting CRM."""
    snapshot = latest_snapshot(path)
    if not snapshot or snapshot["id"] != snapshot_id:
        raise WriteBlocked("Review the latest snapshot before confirming the Findlay dry run.")
    item = next((row for row in snapshot["proposals"] if row["id"] == proposal_id), None)
    if not item or not is_findlay_direct_candidate(item):
        raise WriteBlocked("Only the exact Findlay direct parent correction can be confirmed.")
    if item["review_state"] != "approved" or not item["live_reviewed_at"] or not item["ownership_confirmed"]:
        raise WriteBlocked("Fresh local approval and explicit ownership confirmation are required.")
    if phrase != f"APPLY {proposal_id} {FINDLAY_ID}":
        raise WriteBlocked("Type the exact Findlay proposal and account confirmation.")
    expected, payload, creation = reviewed_plan(item, item["ownership_confirmed"])
    if creation is not None or payload != {"parent_id": BELLHAVEN}:
        raise WriteBlocked("Findlay is outside the reviewed direct-parent strategy.")
    return {"account_id": expected["account_id"], "payload": payload}


def apply_findlay_mock(client, snapshot_id, proposal_id, phrase, path=None):
    """Exercise the confirmed Findlay workflow through the existing mock executor."""
    if client.mode != "mock":
        raise WriteBlocked("Findlay parent changes are mock-only.")
    confirm_findlay_dry_run(snapshot_id, proposal_id, phrase, path)
    return apply_approved(client, proposal_id, confirmation=proposal_id, path=path,
                          required_snapshot_id=snapshot_id)


def approve_and_apply_findlay(snapshot_id, proposal_id, reviewed, *, accepted, typed, path=None):
    """Translate the final dialog action into approval and the existing executor."""
    if not live_canary_armed(FINDLAY_ID) or database_path(path) != DB_PATH.resolve():
        raise WriteBlocked("Findlay live Apply is disabled.")
    if accepted is not True or typed != "APPLY":
        raise WriteBlocked("Accept the displayed evidence and type APPLY before confirming.")
    snapshot = latest_snapshot(path)
    if not snapshot or snapshot["id"] != snapshot_id:
        raise WriteBlocked("The review snapshot changed. Reopen the confirmation.")
    item = next((row for row in snapshot["proposals"] if row["id"] == proposal_id), None)
    if not item or not is_findlay_direct_candidate(item) or item["review_state"] not in ("pending", "approved"):
        raise WriteBlocked("The Findlay proposal is unavailable or rejected.")
    if item["crm"][0] != reviewed:
        raise WriteBlocked("The displayed Findlay evidence changed. Reopen the confirmation.")
    if snapshot.get("findlay_execution"):
        if snapshot["findlay_execution"]["state"] == "applied":
            return {"state": "applied", "message": "Applied and verified previously."}
        raise WriteBlocked("A Findlay execution already exists. Inspect it before continuing.")
    # The reviewer uses a short word. The scoped authorization still binds to
    # the immutable proposal ID, exact account and complete reviewed baseline.
    phrase = f"APPLY {proposal_id} {FINDLAY_ID}"
    permit = LiveAuthorization.confirm(proposal_id, reviewed, phrase)
    client = CRMWriteClient(LiveCRMTransport(permit))
    client.authorize(proposal_id, proposal_id)
    save_review(snapshot_id, proposal_id, "approved", ownership_confirmed=True, live_review=True, path=path)
    result = apply_approved(client, proposal_id, confirmation=proposal_id, path=path,
                            required_snapshot_id=snapshot_id)
    if result["state"] == "needs_investigation" and result.get("phase") == "patch_intent":
        state = reconcile_live_canary(CRMWriteClient(LiveCRMTransport()), proposal_id, path)
        return dict(result, state=state)
    return result


def verify_absence(client, creation, *, exclude=None):
    """Require a complete account listing; uncertain identity blocks a POST."""
    for row in client.list_accounts():
        if row["account_id"] == exclude:
            continue
        same_address = address(row, "billing_") == address(creation, "billing_")
        same_local_name = (
            strong_name(row["name"], creation["name"])
            and row["billing_city"].casefold().strip() == creation["billing_city"].casefold().strip()
            and row["billing_state"] == creation["billing_state"]
        )
        if same_address or same_local_name:
            raise WriteBlocked("An existing matching account requires review before creation.")


def verify_reference(client, target, field, reference, expected, *, identity_confirmed=False):
    seen = {target}
    first = client.read(reference)
    if field == "chow_current_account":
        if (first["parent_id"] == expected["parent_id"] or
                not identity_confirmed and address(first, "billing_") != address(expected, "billing_")):
            raise WriteBlocked("The CHOW target must be a verified same-site account under a different parent.")
    current = first
    while True:
        if current["account_id"] in seen:
            raise WriteBlocked("The account relationship would create a cycle.")
        seen.add(current["account_id"])
        next_id = current[field]
        if not next_id:
            return
        current = client.read(next_id)
