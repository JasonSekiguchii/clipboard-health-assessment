from collections import Counter
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reconcile import BELLHAVEN, build_proposals, parent_plan
from sources import BASE_URL, CRM_FIELD_TYPES, FINDLAY, SourceError, read_accounts, scrape_facilities
from storage import latest_snapshot, save_review, save_snapshot


def account(account_id, name="", street="", city="Town", state="OH", zip_code="01234",
            parent=BELLHAVEN, revenue=0, ar=0):
    return {
        "account_id": account_id, "name": name, "parent_id": parent,
        "billing_street": street, "billing_city": city, "billing_state": state,
        "billing_zip": zip_code, "lifetime_revenue": revenue, "outstanding_ar": ar,
    }


def facility(name="Bellhaven of Town", street="10 West Main Street", zip_code="01234"):
    return {
        "name": name, "street": street, "city": "Town", "state": "OH", "zip": zip_code,
        "source_url": BASE_URL + "/communities/town",
        "observation_date": "2026-11-10", "care_offerings": ["Memory Support"],
    }


def page(current, pages, count, *paths):
    return f"Page {current} of {pages} · {count} communities listed" + "".join(
        f'<a href="{path}">Site</a>' for path in paths
    )


class ScraperTests(unittest.TestCase):
    def setUp(self):
        detail = ('<h1>Memory &amp; Care</h1><dt>Address</dt><dd>10 W Main St<br>Town, OH 01234</dd>'
                  '<dt>Care Offerings</dt><dd><span class="badge">Memory Support</span></dd>')
        self.pages = {
            "/": f'<a href="{FINDLAY}">Findlay</a>',
            "/communities": page(1, 2, 2, "/communities/town"),
            "/communities?page=2": page(2, 2, 2, "/communities/other"),
            "/communities/town": detail, "/communities/other": detail, FINDLAY: detail,
        }

    def test_all_pages_and_homepage_only_listing(self):
        records = scrape_facilities(self.pages.__getitem__)
        self.assertEqual({row["source_url"] for row in records}, {
            BASE_URL + path for path in ("/communities/town", "/communities/other", FINDLAY)
        })
        self.assertEqual(records[0]["name"], "Memory & Care")
        self.assertEqual(records[0]["zip"], "01234")
        self.assertEqual(records[0]["care_offerings"], ["Memory Support"])

    def test_invalid_detail_rejects_snapshot(self):
        self.pages["/communities/other"] = "<h1>Missing address</h1>"
        with self.assertRaises(SourceError):
            scrape_facilities(self.pages.__getitem__)

    def test_duplicate_directory_links_reject_snapshot(self):
        self.pages["/communities?page=2"] = page(2, 2, 2, "/communities/town")
        with self.assertRaises(SourceError):
            scrape_facilities(self.pages.__getitem__)

    def test_missing_homepage_listing_rejects_snapshot(self):
        self.pages["/"] = "<h1>Homepage</h1>"
        with self.assertRaises(SourceError):
            scrape_facilities(self.pages.__getitem__)


class CRMTests(unittest.TestCase):
    def test_pagination_and_duplicate_rejection(self):
        row = {key: kind() for key, kind in CRM_FIELD_TYPES.items()}
        pages = [
            [dict(row, account_id=str(i)) for i in range(50)],
            [dict(row, account_id="50")],
        ]
        requested = []

        def fetch(path, _token):
            requested.append(path)
            number = len(requested)
            return json.dumps({"data": pages[number - 1], "total": 51, "page": number, "page_size": 50})

        self.assertEqual(len(read_accounts("fixture", fetch)), 51)
        self.assertEqual(requested, [f"/api/v1/accounts?page={p}&page_size=50" for p in (1, 2)])
        pages[1][0]["account_id"] = "0"
        requested.clear()
        with self.assertRaises(SourceError):
            read_accounts("fixture", fetch)


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.parent = account(BELLHAVEN, parent="")
        self.match = account("a", "Bellhaven of Town", "10 W Main St")

    def reconcile(self, *accounts):
        return build_proposals([facility()], [self.parent, *accounts])

    def test_normalized_match_across_parents_and_sop(self):
        rows = self.reconcile(dict(self.match, parent_id="other", lifetime_revenue=20, outstanding_ar=5))
        self.assertEqual([row["category"] for row in rows], ["Possible wrong parent"])
        plan = rows[0]["proposed_change"]
        self.assertEqual(plan["new_account"]["parent_id"], BELLHAVEN)
        self.assertEqual(plan["existing_account_changes"], {"chow_current_account": "<new account ID>"})
        for revenue, ar in ((20, 0), (0, 5), (0, 0)):
            direct = parent_plan(dict(self.match, lifetime_revenue=revenue, outstanding_ar=ar), BELLHAVEN)
            self.assertEqual(direct["existing_account_changes"], {"parent_id": BELLHAVEN})

    def test_shared_address_across_parents_requires_manual_review(self):
        other = dict(self.match, account_id="b", parent_id="other")
        rows = self.reconcile(self.match, other)
        self.assertEqual([row["category"] for row in rows], ["Ambiguous address overlap"])
        self.assertEqual(len(rows[0]["crm"]), 2)
        self.assertNotIn("existing_account_changes", rows[0]["proposed_change"])

    def test_same_parent_same_site_duplicate(self):
        rows = self.reconcile(self.match, dict(self.match, account_id="b"))
        self.assertEqual([row["category"] for row in rows], ["Duplicate candidate"])

    def test_name_alone_cannot_match(self):
        rows = self.reconcile(dict(self.match, billing_city="Other Town"))
        self.assertEqual(Counter(row["category"] for row in rows), {
            "Missing CRM account": 1, "Stale Bellhaven account": 1,
        })

    def test_zip_or_billing_street_discrepancy_does_not_mark_stale(self):
        for changes in ({"billing_zip": "01235"}, {"billing_street": "PO Box 10"}):
            rows = self.reconcile(dict(self.match, **changes))
            self.assertEqual([row["category"] for row in rows], ["Address discrepancy"])

    def test_ids_stable_across_order_and_observation_date(self):
        other = dict(self.match, account_id="b")
        original = self.reconcile(self.match, other)[0]
        later = dict(facility(), observation_date="2026-11-11")
        rerun = build_proposals([later], [other, self.match, self.parent])[0]
        self.assertEqual(original["id"], rerun["id"])
        changed = self.reconcile(dict(self.match, outstanding_ar=500), other)[0]
        self.assertNotEqual(original["id"], changed["id"])

    def test_distinct_stale_rows_at_same_address(self):
        rows = build_proposals([], [self.parent, self.match, dict(self.match, account_id="b")])
        self.assertEqual(len({row["id"] for row in rows}), 2)


class StorageTests(unittest.TestCase):
    def test_snapshot_and_local_decision_survive_identical_refresh(self):
        site = facility()
        rows = build_proposals([site], [account(BELLHAVEN, parent="")])
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "test.sqlite3"
            first = save_snapshot([site], 1, rows, path)
            save_review(first, rows[0]["id"], "rejected", path=path)
            second = save_snapshot([site], 1, rows, path)
            saved = latest_snapshot(path)
            self.assertEqual(saved["id"], second)
            self.assertEqual(saved["facility_count"], 1)
            self.assertEqual(saved["proposals"][0]["review_state"], "rejected")
