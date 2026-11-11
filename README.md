# Clipboard Health assessment

A Python application that compares Bellhaven's public facility roster with Clipboard's assessment CRM, presents evidence for human review, and records decisions and execution checkpoints in SQLite. Streamlit provides the review screen. Collection is GET-only; a local approval never automatically updates CRM.

## Install and launch privately

The application was tested with Python 3.14 and Streamlit 1.67. Install the declared dependencies in a local virtual environment:

```bash
cd /workspaces/clipboard-health-assessment
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Configure **CLIPBOARD_API_TOKEN** as a Codespaces repository secret, or inject it through a secure process environment. Restart the Codespace if necessary. Do not paste credentials into source, command arguments, browser URLs, screenshots, or the review interface. The application reads the variable server-side for authenticated CRM requests. No `.env` loader is implemented.

Start with CRM writes explicitly disabled, including if the terminal previously inherited live-review settings:

```bash
CLIPBOARD_LIVE_WRITES=0 CLIPBOARD_REVIEWED_WRITES=0 \
  .venv/bin/python -m streamlit run app.py \
  --server.address 127.0.0.1 --server.port 8501 --server.headless true \
  --browser.gatherUsageStats false
```

Keep the Codespaces forwarded port **Private**. Stop an existing process normally before restarting on the same port. Select **Refresh from website and CRM** to obtain the first complete snapshot. Do not publish or deploy this authenticated review application.

For an isolated QA database on private port 8502:

```bash
CLIPBOARD_REVIEW_DB_PATH=data/local_qa.sqlite3 \
CLIPBOARD_LIVE_WRITES=0 CLIPBOARD_REVIEWED_WRITES=0 \
  .venv/bin/python -m streamlit run app.py \
  --server.address 127.0.0.1 --server.port 8502 --server.headless true \
  --browser.gatherUsageStats false
```

That opens **TEST MODE — NO CRM WRITES**, with independent decisions. Do not copy QA approvals into the assessment database. Default assessment storage is `data/review.sqlite3`; databases and secret files remain local and are Git-ignored. A fresh clone has no approval or execution history: refresh to create a snapshot. The included public roster is a reference artifact, not a replacement for a fresh scrape.

## Architecture and matching

| Module | Responsibility |
| --- | --- |
| `sources.py` | Fetch public pages and authenticated CRM GETs; validate complete pagination and observed response fields. |
| `reconcile.py`, `review_plans.py` | Normalize names/addresses, generate evidence-backed findings, and validate explicit reviewer resolutions and financial plans. |
| `storage.py` | SQLite snapshots, stable proposal IDs, decisions, review evidence, creation claims and durable execution checkpoints. |
| `app.py`, `resolution_ui.py` | Review evidence, record decisions, and request separate exact-plan execution confirmation. |
| `execution.py`, `crm_write.py` | Shared approval checks, stale-record protection, minimal updates, creation tracking and GET verification. |
| `live_crm.py`, `reviewed_crm.py`, `simulation.py` | Gated real adapter and fake CRM execution for tests/rehearsals. |
| `pipeline.py` | Read-only daily collection and reconciliation; saves snapshots without approving or executing changes. |

The scraper follows every directory page, includes the homepage-only Findlay listing, and retrieves every discovered detail page. Advertised counts, unique links and required facility fields must validate before accepting a snapshot. CRM accounts are read across all parents using `page_size=50`; incomplete or inconsistent pages fail collection. A failed scrape does not replace the last complete snapshot.

Matching compares normalized street, city, state, ZIP and name across all CRM parents. ZIPs remain strings; common street/name variants are normalized. Billing and physical addresses may differ. A shared address establishes a candidate site relationship, not legal ownership or duplicate identity. Ambiguous aliases, historical operators, competing records and website omissions remain findings for investigation.

## Human review and safe execution

Inspect website evidence, existing CRM fields, the proposed change, confidence and financial fields. **Approve**, **Reject**, or **Keep unresolved** records a local decision. An uncertain finding needs an explicit reasoned resolution before approval; resolving it creates a new review item. Stable business evidence preserves proposal IDs and decisions across daily runs. Material changes require new review.

Execution is a separate deliberate action. Where the live adapter permits the exact plan, the reviewer opens a confirmation dialog, reviews its complete payload and operations, accepts the evidence, and types **APPLY**. Startup, refresh, collection, local approval, rejection and environment settings alone do not mutate CRM. QA mode blocks live execution. Specialized historical canaries retain their own confirmation paths.

Immediately before mutation, the shared executor compares fresh CRM GETs with the reviewed baseline and validates related accounts. It commits request intent before dispatch, sends minimal approved PATCH fields, persists a returned creation ID, and verifies by GET. **Approved locally**, **Applying**, **Applied and verified**, **Failed**, and **Needs investigation** have distinct meanings. Applied requires verified read-back.

An uncertain POST never triggers a blind second creation. A known created ID is reused after a recoverable pre-dispatch/link failure. An uncertain PATCH intent blocks another live PATCH and requires GET-only investigation. Local SQLite locks/claims prevent this application's conflicting submissions; they do not guarantee server-side idempotency or exclude another client's concurrent writes.

### Financial CHOW rule

For a confirmed parent change:

- If **lifetime revenue > 0 AND outstanding AR > 0**, preserve the old account and parent, create a new account under the confirmed parent, then set **chow_current_account on the old account** to the verified new ID. Preserve all other old fields, including balances and status; a server-managed timestamp may advance.
- Otherwise, directly update the existing account's `parent_id`.

Tiffin version 2 uses the eight-field creation shape verified with Batavia: name, parent, billing street/city/state/ZIP, care type and status. Zero revenue, zero AR and blank outgoing CHOW/duplicate links are mandatory **GET verification conditions**, not POST initialization fields. Failure blocks linking. Both accounts are verified after linking. The earlier 12-field proposal and its approval remain historical; they do not authorize version 2.

The Tiffin attestation records deliberate acceptance of the website listing, exact CRM name/address match and financial evidence for this fictional assessment. Historical legal ownership was not independently verified. That limitation must remain explicit for other ownership reviews too.

## Verified assessment operations

Final independent reconciliation on **2026-11-11** verified:

| Facility | Verified result |
| --- | --- |
| Ashland | Name corrected to **Bellhaven Healthcare Centre of Ashland**; unrelated reviewed fields preserved. |
| Findlay | Existing account assigned to Bellhaven; financial values and unrelated fields preserved. |
| Batavia | New Bellhaven account created with the eight-field payload; zero financial defaults and blank links verified. |
| Tiffin | New Bellhaven account created with zero balances/blank outgoing links; historical Cedar Trail account retained its parent and finances and points to the new account through CHOW. |

All four execution journals were Applied and verified. Two consecutive GET-only runs returned **35 facilities, 123 CRM accounts and 23 findings**, with no completed correction re-proposed. Three saved rejections remained rejected; 20 findings remained pending.

These scoped successes establish observed behavior for those requests. They do not prove a complete general API write contract. Broad creation remains contract-gated; status, note and duplicate-link writes are mock-tested rather than live-verified. Exact live scopes and acknowledgments are enforced in code and disabled by default. This public handoff intentionally contains no instructions to enable live mutations.

## Read-only daily pipeline

From this repository:

```bash
.venv/bin/python pipeline.py
```

For a separate snapshot database:

```bash
.venv/bin/python pipeline.py --database data/daily_qa.sqlite3
```

The command collects public/CRM GETs, matches records and saves a complete local snapshot. It never approves or applies CRM changes, regardless of review-app write settings. Local SQLite writes are expected; CRM writes are absent.

Example cron entry on a **UTC-configured host**, at 07:15 UTC daily:

```cron
15 7 * * * cd /workspaces/clipboard-health-assessment && .venv/bin/python pipeline.py >> data/pipeline.log 2>&1
```

No scheduler is installed by this repository, and `crontab` was unavailable in the assessed Codespace. Actual recurrence requires a running host/scheduler and secure secret injection into the scheduled process; an interactive Codespaces secret is not automatically a cron environment. Configure timezone explicitly on another host. Keep credentials out of the crontab and logs. A stopped Codespace cannot run the job.

## Tests and known limits

Tests use fake/mock CRM responses and temporary databases; they do not require live mutation permission:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Coverage includes complete scraping, ambiguous matching, stable IDs/decisions, approval gates, both financial branches, creation absence checks, durable partial-failure recovery, stale evidence, preservation of unrelated fields, two-account CHOW/default verification, reruns and fresh-process Streamlit startup.

The final CRM remains an exception-managed result, not a claim that every record is corrected. Grove City has a supported display-name proposal awaiting a naming decision. Carlisle has no supported CRM match. Marietta/Lima parent relationships, Owosso canonical selection, other aliases/address overlaps, billing discrepancies and website-absent accounts still require evidence or an explicit retained/unresolved disposition. No closure or ownership transfer is inferred solely from an address or website omission.

The published API omits mutation request schemas. General field limits, rejection/transaction semantics, conditional updates, server idempotency and authoritative unknown-outcome lookup remain unverified. SQLite review history is local to its database, not shared automatically across users or machines. Real CRM snapshots and detailed internal assessment evidence are deliberately excluded from the public repository.

See [submission audit and publication manifest](docs/submission_audit.md) for packaging/security scope, final test results and remaining deliverable gaps.
