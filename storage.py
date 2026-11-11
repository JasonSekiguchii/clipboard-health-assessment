"""Local SQLite snapshots and review decisions."""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from live_crm import CANARY_ID, is_ashland_canary
from reconcile import FINDLAY_ID, business_id, is_actionable, is_findlay_direct_candidate

DB_PATH = Path(__file__).resolve().parent / "data" / "review.sqlite3"
REVIEW_DECISIONS = ("approved", "rejected", "unresolved")


def database_path(path=None):
    if path is None:
        override = os.environ.get("CLIPBOARD_REVIEW_DB_PATH")
        path = DB_PATH
        if override is not None:
            if not override.strip():
                raise sqlite3.OperationalError("An alternate review database path is required.")
            path = Path(override).resolve()
            if path == DB_PATH.resolve() or (
                path.exists() and DB_PATH.exists() and path.samefile(DB_PATH)
            ):
                raise sqlite3.OperationalError("Test mode requires a separate review database.")
    return Path(path).resolve()


def connect(path):
    db = sqlite3.connect(database_path(path))
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY,
            collected_at TEXT NOT NULL,
            facilities TEXT NOT NULL,
            account_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proposals (
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            id TEXT NOT NULL,
            evidence TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, id)
        );
        CREATE TABLE IF NOT EXISTS decisions (
            snapshot_id INTEGER NOT NULL,
            proposal_id TEXT NOT NULL,
            disposition TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, proposal_id),
            FOREIGN KEY (snapshot_id, proposal_id) REFERENCES proposals(snapshot_id, id)
        );
        CREATE TABLE IF NOT EXISTS approvals (
            proposal_id TEXT PRIMARY KEY,
            snapshot_id INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('approved', 'rejected')),
            recorded_at TEXT NOT NULL,
            ownership_confirmed INTEGER NOT NULL,
            FOREIGN KEY (snapshot_id, proposal_id) REFERENCES proposals(snapshot_id, id)
        );
        CREATE TABLE IF NOT EXISTS snapshot_accounts (
            snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id),
            records TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_events (
            id INTEGER PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            snapshot_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (snapshot_id, proposal_id) REFERENCES proposals(snapshot_id, id)
        );
        CREATE TABLE IF NOT EXISTS review_resolutions (
            id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL,
            evidence TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS live_review_approvals (
            snapshot_id INTEGER NOT NULL,
            proposal_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, proposal_id),
            FOREIGN KEY (snapshot_id, proposal_id) REFERENCES proposals(snapshot_id, id)
        );
        CREATE TABLE IF NOT EXISTS live_canary_baselines (
            proposal_id TEXT PRIMARY KEY REFERENCES approvals(proposal_id),
            evidence TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS creation_claims (
            mode TEXT NOT NULL,
            site_key TEXT NOT NULL,
            proposal_id TEXT NOT NULL REFERENCES approvals(proposal_id),
            PRIMARY KEY (mode, site_key)
        );
        CREATE TABLE IF NOT EXISTS executions (
            mode TEXT NOT NULL CHECK (mode IN ('mock', 'live')),
            proposal_id TEXT NOT NULL REFERENCES approvals(proposal_id),
            target_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('applying', 'applied', 'failed', 'needs_investigation')),
            phase TEXT NOT NULL,
            created_account_id TEXT,
            message TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mode, proposal_id)
        );
    """)
    return db


def save_snapshot(facilities, account_count, proposals, path=None, *, accounts=None):
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if accounts is not None:
            proposals = overlay_resolutions(db, proposals, accounts)
        wanted = {business_id(item) for item in proposals}
        previous_ids = {}
        if wanted:
            # Prefer an existing decision or execution ID to keep its journal and
            # timestamp attached. Never rewrite historical snapshots or decisions.
            history = db.execute("""
                SELECT p.id, p.evidence FROM proposals p
                LEFT JOIN approvals a ON a.proposal_id = p.id
                WHERE p.snapshot_id = (
                    SELECT MIN(first.snapshot_id) FROM proposals first WHERE first.id = p.id
                )
                ORDER BY EXISTS (SELECT 1 FROM executions e WHERE e.proposal_id = p.id
                                 AND e.mode = 'live') DESC,
                         a.recorded_at DESC, p.snapshot_id, p.id
            """)
            for proposal_id, evidence in history:
                key = business_id(json.loads(evidence))
                if key in wanted:
                    previous_ids.setdefault(key, proposal_id)
        proposals = [dict(item, id=previous_ids.get(business_id(item), item["id"])) for item in proposals]
        cursor = db.execute(
            "INSERT INTO snapshots(collected_at, facilities, account_count) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), json.dumps(facilities), account_count),
        )
        snapshot_id = cursor.lastrowid
        db.executemany(
            "INSERT INTO proposals(snapshot_id, id, evidence) VALUES (?, ?, ?)",
            [(snapshot_id, row["id"], json.dumps(row)) for row in proposals],
        )
        if accounts is not None:
            db.execute("INSERT INTO snapshot_accounts VALUES (?, ?)", (snapshot_id, json.dumps(accounts)))
        return snapshot_id


def latest_snapshot(path=None):
    with closing(connect(path)) as db, db:
        snapshot = db.execute(
            "SELECT id, collected_at, facilities, account_count FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if snapshot is None:
            return None
        snapshot_id, collected_at, facilities, account_count = snapshot
        rows = db.execute("""
            SELECT p.evidence, COALESCE(a.state, (SELECT state FROM review_events
                WHERE proposal_id=p.id ORDER BY id DESC LIMIT 1), 'pending'),
                COALESCE(a.recorded_at, (SELECT recorded_at FROM review_events WHERE proposal_id=p.id ORDER BY id DESC LIMIT 1)),
                a.ownership_confirmed,
                e.state, e.message, l.recorded_at,
                COALESCE((SELECT reason FROM review_events WHERE proposal_id=p.id ORDER BY id DESC LIMIT 1), ''),
                (SELECT disposition FROM decisions
                 WHERE proposal_id = p.id AND snapshot_id <= p.snapshot_id
                 ORDER BY snapshot_id DESC LIMIT 1)
            FROM proposals p LEFT JOIN approvals a ON a.proposal_id = p.id
            LEFT JOIN executions e ON e.proposal_id = p.id AND e.mode = 'live'
            LEFT JOIN live_review_approvals l ON l.proposal_id = p.id AND l.snapshot_id = p.snapshot_id
            WHERE p.snapshot_id = ? ORDER BY p.rowid
        """, (snapshot_id,))
        history = {}
        for target, *entry in db.execute("""
            SELECT target_id, proposal_id, state, phase, message FROM executions
            WHERE mode = 'live' AND target_id IN (?, ?) ORDER BY updated_at DESC
        """, (CANARY_ID, FINDLAY_ID)):
            history.setdefault(target, dict(zip(("proposal_id", "state", "phase", "message"), entry)))
        inventory = db.execute("SELECT records FROM snapshot_accounts WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        execution_history = [
            dict(zip(("mode", "proposal_id", "target_id", "state", "phase", "created_account_id", "message", "updated_at"), row))
            for row in db.execute("SELECT * FROM executions ORDER BY updated_at DESC")
        ]
        return {
            "accounts": json.loads(inventory[0]) if inventory else [],
            "execution_history": execution_history,
            "canary_execution": history.get(CANARY_ID),
            "findlay_execution": history.get(FINDLAY_ID),
            "id": snapshot_id, "collected_at": collected_at, "facility_count": len(json.loads(facilities)),
            "account_count": account_count,
            "proposals": [
                dict(json.loads(evidence), review_state=state, reviewed_at=recorded_at,
                     ownership_confirmed=bool(confirmed), previous_disposition=previous,
                     execution_state=execution_state, execution_message=message, live_reviewed_at=live_reviewed_at, decision_reason=reason)
                for evidence, state, recorded_at, confirmed, execution_state, message, live_reviewed_at, reason, previous in rows
            ],
        }


def save_review(snapshot_id, proposal_id, state, *, ownership_confirmed=False, live_review=False, path=None, reason="", general_review=False):
    if live_review and (
        state != "approved" or "CLIPBOARD_REVIEW_DB_PATH" in os.environ
        or database_path(path) != DB_PATH.resolve()
    ):
        raise ValueError("Live review approval requires the assessment database.")
    if state not in REVIEW_DECISIONS:
        raise ValueError("Invalid local review decision.")
    if general_review and not reason.strip():
        raise ValueError("Record the supporting evidence for this general correction.")
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""
            SELECT evidence FROM proposals
            WHERE snapshot_id = ? AND id = ? AND snapshot_id = (SELECT MAX(id) FROM snapshots)
        """, (snapshot_id, proposal_id)).fetchone()
        if row is None:
            raise ValueError("Review the latest snapshot before deciding.")
        item = json.loads(row[0])
        if live_review and not (
            is_ashland_canary(item) or is_findlay_direct_candidate(item)
            or is_actionable(item) and (general_review or item.get("review_resolution") or item["category"] == "Name correction")
        ):
            raise ValueError("This proposal is outside the scoped canary reviews.")
        if state == "unresolved":
            if not reason.strip():
                raise ValueError("Record why this proposal remains unresolved.")
            if db.execute("SELECT 1 FROM approvals WHERE proposal_id=?", (proposal_id,)).fetchone():
                raise ValueError("A decision has already been recorded for this proposal.")
            db.execute("INSERT INTO review_events(proposal_id,snapshot_id,state,reason,recorded_at) VALUES (?,?,?,?,?)",
                       (proposal_id, snapshot_id, state, reason.strip(), datetime.now(timezone.utc).isoformat()))
            return
        if state == "approved":
            if not is_actionable(item):
                raise ValueError("This finding requires investigation before it can be approved.")
            ownership_plan = item["proposed_change"].get("strategy") in {"reparent_existing", "preserve_create_and_link"}
            if (item["category"] == "Possible wrong parent" or ownership_plan) and ownership_confirmed is not True:
                if is_findlay_direct_candidate(item):
                    raise ValueError("Accept the Findlay evidence for this fictional assessment before approving.")
                raise ValueError("Confirm the legal operator and correct billing parent before approving.")
        cursor = db.execute("""
            INSERT INTO approvals(proposal_id, snapshot_id, state, recorded_at, ownership_confirmed)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(proposal_id) DO NOTHING
        """, (proposal_id, snapshot_id, state, datetime.now(timezone.utc).isoformat(),
              int(state == "approved" and ownership_confirmed is True)))
        if cursor.rowcount == 0:
            recorded = db.execute("SELECT state FROM approvals WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if not live_review or recorded[0] != "approved":
                raise ValueError("A decision has already been recorded for this proposal.")
        db.execute("INSERT INTO review_events(proposal_id,snapshot_id,state,reason,recorded_at) VALUES (?,?,?,?,?)",
                   (proposal_id, snapshot_id, state, reason.strip(), datetime.now(timezone.utc).isoformat()))
        if live_review:
            db.execute("""
                INSERT INTO live_review_approvals VALUES (?, ?, ?)
                ON CONFLICT(snapshot_id, proposal_id) DO NOTHING
            """, (snapshot_id, proposal_id, datetime.now(timezone.utc).isoformat()))


def save_resolution(snapshot_id, proposal_id, *, reason, operation, target_id="", field="", value="",
                    details=None, ownership_confirmed=False, path=None):
    from review_plans import resolve
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT MAX(id) FROM snapshots").fetchone()[0] != snapshot_id:
            raise ValueError("Refresh the current review before resolving a finding.")
        row = db.execute("SELECT evidence FROM proposals WHERE snapshot_id=? AND id=?",
                         (snapshot_id, proposal_id)).fetchone()
        inventory = db.execute("SELECT records FROM snapshot_accounts WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if not row or not inventory:
            raise ValueError("Refresh to save the complete CRM evidence before resolving a finding.")
        item = resolve(json.loads(row[0]), json.loads(inventory[0]), reason=reason, operation=operation,
                       target_id=target_id, field=field, value=value, details=details,
                       ownership_confirmed=ownership_confirmed)
        db.execute("INSERT OR IGNORE INTO proposals VALUES (?,?,?)", (snapshot_id, item["id"], json.dumps(item)))
        db.execute("INSERT OR IGNORE INTO review_resolutions VALUES (?,?,?,?)",
                   (item["id"], item["review_resolution"]["source_key"], json.dumps(item),
                    datetime.now(timezone.utc).isoformat()))
        db.execute("INSERT INTO review_events(proposal_id,snapshot_id,state,reason,recorded_at) VALUES (?,?,?,?,?)",
                   (proposal_id, snapshot_id, "unresolved", f"Resolved into {item['id']}: {reason.strip()}",
                    datetime.now(timezone.utc).isoformat()))
        return item["id"]


def save_chow_version(snapshot_id, proposal_id, path=None):
    """Append a pending version, preserving the approved source and all its history."""
    from review_plans import version_chow_proposal
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT p.evidence FROM proposals p JOIN approvals a ON a.proposal_id=p.id
                            WHERE p.snapshot_id=? AND p.id=? AND a.state='approved'
                              AND p.snapshot_id=(SELECT MAX(id) FROM snapshots)""",
                         (snapshot_id, proposal_id)).fetchone()
        if not row or db.execute("SELECT 1 FROM executions WHERE proposal_id=?", (proposal_id,)).fetchone():
            raise ValueError("Versioning requires a current approved proposal with no execution attempt.")
        item = version_chow_proposal(json.loads(row[0]))
        db.execute("INSERT OR IGNORE INTO proposals VALUES (?,?,?)", (snapshot_id, item["id"], json.dumps(item)))
        db.execute("INSERT OR IGNORE INTO review_resolutions VALUES (?,?,?,?)",
                   (item["id"], item["review_resolution"]["source_key"], json.dumps(item),
                    datetime.now(timezone.utc).isoformat()))
        return item["id"]


def overlay_resolutions(db, proposals, accounts):
    """Carry forward a reasoned plan only while its original business evidence agrees."""
    from reconcile import CRM_BUSINESS_FIELDS
    by_id = {row["account_id"]: row for row in accounts}
    resolutions = {}
    for key, evidence in db.execute("SELECT source_key,evidence FROM review_resolutions ORDER BY recorded_at"):
        resolutions[key] = json.loads(evidence)
    result = []
    for source in proposals:
        resolved = resolutions.get(business_id(source))
        if resolved:
            records = resolved["crm"] + resolved["review_resolution"]["related"]
            def facts(row):
                return {key: row.get(key, "") for key in CRM_BUSINESS_FIELDS}
            if all(row["account_id"] in by_id and facts(row) == facts(by_id[row["account_id"]])
                   for row in records):
                resolved["website"] = source["website"]
                resolved["crm"] = [by_id[row["account_id"]] for row in resolved["crm"]]
                resolved["review_resolution"]["related"] = [
                    by_id[row["account_id"]] for row in resolved["review_resolution"]["related"]
                ]
                result.append(resolved)
                continue
        result.append(source)
    return result
