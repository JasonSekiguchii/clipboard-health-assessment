"""Streamlit review and controlled CRM correction interface."""

from collections import Counter
import os
import sqlite3

import streamlit as st

from crm_write import ACCOUNT_PATH, CRMWriteClient, WriteBlocked, live_canary_armed
from execution import apply_approved, approve_and_apply_findlay, reconcile_live_canary
from live_crm import CANARY_ID, CANARY_NEW_NAME, CANARY_OLD_NAME, LiveAuthorization, LiveCRMTransport, is_ashland_canary
from reconcile import (
    BELLHAVEN, BELLHAVEN_PARENT_NAME, FINDLAY_ID, build_proposals, is_actionable,
    is_findlay_direct_candidate,
)
from sources import BASE_URL, SourceError, read_accounts, scrape_facilities
from storage import latest_snapshot, save_review, save_snapshot
from resolution_ui import generic_apply_controls, resolution_controls, tiffin_chow_attestation
from reviewed_crm import BATAVIA_PROPOSAL_ID, tiffin_contracts_enabled

def close_findlay_dialog():
    st.session_state.pop("findlay_dialog", None)


@st.dialog("Confirm Findlay parent correction", on_dismiss=close_findlay_dialog)
def findlay_confirmation(snapshot_id, proposal_id, reviewed, attempt):
    st.text(
        f"Account ID: {FINDLAY_ID}\nExisting parent: (blank)\n"
        f"Proposed parent: {BELLHAVEN_PARENT_NAME} · {BELLHAVEN}\n"
        f"Lifetime revenue: {reviewed['lifetime_revenue']:,}\nOutstanding AR: {reviewed['outstanding_ar']:,}"
    )
    st.code(f"PATCH {ACCOUNT_PATH}/{FINDLAY_ID}", language="text")
    st.json({"parent_id": BELLHAVEN})
    st.caption("Legal ownership history was not independently verified.")
    key = f"findlay_dialog_{snapshot_id}_{proposal_id}_{attempt}"
    accepted = st.checkbox(
        "I accept the Bellhaven homepage, facility listing, and exact CRM name and address match "
        "as sufficient evidence for this fictional assessment’s Findlay parent correction.",
        key=f"{key}_accepted",
    )
    typed = st.text_input("Type APPLY to confirm", key=f"{key}_typed")
    if st.button("Cancel", key=f"{key}_cancel"):
        close_findlay_dialog()
        st.rerun()
    if st.button("Confirm & Apply", key=f"{key}_confirm",
                 disabled=not (accepted and typed == "APPLY" and live_canary_armed(FINDLAY_ID))):
        try:
            with st.spinner("Verifying Findlay, applying once, and reading back…"):
                approve_and_apply_findlay(snapshot_id, proposal_id, reviewed, accepted=accepted, typed=typed)
            close_findlay_dialog()
            st.rerun()
        except (WriteBlocked, ValueError, sqlite3.Error) as error:
            st.error(str(error) if not isinstance(error, sqlite3.Error) else "Could not save execution state.")


st.set_page_config(page_title="Bellhaven CRM review", layout="wide")
st.title("Bellhaven CRM review")
test_mode = "CLIPBOARD_REVIEW_DB_PATH" in os.environ
if test_mode:
    st.warning("TEST MODE — NO CRM WRITES")
    st.info("Approvals are recorded in the QA database.")
else:
    st.info("LIVE REVIEW — Scoped canary corrections. Local approval does not apply a CRM change.")
    batavia_enabled = (os.environ.get("CLIPBOARD_BATAVIA_CREATION") == BATAVIA_PROPOSAL_ID
                       and os.environ.get("CLIPBOARD_REVIEWED_WRITES") == "1"
                       and os.environ.get("CLIPBOARD_LIVE_WRITES") == "1")
    tiffin_enabled = (tiffin_contracts_enabled() and os.environ.get("CLIPBOARD_REVIEWED_WRITES") == "1"
                     and os.environ.get("CLIPBOARD_LIVE_WRITES") == "1")
    if not (live_canary_armed() or live_canary_armed(FINDLAY_ID) or batavia_enabled or tiffin_enabled):
        st.caption("Live Apply is disabled. Review and local approval are available.")

if st.button("Refresh from website and CRM", type="primary"):
    close_findlay_dialog()
    try:
        with st.spinner("Reading public facilities and CRM accounts…"):
            facilities = scrape_facilities()
            accounts = read_accounts()
            proposals = build_proposals(facilities, accounts)
            save_snapshot(facilities, len(accounts), proposals, accounts=accounts)
    except (SourceError, ValueError) as error:
        st.error(str(error))
    except sqlite3.Error:
        st.error("Could not save the local snapshot.")

try:
    snapshot = latest_snapshot()
except sqlite3.Error:
    st.error("Could not open the local review database.")
    st.stop()

if not snapshot:
    st.write("Refresh to collect the first complete website and CRM snapshot.")
    st.stop()

canary_execution = snapshot.get("canary_execution") if not test_mode else None
findlay_execution = snapshot.get("findlay_execution") if not test_mode else None
labels = {"applying": "Applying", "applied": "Applied and verified",
          "needs_investigation": "Needs investigation", "failed": "Failed"}
for label, recorded in (("Ashland", canary_execution), ("Findlay", findlay_execution)):
    if not recorded:
        continue
    st.write(f"{label} execution · {labels[recorded['state']]}")
    st.caption(recorded["message"])
    if recorded["phase"] == "patch_intent" and recorded["state"] != "applied":
        if st.button(f"Verify recorded {label} attempt by GET"):
            try:
                reconcile_live_canary(CRMWriteClient(LiveCRMTransport()), recorded["proposal_id"])
                st.rerun()
            except (WriteBlocked, sqlite3.Error) as error:
                st.error(str(error) if isinstance(error, WriteBlocked) else "Could not save verification.")

proposals = snapshot["proposals"]
st.caption(f"Latest complete snapshot · {snapshot['collected_at']}")
cols = st.columns(3)
cols[0].metric("Website facilities", snapshot["facility_count"])
cols[1].metric("CRM accounts checked", snapshot["account_count"])
cols[2].metric("Proposals", len(proposals))

counts = Counter(p["category"] for p in proposals)
if not counts:
    st.success("No proposals for this snapshot.")
    st.stop()

category = st.selectbox("Category", ["All", *sorted(counts)], format_func=lambda value:
                        value if value == "All" else f"{value} ({counts[value]})")
status = st.selectbox("Review state", ["Pending", "Approved", "Rejected", "Unresolved", "All"])
visible = {
    p["id"]: p for p in proposals
    if category in ("All", p["category"]) and status in ("All", p["review_state"].title())
}
if not visible:
    st.write("No proposals match these filters.")
    st.stop()


def title(item):
    website = item["website"]
    if website:
        return website["name"]
    account_id = item["proposed_change"]["account_id"]
    return next(row["name"] for row in item["crm"] if row["account_id"] == account_id)


selected_id = st.selectbox(
    "Proposal", list(visible), format_func=lambda key: f"{title(visible[key])} · {visible[key]['category']}",
)
item = visible[selected_id]
st.subheader(title(item))
st.caption(f"{item['category']} · {item['confidence']} · {item['id']}")
st.write(item["reason"])

website_column, crm_column = st.columns([1, 2])
with website_column:
    st.subheader("Website evidence")
    facility = item["website"]
    if facility:
        st.text(f"{facility['street']}\n{facility['city']}, {facility['state']} {facility['zip']}")
        st.write("Website affiliation: Bellhaven Senior Living")
        st.write(", ".join(facility["care_offerings"]))
        st.link_button("View source page", facility["source_url"])
        st.caption(f"Observed {facility['observation_date']}")
    else:
        st.write("No supported website listing for this CRM account.")

with crm_column:
    st.subheader("CRM evidence and recorded parent")
    if item["crm"]:
        st.dataframe(item["crm"], hide_index=True, column_order=(
            "name", "account_id", "parent_name", "parent_id", "billing_street", "billing_city",
            "billing_state", "billing_zip", "status", "lifetime_revenue", "outstanding_ar",
            "duplicate_of_account", "chow_current_account",
        ))
        st.caption("Revenue and AR are shown in the units returned by CRM.")
    else:
        st.write("No candidate CRM rows.")

st.subheader("Proposed change")
plan = item["proposed_change"]
canary = not test_mode and is_ashland_canary(item)
findlay = is_findlay_direct_candidate(item)
findlay_attestation = (
    "I accept the Bellhaven homepage, facility listing, and exact CRM name and address match "
    "as sufficient evidence for this fictional assessment’s Findlay parent correction."
)
if findlay:
    record = item["crm"][0]
    st.subheader("Findlay direct parent correction")
    st.text(
        f"Account ID: {FINDLAY_ID}\nCurrent parent: (blank)\n"
        f"Proposed parent: {BELLHAVEN_PARENT_NAME} · {BELLHAVEN}\n"
        f"Lifetime revenue: {record['lifetime_revenue']:,}\nOutstanding AR: {record['outstanding_ar']:,}"
    )
    st.link_button("View Bellhaven homepage affiliation", BASE_URL + "/")
    st.caption("Legal ownership history was not independently verified.")
    st.code(f"PATCH {ACCOUNT_PATH}/{FINDLAY_ID}", language="text")
    st.json({"parent_id": BELLHAVEN})
    if not live_canary_armed(FINDLAY_ID):
        st.warning("DRY RUN — Findlay live Apply is disabled.")
if plan.get("strategy") == "preserve_create_and_link":
    st.write("Preserve the existing account and its parent. Create a separate account and link it from the existing account.")
    st.write("Proposed replacement account data")
    st.json(plan["new_account"])
    st.write("Proposed change to the existing account")
    st.json(plan["existing_account_changes"])
else:
    st.json(plan)

state = item["review_state"]
if state == "approved":
    execution_state = item.get("execution_state")
    if execution_state == "applied":
        st.success("Applied and verified")
    elif execution_state:
        labels = {"applying": "Applying", "failed": "Failed",
                  "needs_investigation": "Needs investigation"}
        st.warning(f"{labels[execution_state]} · {item['execution_message']}")
    else:
        st.success(f"Approved locally · Not applied to CRM · {item['reviewed_at']}")
elif state in ("rejected", "unresolved"):
    st.info(f"{state.title()} locally · {item['reviewed_at'] or 'recorded'}")
elif not (findlay and not test_mode):
    actionable = is_actionable(item)
    st.write("Pending review" if actionable else "Pending investigation")
    if item["previous_disposition"]:
        st.caption(f"Earlier preview disposition: {item['previous_disposition']}")

    with st.form(f"review_{snapshot['id']}_{item['id']}"):
        confirmed = False
        if actionable and item["proposed_change"].get("strategy") in {"reparent_existing", "preserve_create_and_link"}:
            confirmed = st.checkbox(
                findlay_attestation if findlay else
                (tiffin_chow_attestation(item) or
                 "I verified the legal operator and correct CRM parent using evidence beyond the website address, "
                 "and reviewed the proposed account data.")
            )
        approve = st.form_submit_button(
            "Approve locally for live review" if canary else
            "Approve locally for Findlay review" if findlay and not test_mode else "Approve",
            disabled=not actionable,
        )
        decision_reason = st.text_area("Review reason", key=f"reason_{snapshot['id']}_{item['id']}")
        reject = st.form_submit_button("Reject")
        unresolved = st.form_submit_button("Keep unresolved")
        if approve or reject or unresolved:
            try:
                save_review(snapshot["id"], item["id"], "approved" if approve else "rejected" if reject else "unresolved",
                            ownership_confirmed=confirmed, live_review=approve and (canary or (findlay and not test_mode)),
                            reason=decision_reason)
                st.rerun()
            except ValueError as error:
                st.error(str(error))
            except sqlite3.Error:
                st.error("Could not save the local review decision.")


if canary and state == "approved" and not canary_execution:
    st.subheader("Ashland live Apply confirmation")
    st.text(f"Account ID: {CANARY_ID}\nCurrent name: {CANARY_OLD_NAME}\nProposed name: {CANARY_NEW_NAME}")
    st.code(f"PATCH {ACCOUNT_PATH}/{CANARY_ID}", language="text")
    st.json({"name": CANARY_NEW_NAME})
    st.caption("The API publishes the PATCH route. Acceptance of this JSON body is unverified.")
    if not item["live_reviewed_at"]:
        if st.button("Approve locally for this live review"):
            try:
                save_review(snapshot["id"], item["id"], "approved", live_review=True)
                st.rerun()
            except (ValueError, sqlite3.Error) as error:
                st.error(str(error) if isinstance(error, ValueError) else "Could not save the local approval.")
    else:
        st.caption(f"Fresh local live review approval · {item['live_reviewed_at']}")
    gate_open = live_canary_armed() and bool(item["live_reviewed_at"])
    key = f"ashland_{snapshot['id']}_{item['id']}"
    enabled = st.checkbox("Enable this single Ashland PATCH", key=f"{key}_enable", disabled=not gate_open)
    phrase = f"APPLY {item['id']} {CANARY_ID}"
    typed = st.text_input(f"Type {phrase}", key=f"{key}_phrase", disabled=not gate_open or not enabled)
    if st.button("Apply Ashland name change to CRM", disabled=not (gate_open and enabled and typed == phrase)):
        try:
            permit = LiveAuthorization.confirm(item["id"], item["crm"][0], typed)
            client = CRMWriteClient(LiveCRMTransport(permit))
            with st.spinner("Verifying baseline, applying once, and reading back…"):
                result = apply_approved(client, item["id"], confirmation=item["id"])
                if result["state"] == "needs_investigation" and result.get("phase") == "patch_intent":
                    reconcile_live_canary(CRMWriteClient(LiveCRMTransport()), item["id"])
            st.rerun()
        except (WriteBlocked, sqlite3.Error) as error:
            st.error(str(error) if isinstance(error, WriteBlocked) else "Could not save execution state.")


if findlay and not test_mode and state in ("pending", "approved") and not findlay_execution:
    if st.button("Approve & Apply", disabled=not live_canary_armed(FINDLAY_ID)):
        attempt = st.session_state.get("findlay_dialog_attempt", 0) + 1
        st.session_state["findlay_dialog_attempt"] = attempt
        st.session_state["findlay_dialog"] = (snapshot["id"], item["id"], item["crm"][0], attempt)
    if state == "pending" and st.button("Reject"):
        try:
            save_review(snapshot["id"], item["id"], "rejected")
            st.rerun()
        except (ValueError, sqlite3.Error) as error:
            st.error(str(error) if isinstance(error, ValueError) else "Could not save rejection.")


opened = st.session_state.get("findlay_dialog")
if opened:
    if (findlay and not test_mode and state in ("pending", "approved") and not findlay_execution
            and opened[:2] == (snapshot["id"], item["id"])):
        findlay_confirmation(*opened)
    else:
        close_findlay_dialog()


if item.get("decision_reason"):
    st.caption(f"Recorded review reason: {item['decision_reason']}")
if not canary and not findlay:
    resolution_controls(snapshot, item)
    generic_apply_controls(snapshot, item, test_mode=test_mode)
with st.expander("Execution history"):
    st.dataframe(snapshot.get("execution_history", []), hide_index=True)
    if not test_mode:
        for execution in snapshot.get("execution_history", []):
            if (execution["mode"] == "live" and execution["state"] == "needs_investigation"
                    and execution["phase"] in ("patch_intent", "created")
                    and execution["target_id"] not in (CANARY_ID, FINDLAY_ID)):
                if st.button(f"Verify by GET · {execution['proposal_id']}"):
                    try:
                        reconcile_live_canary(CRMWriteClient(LiveCRMTransport()), execution["proposal_id"])
                        st.rerun()
                    except (WriteBlocked, sqlite3.Error) as error:
                        st.error(str(error))
