"""Read-only clients for the assessment site and CRM."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE_URL = "https://analyst-assessment-production.up.railway.app"
FINDLAY = "/communities/bellhaven-meadows-of-findlay"


class SourceError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(path, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        request = Request(BASE_URL + path, headers=headers, method="GET")
        with build_opener(NoRedirect).open(request, timeout=20) as response:
            return response.read().decode()
    except HTTPError as error:
        raise SourceError(f"GET {path} returned HTTP {error.code}.") from None
    except (URLError, OSError, ValueError):
        raise SourceError(f"GET {path} failed.") from None


def facility_links(page):
    return re.findall(r'href="(/communities/[a-z0-9-]+)"', page)


def clean(fragment):
    return " ".join(unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def parse_facility(body, path, observed):
    name = re.search(r"<h1>(.*?)</h1>", body, re.S)
    fields = dict(re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", body, re.S))
    address = re.split(r"<br\s*/?>", fields.get("Address", ""), flags=re.I)
    locality = (
        re.fullmatch(r"(.+), ([A-Z]{2}) (\d{5}(?:-\d{4})?)", clean(address[1]))
        if len(address) == 2 else None
    )
    care = [
        clean(badge) for badge in re.findall(
            r'<span class="badge">(.*?)</span>', fields.get("Care Offerings", ""), re.S
        )
    ]
    if not name or not clean(name[1]) or not locality or not clean(address[0]) or not care or not all(care):
        raise SourceError(f"Missing facility details at {path}.")
    city, state, zip_code = locality.groups()
    return {
        "name": clean(name[1]), "street": clean(address[0]), "city": city,
        "state": state, "zip": zip_code, "care_offerings": care,
        "source_url": BASE_URL + path, "observation_date": observed,
    }


def scrape_facilities(fetch=get):
    observed = datetime.now(timezone.utc).date().isoformat()
    homepage_paths = set(facility_links(fetch("/")))
    if FINDLAY not in homepage_paths:
        raise SourceError("The expected Findlay listing is missing from the homepage.")
    directory_paths = []
    expected = None
    page = 1
    while True:
        path = "/communities" if page == 1 else f"/communities?page={page}"
        body = fetch(path)
        summary = re.search(r"Page (\d+) of (\d+)\s*·\s*(\d+) communities listed", body)
        if not summary:
            raise SourceError(f"Missing directory pagination at {path}.")
        current, pages, count = map(int, summary.groups())
        if current != page or pages < page or (expected and expected != (pages, count)):
            raise SourceError(f"Inconsistent directory pagination at {path}.")
        expected = (pages, count)
        links = facility_links(body)
        if not links:
            raise SourceError(f"No facility links at {path}.")
        directory_paths.extend(links)
        if page == pages:
            break
        page += 1

    if len(directory_paths) != count or len(set(directory_paths)) != count:
        raise SourceError("Directory links do not match the advertised unique count.")
    paths = sorted(set(directory_paths) | homepage_paths)
    with ThreadPoolExecutor(max_workers=8) as pool:
        bodies = list(pool.map(fetch, paths))
    return [parse_facility(body, path, observed) for path, body in zip(paths, bodies)]


CRM_FIELDS = (
    "account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city",
    "billing_state", "billing_zip", "care_type", "status", "lifetime_revenue",
    "outstanding_ar", "duplicate_of_account", "chow_current_account",
    "phone", "note", "created_by_candidate", "updated_at",
)
MONEY_FIELDS = {"lifetime_revenue", "outstanding_ar"}
CRM_FIELD_TYPES = {
    **dict.fromkeys(CRM_FIELDS, str),
    **dict.fromkeys(MONEY_FIELDS, int),
    "created_by_candidate": bool,
}


def read_accounts(token=None, fetch=get):
    token = (os.environ.get("CLIPBOARD_API_TOKEN", "") if token is None else token).strip()
    if not token or any(char.isspace() for char in token):
        raise SourceError("Set CLIPBOARD_API_TOKEN to a valid Codespaces secret.")
    page = 1
    page_size = 50
    accounts = {}
    expected_total = None
    while True:
        body = fetch(f"/api/v1/accounts?page={page}&page_size={page_size}", token)
        try:
            payload = json.loads(body)
            rows, total = payload["data"], payload["total"]
            expected_rows = min(page_size, max(0, total - (page - 1) * page_size))
            if (
                type(total) is not int or total < 0 or payload["page"] != page
                or payload["page_size"] != page_size or not isinstance(rows, list)
                or len(rows) != expected_rows
                or (expected_total is not None and total != expected_total)
            ):
                raise ValueError
            for row in rows:
                record = {field: row[field] for field in CRM_FIELDS}
                for field, value in record.items():
                    if type(value) is not CRM_FIELD_TYPES[field]:
                        raise ValueError
                account_id = record["account_id"]
                if not account_id or account_id in accounts:
                    raise ValueError
                accounts[account_id] = record
        except (KeyError, TypeError, ValueError):
            raise SourceError(f"Invalid or incomplete CRM data on page {page}.") from None
        if len(accounts) == total:
            return list(accounts.values())
        expected_total = total
        page += 1
