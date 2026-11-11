"""Daily GET-only collection and reconciliation. Never approves or executes."""

import argparse
from pathlib import Path

from reconcile import build_proposals
from sources import SourceError, read_accounts, scrape_facilities
from storage import save_snapshot


def run_pipeline(path=None, *, scrape=scrape_facilities, read=read_accounts):
    facilities = scrape()
    accounts = read()
    proposals = build_proposals(facilities, accounts)
    snapshot_id = save_snapshot(facilities, len(accounts), proposals, path, accounts=accounts)
    return snapshot_id, len(facilities), len(accounts), len(proposals)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Local SQLite destination")
    args = parser.parse_args()
    try:
        snapshot, facilities, accounts, proposals = run_pipeline(args.database)
    except SourceError as error:
        parser.exit(1, f"{error}\n")
    print(f"Saved complete snapshot {snapshot}: {facilities} facilities, {accounts} accounts, {proposals} findings.")


if __name__ == "__main__":
    main()
