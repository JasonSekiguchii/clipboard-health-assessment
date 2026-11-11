"""Review an explicit correction without changing matching confidence."""

import json
import os
import sqlite3

import streamlit as st

from crm_write import CRMWriteClient, WriteBlocked, valid_id
from execution import apply_approved, reviewed_plan
from reconcile import BELLHAVEN, is_actionable
from reviewed_crm import ReviewedAuthorization, ReviewedCRMTransport, TIFFIN_PROPOSAL_ID
from storage import save_resolution, save_review


def tiffin_chow_attestation(item):
    if (item.get("proposed_change", {}).get("strategy") == "preserve_create_and_link"
            and any(row.get("account_id") == "001U6RW32TY0WSXZZB" for row in item.get("crm", []))):
        return (
            "I accept the Bellhaven facility listing, exact CRM name and address match, and financial records "
            "as sufficient evidence for this fictional assessment's Tiffin parent correction and CHOW plan. "
            "Historical legal ownership was not independently verified. I reviewed the proposed account data."
        )
    return None


def resolution_controls(snapshot, item):
    with st.expander("Record evidence and prepare a specific correction"):
        if not snapshot.get("accounts"):
            st.write("Refresh to obtain the complete CRM snapshot before resolving this finding.")
            return
        records = snapshot["accounts"]
        by_id = {row["account_id"]: row for row in records}
        operation = st.selectbox(
            "Operation", ["update", "create"], key=f"operation_{snapshot['id']}_{item['id']}",
        )
        # These are local to this render. Hidden update widgets cannot supply a
        # target or field when a create form is submitted.
        target, field, value = "", "", ""
        details = None
        with st.form(f"resolve_{snapshot['id']}_{item['id']}"):
            if operation == "update":
                ids = list(by_id)
                default = item["crm"][0]["account_id"] if item["crm"] else ids[0]
                target = st.selectbox("Existing account", ids, index=ids.index(default),
                                      format_func=lambda key: f"{by_id[key]['name']} · {key}")
                field = st.selectbox("Approved field", [
                    "name", "parent_id", "status", "note", "duplicate_of_account", "chow_current_account",
                ])
                value = st.text_input("Proposed value for update")
                st.caption("For parent changes, enter the existing target parent ID. The financial SOP selects direct update or CHOW.")
            else:
                st.caption("Creation details use the local review model. The real POST required fields remain unverified.")
                details = st.text_area("Complete account details for creation (JSON)", value="{}")
            reason = st.text_area("Resolution reason and supporting evidence")
            confirmed = st.checkbox(tiffin_chow_attestation(item) or
                                    "I confirmed the facility identity and proposed ownership/billing parent.")
            if st.form_submit_button("Save resolved proposal for review"):
                try:
                    save_resolution(
                        snapshot["id"], item["id"], reason=reason, operation=operation,
                        target_id=target, field=field, value=value,
                        details=json.loads(details) if operation == "create" else None,
                        ownership_confirmed=confirmed,
                    )
                    st.rerun()
                except (ValueError, WriteBlocked) as error:
                    st.error(str(error))
                except sqlite3.Error:
                    st.error("Could not save the resolution.")


@st.dialog("Confirm reviewed CRM correction")
def reviewed_confirmation(snapshot_id, item, created_id=None):
    st.write(item["reason"])
    st.json(item["proposed_change"])
    if item["proposed_change"].get("strategy") == "preserve_create_and_link":
        st.code("POST /api/v1/accounts\nPATCH /api/v1/accounts/" +
                item["proposed_change"]["existing_account_id"], language="text")
        st.warning("First-use CHOW contract test: the complete POST payload and CHOW link PATCH are unverified. "
                   "Preserve the old parent and financial history. Stop on any uncertain outcome; do not repeat creation.")
        if created_id:
            st.caption(f"Saved new account ID: {created_id}. POST will not repeat; verify this account and resume linking only.")
    if item["proposed_change"].get("strategy") == "create_account":
        st.warning("First-use POST contract test: API payload acceptance and server defaults are unverified. "
                   "An uncertain result must be investigated; do not repeat creation.")
    reason = st.text_area("Supporting evidence for execution", value=item.get("decision_reason", ""))
    accepted = st.checkbox("I reviewed the account evidence and accept this exact change.")
    typed = st.text_input("Type APPLY")
    if st.button("Cancel reviewed correction"):
        st.rerun()
    if st.button("Confirm reviewed correction", disabled=not (accepted and typed == "APPLY" and reason.strip())):
        try:
            expected, changes, creation = reviewed_plan(item, item["ownership_confirmed"])
            permit = ReviewedAuthorization.confirm(item["id"], expected, changes, creation,
                                                    accepted=accepted, typed=typed)
            client = CRMWriteClient(ReviewedCRMTransport(permit))
            client.authorize(item["id"], item["id"])
            save_review(snapshot_id, item["id"], "approved", live_review=True,
                        ownership_confirmed=item["ownership_confirmed"], reason=reason, general_review=True)
            apply_approved(client, item["id"], confirmation=item["id"], required_snapshot_id=snapshot_id)
            st.rerun()
        except (ValueError, WriteBlocked, sqlite3.Error) as error:
            st.error(str(error) if not isinstance(error, sqlite3.Error) else "Could not save execution state.")


def generic_apply_controls(snapshot, item, *, test_mode):
    if test_mode or not is_actionable(item) or item["review_state"] != "approved":
        return
    transport = ReviewedCRMTransport()
    recorded = next((row for row in snapshot.get("execution_history", [])
                     if row["mode"] == "live" and row["proposal_id"] == item["id"]), None)
    resumable = bool(item["id"] == TIFFIN_PROPOSAL_ID and recorded
                     and recorded["phase"] == "created" and valid_id(recorded["created_account_id"])
                     and recorded["state"] in {"failed", "needs_investigation"})
    try:
        expected, changes, creation = reviewed_plan(item, item["ownership_confirmed"])
        enabled = (os.environ.get("CLIPBOARD_LIVE_WRITES") == "1"
                   and transport.supports_plan(expected, changes, creation, item["id"])
                   and not test_mode and (not item.get("execution_state") or resumable))
    except (ValueError, WriteBlocked):
        enabled = False
    if not enabled:
        st.caption("General CRM execution is disabled until the relevant API field contracts are verified.")
    st.json(item["proposed_change"])
    if st.button("Apply approved correction", disabled=not enabled):
        if resumable:
            reviewed_confirmation(snapshot["id"], item, recorded["created_account_id"])
        else:
            reviewed_confirmation(snapshot["id"], item)
