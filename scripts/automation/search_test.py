#!/usr/bin/env python3
"""Submit one synthetic search on each fully automatable broker.

Uses the stored public search form only. The query is a made-up person, not
someone in the catalog. Removal forms are not submitted. CAPTCHAs are not
solved. robots.txt does not block the test; --robots only notes a disallow. A page that prohibits automation is skipped
unless --include-prohibited is set.

The result log records the outcome, status, and path prefix. It does not
store the response, a people-search listing URL, or anyone found on the page.

Writes research/evidence/search-tests.jsonl and data/v4/search-tests.csv.
"""

from __future__ import annotations

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_scripts = _here if (_here / "bootstrap_path.py").exists() else _here.parent
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
import bootstrap_path

bootstrap_path.install()

import argparse
import csv
import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests

from catalog import ROOT
from map_brokers import MappingFetcher
from rescrape_fields import CHALLENGE
from v2_common import canonical_field

V4_PATH = ROOT / "data" / "v4" / "brokers.json"
LOG_PATH = ROOT / "research" / "evidence" / "search-tests.jsonl"
CSV_PATH = ROOT / "data" / "v4" / "search-tests.csv"

# Reserved fictional telephone range, plus example.com, which cannot be a real inbox.
VALUES = {
    "first_name": "Alder",
    "last_name": "Northwind",
    "middle_name": "Q",
    "full_name": "Alder Northwind",
    "phone": "555-010-0199",
    "email": "alder.northwind@example.com",
    "city": "Springfield",
    "region": "CA",
    "postal_code": "90210",
    "country": "US",
    "street_address": "100 Testing Street",
    "age": "40",
    "birth_year": "1980",
    "birth_date": "1980-01-01",
    "domain": "example.com",
}
QUERY_NAMES = {"q", "query", "search", "searchterm", "search_term", "keyword", "keywords", "name", "s_name"}
SKIP_TYPES = {"hidden", "submit", "button", "image", "reset", "file"}
PLACEHOLDER_OPTION = re.compile(r"^(please select\.{0,3}|select|choose|select state|select a state)$", re.I)
RESULT_PREFIXES = {"results", "result", "namesearch", "name_search"}
FALSE_RESULT_PREFIXES = {"hc", "help", "support", "404", "licenses"}
RETRY = {"http_error", "unfilled", "robots"}
_STATE_CODES = (
    ("districtofcolumbia", "DC"), ("northcarolina", "NC"), ("northdakota", "ND"),
    ("southcarolina", "SC"), ("southdakota", "SD"), ("westvirginia", "WV"),
    ("newhampshire", "NH"), ("newjersey", "NJ"), ("newmexico", "NM"), ("newyork", "NY"),
    ("rhodeisland", "RI"), ("massachusetts", "MA"), ("pennsylvania", "PA"),
    ("connecticut", "CT"), ("washington", "WA"), ("california", "CA"), ("wisconsin", "WI"),
    ("tennessee", "TN"), ("minnesota", "MN"), ("mississippi", "MS"), ("michigan", "MI"),
    ("louisiana", "LA"), ("kentucky", "KY"), ("oklahoma", "OK"), ("nebraska", "NE"),
    ("maryland", "MD"), ("colorado", "CO"), ("delaware", "DE"), ("arkansas", "AR"),
    ("alabama", "AL"), ("alaska", "AK"), ("arizona", "AZ"), ("florida", "FL"),
    ("georgia", "GA"), ("hawaii", "HI"), ("idaho", "ID"), ("illinois", "IL"),
    ("indiana", "IN"), ("iowa", "IA"), ("kansas", "KS"), ("maine", "ME"),
    ("missouri", "MO"), ("montana", "MT"), ("nevada", "NV"), ("ohio", "OH"),
    ("oregon", "OR"), ("texas", "TX"), ("utah", "UT"), ("vermont", "VT"),
    ("virginia", "VA"), ("wyoming", "WY"),
)
NO_RESULTS = (
    "no results", "no records", "0 results", "did not match", "couldn't find", "could not find",
    "no matches", "no people found", "we found 0", "0 people",
)
RESULTS = (
    "search results", "results found", "we found", "possible matches", "records found",
    "people found", "matching records", "view details",
)
COUNT_RE = re.compile(r"\b(\d{1,5})\s+(?:results|records|matches|people)\b", re.I)
DEEP = {"submitted", "results", "no_results"}
_BROWSER = threading.Semaphore(2)
_AJAX_LOCK = threading.Lock()
_AJAX_NEXT = 0.0
PROFILE_PREFIX = re.compile(r"^(people|person|persons|profiles?|names?|phones?|emails?|addresses|search-results?|background-check)$", re.I)
_REQUESTS = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_done(path: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("id"):
                done[item["id"]] = item
    return done


def polish(row: dict) -> dict:
    prefix = (row.get("path_prefix") or "").lower()
    outcome = row.get("outcome")
    changed = False
    if re.fullmatch(r"alder[-_ ]?northwind", prefix, re.I):
        row = dict(row)
        row["path_prefix"] = "lookup"
        prefix = "lookup"
        changed = True
    if row.get("page_kind") == "empty":
        if not changed:
            row = dict(row)
        row["outcome"] = "no_results"
        if row.get("result_count") is None:
            row["result_count"] = 0
        outcome = "no_results"
        changed = True
    if (
        row.get("status") == 200
        and outcome == "submitted"
        and prefix in RESULT_PREFIXES
        and row.get("page_kind") in {None, "results"}
    ):
        if not changed:
            row = dict(row)
        row["outcome"] = "results"
    elif outcome == "results" and prefix in FALSE_RESULT_PREFIXES:
        if not changed:
            row = dict(row)
        row["outcome"] = "submitted"
    return row


def latest_rows(rows: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for row in rows:
        if row.get("id"):
            by_id[row["id"]] = polish(row)
    return list(by_id.values())


def targets(include_prohibited: bool, only: set[str] | None, searchable: bool = False) -> list[dict]:
    payload = json.loads(V4_PATH.read_text(encoding="utf-8"))
    rows = []
    for item in payload["brokers"].values():
        automation = item["automation"]
        if searchable:
            if not (item.get("search") or {}).get("available"):
                continue
        elif not automation.get("fully_automatable"):
            continue
        if only and item["id"] not in only:
            continue
        legal = item.get("legal") or {}
        prohibited = bool(legal.get("automation_prohibited") or legal.get("automation_clause"))
        if prohibited and not include_prohibited:
            continue
        rows.append(item)
    rows.sort(key=lambda item: item["id"])
    return rows


def field_canon(field: dict) -> str | None:
    canon = field.get("canonical")
    if canon in VALUES or canon in {"state", "zip"}:
        return "region" if canon == "state" else "postal_code" if canon == "zip" else canon
    kind = (field.get("type") or "").lower()
    if kind == "tel":
        return "phone"
    if kind == "email":
        return "email"
    if kind == "search":
        return "full_name"
    name = (field.get("name") or "").strip().lower()
    if name in QUERY_NAMES:
        return "full_name"
    label = re.sub(r"[*:]", "", field.get("label") or "").strip().lower()
    if label in {"name", "full name", "person name"}:
        return "full_name"
    if label in {"location", "city state", "city, state"}:
        return "city"
    if label in {"domain", "website"} or name in {"domain", "website"}:
        return "domain"
    guessed = canonical_field(" ".join(filter(None, [field.get("name"), field.get("label"), field.get("placeholder"), kind])))
    if guessed == "state":
        return "region"
    if guessed == "zip":
        return "postal_code"
    return guessed if guessed in VALUES else None


def usable_options(field: dict) -> list[str]:
    return [
        option for option in (field.get("options") or [])
        if option and not PLACEHOLDER_OPTION.match(option.strip())
    ]


def pick_option(field: dict, wanted: str | None) -> str | None:
    options = usable_options(field)
    stored = (field.get("value") or "").strip()
    if stored and not PLACEHOLDER_OPTION.match(stored):
        for option in options:
            if option.lower() == stored.lower():
                return option
        if not options:
            return stored
    if not options:
        return wanted or None
    if wanted:
        folded = wanted.lower()
        aliases = {folded}
        if folded in {"ca", "california"}:
            aliases |= {"ca", "california", "us-ca"}
        if folded in {"us", "usa", "united states"}:
            aliases |= {"us", "usa", "united states", "united states of america"}
        for option in options:
            if option.lower() in aliases:
                return option
    if field.get("required") or field.get("value"):
        return options[0]
    return None


def pick_radio(fields: list[dict]) -> str | None:
    for field in fields:
        blob = f"{field.get('value') or ''} {field.get('label') or ''}".lower()
        if re.search(r"\ball\b|\bany\b", blob) and field.get("value"):
            return field["value"]
    for field in fields:
        if field.get("value"):
            return field["value"]
    return "on" if any(field.get("required") for field in fields) else None


def build_query(record: dict) -> tuple[dict[str, str], list[str], list[str]]:
    """Return posted fields, the canonical names that were filled, and required names that were not."""
    groups: dict[str, list[dict]] = {}
    for field in record["search"].get("inputs") or []:
        kind = (field.get("type") or "").lower()
        name = (field.get("name") or "").strip()
        if not name or kind in SKIP_TYPES:
            continue
        if field.get("disabled") and kind in {"checkbox", "radio"}:
            continue
        groups.setdefault(name, []).append(field)
    data: dict[str, str] = {}
    sent: list[str] = []
    missing: list[str] = []
    for name, fields in groups.items():
        kind = (fields[0].get("type") or "").lower()
        required = any(field.get("required") for field in fields)
        if kind == "radio":
            chosen = pick_radio(fields)
            if chosen:
                data[name] = chosen[:80]
                sent.append(field_canon(fields[0]) or name)
            elif required:
                missing.append(name)
            continue
        if kind == "checkbox":
            if not required:
                continue
            value = next((field.get("value") for field in fields if field.get("value")), None) or "on"
            data[name] = str(value)[:80]
            sent.append(field_canon(fields[0]) or name)
            continue
        field = fields[0]
        canon = field_canon(field)
        if kind == "select":
            chosen = pick_option(field, VALUES.get(canon or ""))
            if chosen:
                data[name] = chosen[:80]
                sent.append(canon or name)
            elif required:
                missing.append(canon or name)
            continue
        if canon and VALUES.get(canon):
            data[name] = VALUES[canon]
            sent.append(canon)
        elif required:
            missing.append(canon or name)
    return data, sent, missing


def state_from_domain(domain: str) -> str | None:
    host = (domain or "").lower().split(".", 1)[0]
    for name, code in _STATE_CODES:
        if host.startswith(name):
            return code
    return None


def stored_hidden(record: dict) -> dict[str, str]:
    """Hidden controls whose value is already known. Tokens read from the live page are added later."""
    data: dict[str, str] = {}
    for field in record["search"].get("inputs") or []:
        if (field.get("type") or "").lower() != "hidden":
            continue
        name = (field.get("name") or "").strip()
        value = (field.get("value") or "").strip()
        if name and value and "@" not in value and len(value) <= 40:
            data[name] = value
    names = (record["search"].get("form_meta") or {}).get("hidden_names") or []
    if "state" in names and "state" not in data:
        code = state_from_domain(record.get("domain") or "")
        if code:
            data["state"] = code
    return data


def hidden_from_html(html: str, names: set[str], near: set[str] | None = None) -> dict[str, str]:
    found: dict[str, str] = {}
    scopes = re.findall(r"<form\b[^>]*>.*?</form>", html or "", re.I | re.S)
    if near:
        ranked = []
        for form in scopes:
            score = sum(
                1 for name in near
                if re.search(rf"""name\s*=\s*["']{re.escape(name)}["']""", form, re.I)
            )
            if score:
                ranked.append((score, form))
        if ranked:
            scopes = [max(ranked, key=lambda item: item[0])[1]]
    blob = scopes[0] if scopes else (html or "")
    for tag in re.findall(r"<input\b[^>]*>", blob, re.I):
        if not re.search(r"type\s*=\s*[\"']hidden[\"']", tag, re.I):
            continue
        name_match = re.search(r"""name\s*=\s*["']([^"']+)["']""", tag, re.I)
        value_match = re.search(r"""value\s*=\s*["']([^"']*)["']""", tag, re.I)
        if not name_match:
            continue
        name = name_match.group(1)
        if name not in names or name in found:
            continue
        found[name] = (value_match.group(1) if value_match else "")[:200]
    return found


def path_prefix(url: str | None) -> str | None:
    if not url:
        return None
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return ""
    for part in parts[:3]:
        if part.lower() in RESULT_PREFIXES:
            return part[:40]
    prefix = parts[0][:40]
    if re.fullmatch(r"alder[-_ ]?northwind", prefix, re.I):
        return "lookup"
    if PROFILE_PREFIX.match(prefix):
        return prefix
    return prefix


PATH_SLOT = re.compile(r"\{([a-z_]+)\}")


def pattern_search_url(record: dict, data: dict[str, str]) -> tuple[str, dict[str, str]] | None:
    """A results URL the page builds in script, with canonical names in the path.

    `url_pattern` such as https://site/find/{first_name}-{last_name} comes from the rendered page
    (research/manual/v4-search-forms.json). Query-string patterns are already the form's own GET.
    """
    pattern = (record.get("search") or {}).get("url_pattern") or ""
    path = urlparse(pattern).path
    slots = PATH_SLOT.findall(path)
    if not slots:
        return None
    values: dict[str, str] = {}
    for field in record["search"].get("inputs") or []:
        canon = field_canon(field)
        name = (field.get("name") or "").strip()
        if canon and name in data:
            values[canon] = data[name]
            values[name] = data[name]
    filled = path
    for slot in slots:
        value = values.get(slot) or VALUES.get(slot) or ""
        if not value and slot in {"city", "region"}:
            value = VALUES[slot]
        if not value:
            return None
        filled = filled.replace("{" + slot + "}", re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-"))
    parsed = urlparse(pattern)
    return f"{parsed.scheme}://{parsed.netloc}{filled}", {}


def direct_search_url(url: str, data: dict[str, str]) -> tuple[str, dict[str, str]] | None:
    """411's form posts to /search, which 404s. The site reads /person-search/Name/ST.

    Sync.me ignores a query on the homepage. The lookup page is /search/?number=.
    """
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host == "sync.me":
        digits = re.sub(r"\D", "", data.get("nationalNumber") or data.get("phone") or "")
        if len(digits) >= 7:
            return f"https://sync.me/search/?number={digits}", {}
    path = urlparse(url).path.rstrip("/")
    if not path.endswith("/search") or "s_name" not in data:
        return None
    name = re.sub(r"[^A-Za-z0-9]+", "-", data["s_name"]).strip("-")
    if not name:
        return None
    loc = data.get("s_loc") or ""
    state = "CA"
    if "," in loc:
        state = re.sub(r"[^A-Za-z]", "", loc.split(",")[-1])[:2].upper() or "CA"
    parsed = urlparse(url)
    target = f"{parsed.scheme}://{parsed.netloc}/person-search/{name}/{state}"
    return target, {}


def classify(status: int | None, text: str, final_url: str | None, requested: str | None = None) -> str:
    blob = text.lower()[:2500]
    if status in (401, 403, 429, 503) or any(marker in blob for marker in CHALLENGE):
        return "challenge"
    if status == 404:
        asked = urlparse(requested or "").path.rstrip("/")
        landed = urlparse(final_url or "").path.rstrip("/")
        if landed and asked and landed != asked:
            return "no_results"
        return "http_error"
    if status is None or status >= 400:
        return "http_error"
    if any(marker in blob for marker in NO_RESULTS):
        return "no_results"
    host = urlparse(final_url or "").netloc.lower().removeprefix("www.")
    if host == "sync.me" and "we found" in (text or "").lower():
        return "results"
    prefix = (path_prefix(final_url) or "").lower()
    if prefix in RESULT_PREFIXES or PROFILE_PREFIX.match(prefix):
        return "results"
    if prefix in FALSE_RESULT_PREFIXES:
        return "submitted"
    if any(marker in blob for marker in RESULTS):
        return "results"
    return "submitted"


def stated_count(text: str) -> int | None:
    match = COUNT_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1))


def describe(outcome: str | None, text: str, final_url: str | None) -> tuple[str | None, int | None]:
    """What the response page is, and how many records it stated. No record text is kept."""
    if outcome in {None, "challenge", "captcha", "http_error", "error", "robots", "unfilled"}:
        return None, None
    if outcome == "no_results":
        return "empty", 0
    count = stated_count(text)
    low = (text or "").lower()
    if "ajaxrequesturl" in low:
        return "loader", None
    listed = listing_count(text or "")
    if listed:
        return "results", count if count is not None else listed
    if outcome == "results" and empty_results_shell(text or ""):
        return "empty", 0
    if outcome == "results":
        return "results", count
    path = (path_prefix(final_url) or "").lower()
    if path in {"loading", "loader"} or "searching" in low[:2500]:
        return "loader", None
    if re.search(r"<form\b", text or "", re.I):
        return "form", None
    return "other", count


def records_count(payload: object) -> int | None:
    """Length of a records list. None when the endpoint says it is rate-limiting."""
    if not isinstance(payload, dict):
        return None
    inner = payload.get("response")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            inner = None
    for source in (inner, payload):
        if not isinstance(source, dict):
            continue
        error = str(source.get("error") or "")
        if "too many" in error.lower():
            return None
        for key in ("records", "rows"):
            rows = source.get(key)
            if isinstance(rows, list):
                return min(len(rows), 10000)
            if isinstance(rows, int) and not isinstance(rows, bool) and rows >= 0:
                return min(rows, 10000)
    return None


def listing_count(html: str) -> int:
    cards = len(re.findall(
        r'class="[^"]*(?:result-item|result-card|person-card|profile-card|listing-card)[^"]*"',
        html or "",
        re.I,
    ))
    rows = re.findall(r"<tr\b[^>]*>.*?</tr>", html or "", re.I | re.S)
    data_rows = sum(1 for row in rows if re.search(r"<td\b", row, re.I))
    return min(max(cards, data_rows), 10000)


def empty_results_shell(html: str) -> bool:
    """A results URL whose main content is only the word results, with no listing markup."""
    match = re.search(r"<(article|main)\b[^>]*>(.*?)</\1>", html or "", re.I | re.S)
    if not match or listing_count(html):
        return False
    chunk = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", match.group(2), flags=re.I | re.S)
    chunk = re.sub(r"<[^>]+>", " ", chunk)
    chunk = re.sub(r"\bresults?\b", " ", chunk, flags=re.I)
    chunk = re.sub(r"\s+", " ", chunk).strip()
    return len(chunk) < 40


def follow_data_url(session: requests.Session, page_url: str, html: str) -> tuple[str | None, int | None, str | None]:
    """InfoTracer loaders publish a same-host data URL and a results path. The count is kept; the query is not."""
    data_match = re.search(r"window\.ajaxDataUrl\s*=\s*'([^']+)'", html or "")
    if not data_match:
        return None, None, None
    ajax_url = urljoin(page_url, data_match.group(1))
    if urlparse(ajax_url).netloc != urlparse(page_url).netloc:
        return None, None, None
    redirect_match = re.search(r"window\.redirect\s*=\s*'([^']+)'", html or "")
    results_url = None
    if redirect_match:
        results_url = urljoin(page_url, redirect_match.group(1))
        if urlparse(results_url).netloc != urlparse(page_url).netloc:
            results_url = None
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": page_url,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        ),
    }
    key_match = re.search(r"window\.ajaxCsrfKey\s*=\s*'([^']+)'", html or "")
    value_match = re.search(r"window\.ajaxCsrfValue\s*=\s*'([^']*)'", html or "")
    if key_match and value_match and len(value_match.group(1)) <= 200:
        headers[key_match.group(1)] = value_match.group(1)
    global _AJAX_NEXT
    try:
        with _AJAX_LOCK:
            wait = _AJAX_NEXT - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _AJAX_NEXT = time.monotonic() + 3.0
            response = session.get(ajax_url, headers=headers, timeout=(8, 20), allow_redirects=True)
            raw = response.text[:4000]
            response.close()
    except requests.RequestException:
        return None, None, None
    try:
        count = records_count(json.loads(raw))
    except json.JSONDecodeError:
        return None, None, None
    if count is None:
        return None, None, None
    if count == 0:
        return "empty", 0, results_url
    return "results", count, results_url


def follow_loader(session: requests.Session, page_url: str, html: str) -> tuple[str | None, int | None, str | None]:
    """Court-record loaders post the same query to /search/loader/ and return a record count."""
    named = follow_data_url(session, page_url, html)
    if named[0] is not None:
        return named
    ajax_match = re.search(r'"ajaxRequestUrl"\s*:\s*"([^"]+)"', html or "")
    data_match = re.search(r'"ajaxRequestData"\s*:\s*(\{[^{}]*\})', html or "")
    results_match = re.search(r'"results"\s*:\s*"([^"]+)"', html or "")
    if not ajax_match or not data_match:
        return None, None, None
    try:
        data = json.loads(data_match.group(1).replace(r"\/", "/"))
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    payload = {
        key: value for key, value in data.items()
        if isinstance(value, str) and len(value) <= 80
    }
    ajax_url = urljoin(page_url, ajax_match.group(1).replace(r"\/", "/"))
    if urlparse(ajax_url).netloc != urlparse(page_url).netloc:
        return None, None, None
    results_url = None
    if results_match:
        results_url = urljoin(page_url, results_match.group(1).replace(r"\/", "/"))
        if urlparse(results_url).netloc != urlparse(page_url).netloc:
            results_url = None
    global _AJAX_NEXT
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": page_url,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        ),
    }
    raw = ""
    status = None
    for attempt in range(4):
        try:
            with _AJAX_LOCK:
                wait = _AJAX_NEXT - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                _AJAX_NEXT = time.monotonic() + 3.0
                response = session.post(
                    ajax_url,
                    data=payload,
                    headers=headers,
                    timeout=(8, 20),
                    allow_redirects=True,
                )
                raw = response.text[:4000]
                status = response.status_code
                response.close()
        except requests.RequestException:
            return None, None, None
        limited = status == 429 or "too many requests" in raw.lower()
        if not limited:
            break
        time.sleep(4 * (attempt + 1))
    else:
        return "loader", None, None
    try:
        count = records_count(json.loads(raw))
    except json.JSONDecodeError:
        count = None
    if count is None:
        if "too many requests" in raw.lower():
            return "loader", None, None
        return None, None, None
    if count == 0:
        return "empty", 0, results_url
    return "results", count, results_url


def browser_facts(page_url: str, data: dict[str, str]) -> tuple[int | None, str | None, str]:
    """Submit the same synthetic fields in a browser when the HTTP response never left the form."""
    from playwright.sync_api import sync_playwright

    with _BROWSER:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                opened = page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
                anchor = None
                for name, value in data.items():
                    field = page.locator(f'[name="{name}"]')
                    try:
                        if not field.count():
                            continue
                        field.first.fill(value, timeout=2000)
                        anchor = field.first
                    except Exception:
                        continue
                select = page.locator("select[name='state'], select[name='region']")
                if select.count():
                    try:
                        select.first.select_option("CA")
                    except Exception:
                        pass
                form = anchor.locator("xpath=ancestor::form[1]") if anchor is not None else page.locator("form").first
                button = form.locator("button[type=submit], input[type=submit]").first
                navigated = None
                try:
                    if button.count():
                        button.click(timeout=3000)
                    elif form.count():
                        form.evaluate("form => form.requestSubmit ? form.requestSubmit() : form.submit()")
                    page.wait_for_timeout(6000)
                    navigated = page.url.rstrip("/") != page_url.rstrip("/")
                except Exception:
                    navigated = False
                text = page.content()[:60000]
                status = opened.status if opened is not None and not navigated else 200
                return status, page.url, text
            except Exception:
                return None, None, ""
            finally:
                browser.close()


def submit(fetcher: MappingFetcher, record: dict, robots_check=None) -> dict:
    search = record["search"]
    base = {
        "id": record["id"],
        "domain": record["domain"],
        "checked_at": utc_now(),
        "method": search.get("method"),
        "status": None,
        "path_prefix": None,
        "fields_sent": [],
        "missing": [],
        "page_kind": None,
        "result_count": None,
    }
    if search.get("captcha_provider"):
        base["outcome"] = "captcha"
        return base
    data, sent, missing = build_query(record)
    for name, value in stored_hidden(record).items():
        data.setdefault(name, value)
        if name not in sent:
            sent.append(name)
    base["fields_sent"] = sent
    base["missing"] = missing
    if missing or not data:
        base["outcome"] = "unfilled"
        return base
    url = search.get("form_action") or search.get("page_url")
    if not url:
        base["outcome"] = "unfilled"
        return base
    method = search.get("method")
    form_id = ((search.get("form_meta") or {}).get("id") or "").lower()
    if form_id == "goog-gt-votingform" or "translate.googleapis.com" in url.lower():
        base["outcome"] = "unfilled"
        base["missing"] = ["widget"]
        return base
    rewritten = pattern_search_url(record, data) or direct_search_url(url, data)
    if robots_check is not None:
        # Note only: a robots.txt disallow is recorded but does not stop the search.
        base["robots_disallowed"] = not robots_check((rewritten or (url, None))[0])
    if rewritten:
        url, data = rewritten
        method = "http_get"
    hidden_names = [
        name for name in ((search.get("form_meta") or {}).get("hidden_names") or [])
        if name not in data
    ]
    page = search.get("page_url")
    if hidden_names and page:
        fetcher.wait(urlparse(page).netloc)
        try:
            with _REQUESTS:
                peeked = fetcher.s.get(page, timeout=(8, 20), allow_redirects=True)
                html = peeked.text[:200_000]
                peeked.close()
            for name, value in hidden_from_html(html, set(hidden_names), set(data)).items():
                if value:
                    data[name] = value
        except requests.RequestException:
            pass
    fetcher.wait(urlparse(url).netloc)
    multipart = "multipart" in url.lower()
    try:
        with _REQUESTS:
            if method == "http_form_post" and multipart:
                response = fetcher.s.post(
                    url,
                    files={key: (None, value) for key, value in data.items()},
                    timeout=(8, 20),
                    allow_redirects=True,
                )
            elif method == "http_form_post":
                response = fetcher.s.post(url, data=data, timeout=(8, 20), allow_redirects=True)
            else:
                response = fetcher.s.get(url, params=data, timeout=(8, 20), allow_redirects=True)
            status = response.status_code
            final_url = response.url
            try:
                body = response.text[:60000]
            except Exception:
                body = ""
            response.close()
            low_body = body.lower()
            thin_loader = (
                status == 200
                and ("searching" in low_body[:2500] or "/loading" in (final_url or ""))
                and "ajaxdataurl" not in low_body
                and "ajaxrequesturl" not in low_body
            )
            if status >= 500 or thin_loader:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
                    )
                }
                if method == "http_form_post" and multipart:
                    response = fetcher.s.post(
                        url,
                        files={key: (None, value) for key, value in data.items()},
                        headers=headers,
                        timeout=(8, 20),
                        allow_redirects=True,
                    )
                elif method == "http_form_post":
                    response = fetcher.s.post(url, data=data, headers=headers, timeout=(8, 20), allow_redirects=True)
                else:
                    response = fetcher.s.get(url, params=data, headers=headers, timeout=(8, 20), allow_redirects=True)
                status = response.status_code
                final_url = response.url
                try:
                    body = response.text[:60000]
                except Exception:
                    body = ""
                response.close()
    except requests.RequestException:
        base["outcome"] = "error"
        return base
    outcome = classify(status, body, final_url, url)
    kind, count = describe(outcome, body, final_url)
    if kind == "loader":
        fetcher.wait(urlparse(final_url).netloc)
        try:
            with _REQUESTS:
                deep_kind, deep_count, results_url = follow_loader(fetcher.s, final_url, body)
        except requests.RequestException:
            deep_kind, deep_count, results_url = None, None, None
        if deep_kind in {"empty", "results"}:
            kind, count = deep_kind, deep_count
            if results_url:
                final_url = results_url
            outcome = "no_results" if kind == "empty" else "results"
        elif deep_kind == "loader":
            kind = "loader"
    landed = (path_prefix(final_url) or "").lower()
    skip_browser = landed in FALSE_RESULT_PREFIXES or landed in {"support", "submit-request", "personal-information"}
    if kind in {"loader", "form", "other"} and count is None and page and not skip_browser:
        b_status, b_url, b_text = browser_facts(page, data)
        if b_url:
            b_outcome = classify(b_status or status, b_text, b_url, url)
            b_kind, b_count = describe(b_outcome, b_text, b_url)
            if b_kind in {"results", "empty", "other"} or (b_kind == "loader" and kind != "loader"):
                kind, count = b_kind, b_count
                outcome = b_outcome
                final_url = b_url
                body = b_text
                if isinstance(b_status, int):
                    status = b_status
            if kind == "loader" and count is None and "ajaxrequesturl" in (b_text or "").lower():
                try:
                    with _REQUESTS:
                        deep_kind, deep_count, results_url = follow_loader(fetcher.s, b_url, b_text)
                except requests.RequestException:
                    deep_kind, deep_count, results_url = None, None, None
                if deep_kind in {"empty", "results"}:
                    kind, count = deep_kind, deep_count
                    if results_url:
                        final_url = results_url
                    outcome = "no_results" if kind == "empty" else "results"
    if kind == "empty":
        outcome = "no_results"
        if count is None:
            count = 0
    base["status"] = status
    base["path_prefix"] = path_prefix(final_url)
    base["page_kind"] = kind
    base["result_count"] = count
    base["outcome"] = outcome
    return base


def write_csv(rows: list[dict]) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id", "domain", "outcome", "status", "method", "path_prefix", "page_kind", "result_count", "fields_sent", "missing", "checked_at"]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in sorted(rows, key=lambda item: item["id"]):
            writer.writerow({
                "id": row["id"],
                "domain": row["domain"],
                "outcome": row["outcome"],
                "status": "" if row.get("status") is None else row["status"],
                "method": row.get("method") or "",
                "path_prefix": row.get("path_prefix") or "",
                "page_kind": row.get("page_kind") or "",
                "result_count": "" if row.get("result_count") is None else row.get("result_count"),
                "fields_sent": ";".join(row.get("fields_sent") or []),
                "missing": ";".join(row.get("missing") or []),
                "checked_at": row.get("checked_at") or "",
            })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", default="", help="Comma-separated catalog ids")
    parser.add_argument("--include-prohibited", action="store_true", help="Also search brokers whose terms prohibit automation")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent and do not request the form")
    parser.add_argument("--fresh", action="store_true", help="Delete this test log before running. Does not touch v4 brokers.")
    parser.add_argument("--deep", action="store_true", help="Follow loader pages and record a result count. Retries submitted and results rows.")
    parser.add_argument("--fix", action="store_true", help="Retry loaders, forms, and results pages that still have no count.")
    parser.add_argument("--searchable", action="store_true", help="Target every broker with a stored public search form, not only fully automatable ones. Use with --ids")
    parser.add_argument("--robots", action="store_true", help="Note whether robots.txt disallows the search URL (robots_disallowed). The search still runs")
    args = parser.parse_args()
    if args.fresh and LOG_PATH.exists():
        LOG_PATH.unlink()
    only = {item.strip() for item in args.ids.split(",") if item.strip()} or None
    rows = targets(args.include_prohibited, only, args.searchable)
    done = load_done(LOG_PATH)
    retry = set(RETRY)
    if args.deep:
        retry |= DEEP
    def needs_fix(record: dict) -> bool:
        last = done.get(record["id"])
        if not last:
            return True
        kind = last.get("page_kind")
        if kind in {"loader", "form"}:
            return True
        prefix = (last.get("path_prefix") or "").lower()
        sent = set(last.get("fields_sent") or [])
        named = bool(sent & {"full_name", "first_name", "last_name", "phone"})
        if kind == "other" and named and prefix not in FALSE_RESULT_PREFIXES | {"support", "submit-request", "personal-information", "personal", "your-privacy-choices"}:
            return True
        if kind == "results" and last.get("result_count") is None:
            return True
        return last.get("outcome") in {"http_error", "robots"}

    pending = [
        row for row in rows
        if (args.fix and needs_fix(row)) or (
            not args.fix and (
                row["id"] not in done or done[row["id"]].get("outcome") in retry or (
                    args.deep and done.get(row["id"], {}).get("page_kind") is None and done.get(row["id"], {}).get("outcome") not in {"unfilled", "challenge", "captcha"}
                )
            )
        )
    ]
    if args.limit:
        pending = pending[: args.limit]
    print(f"search tests: {len(pending)}", flush=True)
    if args.dry_run:
        for record in pending:
            data, sent, missing = build_query(record)
            for name, value in stored_hidden(record).items():
                data.setdefault(name, value)
            print(f"{record['id']} send={sent} missing={missing} keys={list(data)}", flush=True)
        return 0
    if not pending:
        saved = latest_rows(list(done.values()))
        write_csv(saved)
        print(json.dumps(Counter(row["outcome"] for row in saved), indent=2), flush=True)
        return 0
    fetcher = MappingFetcher()
    lock = threading.Lock()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def one(record: dict) -> dict:
        result = submit(fetcher, record, fetcher.allowed if args.robots else None)
        with lock:
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(f"{result['outcome']} {record['domain']}", flush=True)
        return result

    fresh_rows = []
    workers = 1 if args.fix else 4
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, record) for record in pending]
        for future in as_completed(futures):
            fresh_rows.append(future.result())
    saved = latest_rows(list(load_done(LOG_PATH).values()))
    write_csv(saved)
    print(json.dumps(Counter(row.get("outcome") for row in saved), indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
