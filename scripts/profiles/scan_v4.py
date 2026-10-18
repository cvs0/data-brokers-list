#!/usr/bin/env python3
"""Build v4 broker profiles from the v3 records.

A privacy or removal mailbox on another domain is stored with confidence
related_domain, so the removal service can use it. Brokers whose loaded pages
published no form, email, phone, postal address, portal, or CAPTCHA are
crawled again: a few unfetched same-site opt-out and privacy URLs, then one
browser open of the catalog URL and the homepage when those pages still show
no form.

The channel recrawl honors robots.txt. A separate pass reads the brokers robots.txt had blocked. A search pass reads homepages whose public search form was missing, including pages robots.txt had disallowed, and stores a name, phone, or address form. Forms are not submitted. CAPTCHAs are not solved.
v3 files are left as they are.
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
import asyncio
import csv
import json
import random
import re
import socket
import ssl
import struct
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from jsonschema import Draft202012Validator

from catalog import ROOT, load_catalog
from map_brokers import MappingFetcher, classify_role, load_parents, norm_url, utc_now
from scan_v3 import (
    DATA_CATEGORIES,
    Doc,
    PROC_RE,
    RENDER_JS,
    _challenge,
    _form_has_email,
    _hard_error,
    analyze,
    captcha_of,
    captcha_widget,
    empty_captcha_detail,
    clause,
    is_search_form,
    option_label,
    search_inputs,
    search_pattern,
    signals_from_render,
)
from rescrape_fields import JURIS
from v2_common import canonical_field, processing_days

V3_PATH = ROOT / "data" / "v3" / "brokers.json"
MAPS_PATH = ROOT / "data" / "v1" / "broker-maps.json"
LOG_PATH = ROOT / "research" / "evidence" / "v4.jsonl"
SEARCH_FIELDS_PATH = ROOT / "research" / "manual" / "v4-search-fields.json"
SEARCH_FORMS_PATH = ROOT / "research" / "manual" / "v4-search-forms.json"
SEARCH_TEST_LOG = ROOT / "research" / "evidence" / "search-tests.jsonl"
OUT_PATH = ROOT / "data" / "v4" / "brokers.json"
CSV_PATH = ROOT / "data" / "v4" / "brokers.csv"
BLOCKERS_PATH = ROOT / "data" / "v4" / "removal-blockers.csv"
DOC_PATH = ROOT / "docs" / "v4.md"
SCHEMA_PATH = ROOT / "data" / "schema" / "broker-v4.schema.json"

PRIVACY_CORE = (
    "privacy", "optout", "opt-out", "opt_out", "dpo", "dataprotection", "data-protection",
    "ccpa", "cpra", "gdpr", "dsar", "dsr", "remove", "removal", "delete", "suppression", "datenschutz",
)
COMMON_PATHS = (
    "/do-not-sell-my-personal-information",
    "/do-not-sell",
    "/opt-out",
    "/optout",
    "/privacy-request",
    "/ccpa",
)
SKIP_BITS = ("/blog/", "/news/", ".pdf", "/wp-content/", "/tag/", "/category/", "/docs/")
LANG_PATH = re.compile(r"^/(?:cs|da|de|et|fr|es|ru|ro|zh|ja|pt|it|nl|pl|sv|fi|ko|ar)(/|$)")
LOOKUP = {"people-search", "background-checks", "public-records"}
SEARCH_PATHS = ("/people-search", "/search", "/people", "/find")
OPT_OUT_URL = re.compile(r"opt-?out|privacy|remove|donotsell|do-not-sell|suppress|dsar", re.I)
PERSON_SEARCH = re.compile(
    r"first name|last name|full name|enter a name|phone|address|city|people search|person search|background check|reverse phone|who lives|find people",
    re.I,
)
def core_privacy(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return any(key in local for key in PRIVACY_CORE)


# Generic inboxes the site itself tells people to use for a deletion, opt-out, or do-not-sell request.
STATED_REMOVAL = {
    "anymailfinder.com": ["legal@anymailfinder.com"],
    "consider.com": ["support@consider.com"],
    "getprospect.com": ["support@getprospect.com"],
    "salesintel.io": ["support@salesintel.io", "gary.holtz@salesintel.io"],
    "attribits.com": ["compliance@allgoodmediagroup.com"],
    "graze.social": ["legal@graze.social"],
    "heartbeat.ai": ["legal@heartbeat.ai"],
    "helixcampaigns.com": ["info@helixcampaigns.com"],
    "idatabasesolutions.com": ["compliance@idatabasesolutions.com"],
    "kbsynergy.com": ["compliance@kbsynergy.com"],
    "plug-industries.com": ["dataretrieval@plug-industries.com"],
    "postie.com": ["info@postie.com"],
    "reverseaustralia.com": ["residential@reverseaustralia.com", "corporate@reverseaustralia.com"],
    "thebump.com": ["help@thebump.com"],
    "melissa.com": ["consumerrequest@melissa.com"],
    "ileads.com": ["dnc@ileads.com"],
    "ripple-fx.com": ["roi@ripple-fx.com"],
    "traackr.com": ["legal@traackr.com", "datarequest@traackr.com"],
    "privacy.ca.gov": ["info@cppa.ca.gov"],
    "thisnumber.com": ["thisnumber.com@gmail.com"],
}


def promote_emails(record: dict) -> int:
    changed = 0
    for row in record["removal"]["emails"]:
        if row["confidence"] == "uncertain" and core_privacy(row["email"]):
            row["confidence"] = "related_domain"
            changed += 1
    stated = {email.lower() for email in STATED_REMOVAL.get(record["domain"], [])}
    for row in record["removal"]["emails"]:
        if row["email"].lower() in stated and row["confidence"] == "uncertain":
            row["confidence"] = "stated_removal"
            changed += 1
    if changed and "email" not in record["removal"]["channels"]:
        record["removal"]["channels"].append("email")
    return changed


def no_channel(record: dict) -> bool:
    removal = record["removal"]
    if removal["form_fields"] or removal["emails"] or removal["phones"] or removal["postal_address"] or removal["portal"]:
        return False
    if removal["captcha"]["provider"]:
        return False
    return record["liveness"] == "alive"


def seen_urls(record: dict) -> set[str]:
    found = set()
    for page in record.get("pages") or []:
        for key in ("url", "final_url"):
            if page.get(key):
                found.add(norm_url(page[key]))
    if record["removal"].get("url"):
        found.add(norm_url(record["removal"]["url"]))
    return found


def same_host(domain: str, url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def interesting(url: str) -> bool:
    path = urlparse(url).path.lower()
    if any(bit in path for bit in SKIP_BITS) or LANG_PATH.search(path):
        return False
    return any(token in path for token in ("opt", "privacy", "ccpa", "dsar", "do-not-sell", "donotsell", "remove", "contact", "request", "gdpr"))


def candidate_urls(domain: str, record: dict, site: dict) -> list[str]:
    seen = seen_urls(record)
    ranked: list[tuple[int, str]] = []
    pool: list[str] = []
    for page in site.get("pages") or []:
        if page.get("role") in {"privacy", "contact", "legal", "opt_out"}:
            pool.append(page.get("final_url") or page.get("url") or "")
    for link in site.get("link_map") or []:
        if link.get("role") in {"privacy", "contact", "opt_out", "legal"}:
            pool.append(link.get("url") or "")
    for loc in (site.get("sitemap") or {}).get("privacy_urls") or []:
        pool.append(loc)
    origin = f"https://{domain}"
    pool.extend(origin + path for path in COMMON_PATHS)
    pool.extend(f"https://www.{domain}" + path for path in COMMON_PATHS)
    for url in pool:
        if not url or not url.startswith("http") or not same_host(domain, url) or not interesting(url):
            continue
        if norm_url(url) in seen:
            continue
        role = classify_role(url, "")
        rank = {"opt_out": 0, "privacy": 1, "contact": 2, "legal": 3}.get(role, 4)
        if "/do-not-sell" in url.lower() or "opt" in url.lower():
            rank = 0
        ranked.append((rank, url))
    ranked.sort()
    chosen = []
    seen_norm = set()
    for _rank, url in ranked:
        key = norm_url(url)
        if key in seen_norm:
            continue
        seen_norm.add(key)
        chosen.append(url)
        if len(chosen) >= 4:
            break
    return chosen


def choose_removal_form(page: dict) -> dict | None:
    forms = page.get("forms") or []

    def labels_of(form: dict) -> str:
        return " ".join(f"{field.get('placeholder') or ''} {field.get('name') or ''}" for field in form.get("inputs") or [])

    def removalish(form: dict) -> bool:
        return _form_has_email(form) and bool(re.search(r"first|last|full name|name|address|phone", labels_of(form), re.I))

    removalish_present = any(removalish(form) for form in forms)
    page_url = page.get("final_url") or page.get("url") or ""
    text = page.get("text") or ""
    for form in forms:
        removal_page = bool(re.search(r"do not sell|opt-?out|delet|dsar|privacy request", f"{page_url} {text[:1500]}", re.I))
        if is_search_form(form, page_url, text) and not (_form_has_email(form) and removal_page):
            continue
        labels = labels_of(form)
        if removalish_present and not _form_has_email(form) and re.search(r"first|last|full name|phone|address|city|state", labels, re.I):
            continue
        if re.search(r"\bcookie\b", labels, re.I) and not _form_has_email(form):
            continue
        if re.search(r"company name", labels, re.I) and not _form_has_email(form):
            continue
        if any((field.get("type") or "").lower() == "password" for field in form.get("inputs") or []):
            continue
        names = {(field.get("name") or "").strip().lower() for field in form.get("inputs") or []}
        if "searchtype" in names and not _form_has_email(form):
            continue
        if names <= {"langmenu"} or "gaoptout" in page_url.lower():
            continue
        if names and names <= {"sl", "tl", "query", "gtrans", "vote", "hl", "u", "client"}:
            continue
        if names & {"offer_id", "aff_id", "gclid"} and not _form_has_email(form):
            continue
        useful = [
            field for field in form.get("inputs") or []
            if field.get("type") != "search" and (
                field.get("canonical") or field.get("type") in ("email", "text", "select", "textarea", "checkbox")
            )
        ]
        tokens = {(field.get("name") or field.get("placeholder") or field.get("canonical") or "").strip().lower() for field in useful}
        if tokens and tokens <= {"search", "q", "query", "s", "k", "search_term", "keyword"}:
            continue
        if not _form_has_email(form) and re.search(
            r"\bsearch\b|mailing list|find a city|enter a domain|search for a company|find a business|find answers|\bwhere\b|e\.g\.",
            labels,
            re.I,
        ):
            continue
        if re.search(r"builder type|book a demo|request a meeting", labels, re.I):
            continue
        if re.search(r"business email", labels, re.I) and re.search(r"industry|company", labels, re.I):
            continue
        if re.search(r"forgot your password", f"{labels} {text[:700]}", re.I) and len(useful) <= 2:
            continue
        if re.search(r"\bsender\b", labels, re.I) and re.search(r"\bto\b", labels, re.I):
            continue
        if not _form_has_email(form) and re.search(r"first|last", labels, re.I):
            head = text[:500].lower()
            if ("search now" in head or "search public records" in head) and not re.search(r"opt out|opt-out|remove my info|please enter", head):
                continue
        page_blob = f"{page_url} {text[:2000]}".lower()
        if not re.search(r"opt-?out|privacy|do-not-sell|donotsell|remove|delet|dsar|ccpa|suppress|gdpr|not-sell", page_blob):
            continue
        if useful:
            return _submission(page, form, useful[:40])
    return None


def pick_removal_form(page: dict) -> list[dict]:
    chosen = choose_removal_form(page)
    return chosen["fields"] if chosen else []


def _submission(page: dict, form: dict, fields: list[dict]) -> dict:
    page_url = page.get("final_url") or page.get("url") or ""
    action = (form.get("action") or "").strip()
    if action.lower().startswith("javascript:"):
        action = ""
    target = urljoin(page_url, action) if page_url else (action or None)
    if not target or not target.startswith("http"):
        target = None
    method = "http_form_post" if (form.get("method") or "").lower() == "post" else "http_get"
    return {"fields": fields, "action": target, "method": method if target else None}


def slim_page(page: dict) -> dict:
    return {
        "url": page.get("url"),
        "final_url": page.get("final_url"),
        "status": page.get("status"),
        "liveness": page.get("liveness") or "alive",
        "role": page.get("role") or "other",
        "title": (page.get("title") or "")[:140],
        "text": (page.get("text") or "")[:2000],
        "forms": page.get("forms") or [],
        "emails": page.get("emails") or [],
        "phones": page.get("phones") or [],
        "captcha_provider": page.get("captcha_provider"),
        "captcha_sitekey": page.get("captcha_sitekey"),
        "portal": page.get("portal"),
    }


def absorb(record: dict, page: dict) -> bool:
    changed = False
    fields = pick_removal_form(page)
    removal = record["removal"]
    if fields and not removal["form_fields"]:
        removal["form_fields"] = fields
        if "web_form" not in removal["channels"]:
            removal["channels"].append("web_form")
        removal["final_url"] = page.get("final_url") or page.get("url")
        changed = True
    have = {row["email"] for row in removal["emails"]}
    for row in page.get("emails") or []:
        email = row.get("email")
        if not email or email in have:
            continue
        confidence = "related_domain" if row.get("confidence") == "uncertain" and core_privacy(email) else row.get("confidence") or "uncertain"
        if confidence == "extracted" and core_privacy(email):
            confidence = "extracted"
        removal["emails"].append({
            "email": email,
            "confidence": confidence,
            "source_url": page.get("final_url") or page.get("url") or "",
        })
        have.add(email)
        if "email" not in removal["channels"]:
            removal["channels"].append("email")
        changed = True
    for phone in page.get("phones") or []:
        if phone and phone not in removal["phones"] and len(removal["phones"]) < 6:
            removal["phones"].append(phone)
            changed = True
    if removal["phones"] and "phone" not in removal["channels"]:
        removal["channels"].append("phone")
        changed = True
    if page.get("postal_address") and not removal["postal_address"]:
        removal["postal_address"] = page["postal_address"]
        if "postal_mail" not in removal["channels"]:
            removal["channels"].append("postal_mail")
        changed = True
    # A captcha pass that loaded this removal page already decided the widget.
    # An older page must not put that decision back.
    if record.get("_captcha_removal_checked") is not True:
        if not removal["captcha"]["provider"] and any(
            re.search(r"captcha", f"{field.get('name') or ''} {field.get('placeholder') or ''}", re.I)
            for field in fields
        ):
            removal["captcha"]["provider"] = "custom"
            page_url = page.get("final_url") or page.get("url")
            if page_url:
                removal["captcha"]["pages"] = [page_url]
            changed = True
        if page.get("captcha_provider") and not removal["captcha"]["provider"]:
            removal["captcha"]["provider"] = page["captcha_provider"]
            removal["captcha"]["sitekey"] = page.get("captcha_sitekey")
            page_url = page.get("final_url") or page.get("url")
            if page_url:
                removal["captcha"]["pages"] = [page_url]
            changed = True
    if page.get("portal") and not removal["portal"]:
        removal["portal"] = page["portal"]
        if "official_portal" not in removal["channels"]:
            removal["channels"].append("official_portal")
        changed = True
    if changed:
        record["pages"].append({
            "url": page.get("url") or "",
            "final_url": page.get("final_url"),
            "status": page.get("status"),
            "role": page.get("role") or "opt_out",
            "title": (page.get("title") or "")[:140],
            "liveness": page.get("liveness") or "alive",
        })
    return changed


def page_from_signals(url: str, final: str, status, signals: dict) -> dict:
    return {
        "url": url,
        "final_url": final,
        "status": status,
        "liveness": "alive",
        "role": "opt_out",
        "title": signals["title"],
        "text": signals["text"],
        "forms": signals["forms"],
        "emails": signals["emails"],
        "phones": signals["phones"],
        "captcha_provider": signals["captcha_provider"],
        "captcha_sitekey": signals["captcha_sitekey"],
        "portal": signals["portal"],
    }


def _host_path(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return f"{(parsed.hostname or '').lower()}{(parsed.path or '/').rstrip('/')}"


def search_captcha(record: dict) -> bool:
    search = record["search"]
    if search.get("captcha_provider"):
        return True
    page = _host_path(search.get("page_url") or "")
    if not page:
        return False
    return any(_host_path(url) == page for url in record["removal"]["captcha"]["pages"])


def apply_captcha(record: dict, update: dict) -> None:
    """Use a later read of the removal or search page. A loaded page with no widget clears a stored marker."""
    removal = update.get("removal") or {}
    if removal.get("seen"):
        captcha = record["removal"]["captcha"]
        captcha["checked_url"] = removal.get("page")
        if removal.get("provider"):
            captcha["provider"] = removal["provider"]
            captcha["sitekey"] = removal.get("sitekey")
            page = removal.get("page")
            if page:
                captcha["pages"] = [page]
            if isinstance(removal.get("detail"), dict):
                captcha["detail"] = removal["detail"]
        else:
            captcha["provider"] = None
            captcha["sitekey"] = None
            captcha["pages"] = []
    search = update.get("search") or {}
    if search.get("seen"):
        record["search"]["captcha_provider"] = search.get("provider")
        record["search"]["captcha_sitekey"] = search.get("sitekey") if search.get("provider") else None
        if search.get("provider") and isinstance(search.get("detail"), dict):
            record["search"]["captcha_detail"] = search["detail"]


def apply_captcha_detail(record: dict, update: dict) -> None:
    """Store widget settings from a later read. Does not clear a provider the page did not show this time."""
    removal = update.get("removal") or {}
    if removal.get("seen") and isinstance(removal.get("detail"), dict) and record["removal"]["captcha"].get("provider"):
        record["removal"]["captcha"]["detail"] = removal["detail"]
    search = update.get("search") or {}
    if search.get("seen") and isinstance(search.get("detail"), dict) and record["search"].get("captcha_provider"):
        record["search"]["captcha_detail"] = search["detail"]


def merge_captcha_detail(provider: str | None, stored: dict | None) -> dict | None:
    """Keep page-read settings, and fill only the facts the provider identity itself establishes."""
    if not provider:
        return None
    detail = empty_captcha_detail()
    if provider == "recaptcha_v3":
        detail["mode"] = "score"
        detail["invisible"] = True
        detail["enterprise"] = False
    elif provider == "recaptcha_v2":
        detail["enterprise"] = False
    elif provider == "recaptcha_enterprise":
        detail["enterprise"] = True
    if isinstance(stored, dict):
        for key in detail:
            if stored.get(key) is not None:
                detail[key] = stored[key]
    return detail


def merge_select_options(record: dict, update: dict) -> None:
    """Replace a stored dropdown when a later read of the same field has more choices."""
    if not update:
        return
    groups = (
        (record["search"].get("inputs") or [], update.get("search") or []),
        (record["removal"].get("form_fields") or [], update.get("removal") or []),
    )
    for current, newer in groups:
        for field in newer:
            options = [option for option in (field.get("options") or []) if option]
            if len(options) < 2:
                continue
            for stored in current:
                if stored.get("type") != "select" or stored.get("name") != field.get("name"):
                    continue
                if len(options) > len(stored.get("options") or []):
                    stored["options"] = options


def apply_search(record: dict, update: dict) -> None:
    current = record["search"]
    if current.get("available") is True or not update:
        return
    if update.get("available") and _usable_search(record["domain"], update):
        current["available"] = True
        current["method"] = update.get("method") or "http_get"
        current["page_url"] = update.get("page_url")
        current["form_action"] = update.get("form_action")
        current["inputs"] = update.get("inputs") or []
        current["url_pattern"] = update.get("url_pattern")
        if update.get("captcha_provider"):
            current["captcha_provider"] = update["captcha_provider"]
        return
    # A loaded page with no usable name, phone, or address form is a completed scan.
    if current.get("available") is None and update.get("page_url") and _real_page(update.get("page_url")):
        current["available"] = False
        current["method"] = "none"
        current["page_url"] = update["page_url"]


def load_done(path: Path) -> dict[str, dict]:
    done = {}
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
            domain = item.get("domain")
            if not domain:
                continue
            if item.get("pass") == "search":
                target = done.get(domain)
                if not target:
                    target = {"domain": domain, "pages": [], "checked_at": item.get("checked_at")}
                    done[domain] = target
                if item.get("search_update"):
                    target["search_update"] = item["search_update"]
                target["checked_at"] = item.get("checked_at") or target.get("checked_at")
                continue
            if item.get("pass") == "options":
                target = done.get(domain)
                if not target:
                    target = {"domain": domain, "pages": [], "checked_at": item.get("checked_at")}
                    done[domain] = target
                if item.get("option_update"):
                    target["option_update"] = item["option_update"]
                target["checked_at"] = item.get("checked_at") or target.get("checked_at")
                continue
            if item.get("pass") == "legal":
                target = done.get(domain)
                if not target:
                    target = {"domain": domain, "pages": [], "checked_at": item.get("checked_at")}
                    done[domain] = target
                target["legal_done"] = True
                if isinstance(item.get("legal_update"), dict):
                    target["legal_update"] = item["legal_update"]
                target["checked_at"] = item.get("checked_at") or target.get("checked_at")
                continue
            if item.get("pass") == "network":
                target = done.get(domain)
                if not target:
                    target = {"domain": domain, "pages": [], "checked_at": item.get("checked_at")}
                    done[domain] = target
                target["network_done"] = True
                if isinstance(item.get("network_update"), dict):
                    target["network_update"] = item["network_update"]
                target["checked_at"] = item.get("checked_at") or target.get("checked_at")
                continue
            if item.get("pass") == "network_extra":
                target = done.get(domain)
                if not target:
                    target = {"domain": domain, "pages": [], "checked_at": item.get("checked_at")}
                    done[domain] = target
                target["network_extra_done"] = True
                if item.get("rdap_retried"):
                    target["rdap_retried"] = True
                if item.get("rdap_retried_again"):
                    target["rdap_retried_again"] = True
                if isinstance(item.get("network_extra"), dict):
                    target["network_extra"] = item["network_extra"]
                target["checked_at"] = item.get("checked_at") or target.get("checked_at")
                continue
            if item.get("pass") == "captcha":
                target = done.get(domain)
                if not target:
                    target = {"domain": domain, "pages": [], "checked_at": item.get("checked_at")}
                    done[domain] = target
                target["captcha_done"] = True
                if item.get("captcha_keys_checked"):
                    target["captcha_keys_checked"] = True
                if isinstance(item.get("captcha_update"), dict):
                    target["captcha_update"] = item["captcha_update"]
                target["checked_at"] = item.get("checked_at") or target.get("checked_at")
                continue
            if item.get("pass") == "captcha_detail":
                target = done.get(domain)
                if not target:
                    target = {"domain": domain, "pages": [], "checked_at": item.get("checked_at")}
                    done[domain] = target
                target["captcha_detail_done"] = True
                if item.get("captcha_detail_rendered"):
                    target["captcha_detail_rendered"] = True
                if isinstance(item.get("captcha_detail"), dict):
                    target["captcha_detail"] = item["captcha_detail"]
                target["checked_at"] = item.get("checked_at") or target.get("checked_at")
                continue
            if item.get("pass") == "form_detail":
                target = done.get(domain)
                if not target:
                    target = {"domain": domain, "pages": [], "checked_at": item.get("checked_at")}
                    done[domain] = target
                target["form_detail_done"] = True
                if isinstance(item.get("form_detail"), dict):
                    target["form_detail"] = item["form_detail"]
                target["checked_at"] = item.get("checked_at") or target.get("checked_at")
                continue
            prev = done.get(domain)
            if not prev:
                done[domain] = item
                continue
            seen = set()
            for page in prev.get("pages") or []:
                seen.add(norm_url(page.get("final_url") or page.get("url") or ""))
            for page in item.get("pages") or []:
                key = norm_url(page.get("final_url") or page.get("url") or "")
                if key in seen:
                    continue
                prev.setdefault("pages", []).append(page)
                seen.add(key)
            if item.get("found_form"):
                prev["found_form"] = True
            prev["checked_at"] = item.get("checked_at") or prev.get("checked_at")
    return done


def base_records() -> dict[str, dict]:
    allowed = {row["domain"] for row in load_catalog()}
    payload = json.loads(V3_PATH.read_text(encoding="utf-8"))
    records = {}
    for key, item in payload["brokers"].items():
        if item.get("domain") not in allowed:
            continue
        record = json.loads(json.dumps(item))
        record["schema_version"] = "4.0.0"
        promote_emails(record)
        records[key] = record
    return records


def apply_log(records: dict[str, dict], raw: dict[str, dict]) -> None:
    by_domain: dict[str, list[dict]] = {}
    for record in records.values():
        by_domain.setdefault(record["domain"], []).append(record)
    for domain, item in raw.items():
        for record in by_domain.get(domain, []):
            if item.get("search_update"):
                apply_search(record, item["search_update"])
            if item.get("option_update"):
                merge_select_options(record, item["option_update"])
            if isinstance(item.get("legal_update"), dict):
                merge_legal(record, item["legal_update"])
            if isinstance(item.get("network_update"), dict):
                record["_network"] = item["network_update"]
            if isinstance(item.get("network_extra"), dict):
                record["_network_extra"] = item["network_extra"]
            if item.get("captcha_done"):
                update = item["captcha_update"] if isinstance(item.get("captcha_update"), dict) else {}
                if update:
                    apply_captcha(record, update)
                record["_captcha_removal_checked"] = bool((update.get("removal") or {}).get("seen"))
                if record["search"].get("available"):
                    record["_captcha_search_checked"] = bool((update.get("search") or {}).get("seen"))
            if isinstance(item.get("captcha_detail"), dict):
                apply_captcha_detail(record, item["captcha_detail"])
            if isinstance(item.get("form_detail"), dict):
                record["_form_detail"] = item["form_detail"]
            for page in item.get("pages") or []:
                absorb(record, page)


USABLE_EMAIL = {"extracted", "related_domain", "stated_removal"}
PERSON_FIELDS = {
    "first_name", "last_name", "full_name", "phone", "street_address", "email", "city", "state", "region",
    "zip", "postal_code", "country",
}
CPPA_PATH = ROOT / "research" / "registries" / "cppa-2026-10-11.csv"
_PLACE_NAMES = (
    ("District of Columbia", "US", "DC"),
    ("Washington DC", "US", "DC"),
    ("North Carolina", "US", "NC"),
    ("North Dakota", "US", "ND"),
    ("South Carolina", "US", "SC"),
    ("South Dakota", "US", "SD"),
    ("West Virginia", "US", "WV"),
    ("New Hampshire", "US", "NH"),
    ("New Jersey", "US", "NJ"),
    ("New Mexico", "US", "NM"),
    ("New York", "US", "NY"),
    ("Rhode Island", "US", "RI"),
    ("British Columbia", "CA", "BC"),
    ("New Brunswick", "CA", "NB"),
    ("Newfoundland", "CA", "NL"),
    ("Nova Scotia", "CA", "NS"),
    ("Prince Edward Island", "CA", "PE"),
    ("Alabama", "US", "AL"),
    ("Alaska", "US", "AK"),
    ("Arizona", "US", "AZ"),
    ("Arkansas", "US", "AR"),
    ("California", "US", "CA"),
    ("Colorado", "US", "CO"),
    ("Connecticut", "US", "CT"),
    ("Delaware", "US", "DE"),
    ("Florida", "US", "FL"),
    ("Georgia", "US", "GA"),
    ("Hawaii", "US", "HI"),
    ("Idaho", "US", "ID"),
    ("Illinois", "US", "IL"),
    ("Indiana", "US", "IN"),
    ("Iowa", "US", "IA"),
    ("Kansas", "US", "KS"),
    ("Kentucky", "US", "KY"),
    ("Louisiana", "US", "LA"),
    ("Maine", "US", "ME"),
    ("Maryland", "US", "MD"),
    ("Massachusetts", "US", "MA"),
    ("Michigan", "US", "MI"),
    ("Minnesota", "US", "MN"),
    ("Mississippi", "US", "MS"),
    ("Missouri", "US", "MO"),
    ("Montana", "US", "MT"),
    ("Nebraska", "US", "NE"),
    ("Nevada", "US", "NV"),
    ("Ohio", "US", "OH"),
    ("Oklahoma", "US", "OK"),
    ("Oregon", "US", "OR"),
    ("Pennsylvania", "US", "PA"),
    ("Tennessee", "US", "TN"),
    ("Texas", "US", "TX"),
    ("Utah", "US", "UT"),
    ("Vermont", "US", "VT"),
    ("Virginia", "US", "VA"),
    ("Washington", "US", "WA"),
    ("Wisconsin", "US", "WI"),
    ("Wyoming", "US", "WY"),
    ("Ontario", "CA", "ON"),
    ("Quebec", "CA", "QC"),
    ("Alberta", "CA", "AB"),
    ("Manitoba", "CA", "MB"),
    ("Saskatchewan", "CA", "SK"),
    ("Yukon", "CA", "YT"),
)
US_REGION_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM",
    "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}
CA_REGION_CODES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
_NAME_TO_REGION = {name.lower(): code for name, _country, code in _PLACE_NAMES}
_CPPA_INDEX: dict[str, dict] | None = None
_CPPA_NAMES: dict[str, dict] | None = None
_FOLDED_PLACES = sorted(
    ((re.sub(r"[^a-z]", "", name.lower()), country, code) for name, country, code in _PLACE_NAMES),
    key=lambda item: len(item[0]),
    reverse=True,
)
_CPPA_INDEX: dict[str, dict] | None = None


def _named_fields(fields: list[dict], allowed: set[str] | None = None) -> list[str]:
    names = []
    seen = set()
    for field in fields:
        name = field.get("canonical")
        if not name or name in seen:
            continue
        if allowed is not None and name not in allowed:
            continue
        names.append(name)
        seen.add(name)
    return names


def _fold_place(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _region_code(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    upper = cleaned.upper()
    if upper in US_REGION_CODES or upper in CA_REGION_CODES:
        return upper
    return _NAME_TO_REGION.get(cleaned.lower())


def _country_name(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value.strip().lower())
    return {
        "united states": "US",
        "united states of america": "US",
        "usa": "US",
        "us": "US",
        "u.s.": "US",
        "u.s.a.": "US",
        "canada": "CA",
        "ca": "CA",
        "united kingdom": "GB",
        "uk": "GB",
        "great britain": "GB",
        "gb": "GB",
        "australia": "AU",
        "au": "AU",
        "germany": "DE",
        "singapore": "SG",
        "israel": "IL",
        "new zealand": "NZ",
        "finland": "FI",
        "united arab emirates": "AE",
        "norway": "NO",
        "spain": "ES",
        "france": "FR",
        "sweden": "SE",
        "india": "IN",
        "poland": "PL",
    }.get(cleaned)


def _fold_org(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\b(llc|inc|incorporated|corp|corporation|ltd|limited)\b", " ", text)
    return re.sub(r"[^a-z0-9]", "", text)


def _cppa_index() -> dict[str, dict]:
    global _CPPA_INDEX, _CPPA_NAMES
    if _CPPA_INDEX is not None:
        return _CPPA_INDEX
    index: dict[str, dict] = {}
    names: dict[str, dict] = {}
    if CPPA_PATH.exists():
        with CPPA_PATH.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                website = (row.get("Data broker primary website:") or "").strip()
                filing = {
                    "street": (row.get("Data broker primary street address:") or "").strip() or None,
                    "locality": (row.get("Data broker city:") or "").strip() or None,
                    "postal_code": (row.get("Data broker zip code:") or "").strip() or None,
                    "country": _country_name(row.get("Data broker country:") or ""),
                }
                filing["region"] = _filing_region(row.get("Data broker state:") or "", filing["country"])
                if not any(filing.values()):
                    continue
                for raw in (row.get("Data broker name:"), row.get("Doing Business As (DBA), if applicable:")):
                    key = _fold_org(raw or "")
                    if len(key) >= 5:
                        names.setdefault(key, filing)
                if not website:
                    continue
                if "://" not in website:
                    website = "https://" + website
                host = (urlparse(website).hostname or "").lower()
                if host.startswith("www."):
                    host = host[4:]
                if not host:
                    continue
                hosts = [host]
                labels = host.split(".")
                if len(labels) > 2:
                    hosts.append(".".join(labels[-2:]))
                for key in hosts:
                    index.setdefault(key, filing)
    _CPPA_INDEX = index
    _CPPA_NAMES = names
    return index


def _filing_region(state: str, country: str | None) -> str | None:
    code = _region_code(state)
    if code:
        return code
    cleaned = re.sub(r"\s+", " ", state).strip()
    lowered = cleaned.lower()
    if not cleaned or lowered in {"n-a", "na", "n/a", "none", "-", "uk", "u.k.", "united kingdom", "united states", "usa"}:
        return None
    if country and lowered == {
        "US": "united states", "CA": "canada", "GB": "united kingdom", "AU": "australia",
        "SG": "singapore", "IL": "israel", "DE": "germany", "NZ": "new zealand", "FI": "finland",
        "AE": "united arab emirates", "NO": "norway", "ES": "spain", "FR": "france", "SE": "sweden",
        "IN": "india", "PL": "poland",
    }.get(country, ""):
        return None
    return cleaned


_LABEL_REST = ("arrests", "courtrecords", "propertyrecords", "residentdirectory", "residentsdirectory", "voterrecords")
_ABBREV_PLACE = {code.lower(): ("US", code) for code in US_REGION_CODES}
_ABBREV_PLACE.update({code.lower(): ("CA", code) for code in CA_REGION_CODES})
_COUNTRY_NAMES = (("australia", "AU"), ("canada", "CA"), ("unitedkingdom", "GB"))
_ADDRESS_RE = re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+){0,3}),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b")
_ADDRESS_INDEX: dict[str, dict] | None = None


def _named_place(name: str, domain: str) -> tuple[str, str] | None:
    label = _fold_place(domain.split(".")[0])
    folded_name = _fold_place(name)
    for folded, country, code in _FOLDED_PLACES:
        if label.startswith(folded) or folded_name.startswith(folded):
            return country, code
        if len(folded) >= 5 and (label.endswith(folded) or folded_name.endswith(folded)):
            return country, code
    for folded, country in _COUNTRY_NAMES:
        if label.startswith(folded) or label.endswith(folded):
            return country, ""
    for rest in _LABEL_REST:
        if label.endswith(rest) and len(label) > len(rest):
            found = _ABBREV_PLACE.get(label[: -len(rest)])
            if found:
                return found
    return None


def _address_index() -> dict[str, dict]:
    global _ADDRESS_INDEX
    if _ADDRESS_INDEX is not None:
        return _ADDRESS_INDEX
    index: dict[str, dict] = {}
    if LOG_PATH.parent.joinpath("v3.jsonl").exists() or (ROOT / "research" / "evidence" / "v3.jsonl").exists():
        path = ROOT / "research" / "evidence" / "v3.jsonl"
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                domain = item.get("domain")
                if not domain:
                    continue
                found = None
                for page in item.get("pages") or []:
                    url = page.get("final_url") or page.get("url") or ""
                    if not same_host(domain, url):
                        continue
                    match = _ADDRESS_RE.search(page.get("text") or "")
                    if not match or match.group(2) not in US_REGION_CODES:
                        continue
                    found = {
                        "locality": match.group(1),
                        "region": match.group(2),
                        "postal_code": match.group(3),
                        "country": "US",
                    }
                    break
                if found:
                    index[domain] = found
    _ADDRESS_INDEX = index
    return index


def _tld_country(domain: str) -> str | None:
    labels = domain.lower().split(".")
    if not labels:
        return None
    if labels[-1] == "uk":
        return "GB"
    if labels[-1] == "ca":
        return "CA"
    if labels[-1] in {"us", "gov"}:
        return "US"
    if labels[-1] == "au":
        return "AU"
    if labels[-1] == "eu":
        return "EU"
    return None


def _form_regions(record: dict) -> list[str]:
    found = []
    seen = set()
    for field in record["search"].get("inputs") or []:
        if field.get("canonical") not in {"region", "state"}:
            continue
        for option in field.get("options") or []:
            code = option.strip().upper()
            if code in seen or code not in US_REGION_CODES and code not in CA_REGION_CODES:
                continue
            seen.add(code)
            found.append(code)
    return found


def _one_country(codes: list[str]) -> str | None:
    found = set()
    for code in codes:
        if code.startswith("US"):
            found.add("US")
        elif code.startswith("CA"):
            found.add("CA")
        elif code in {"EU", "GB", "AU"}:
            found.add(code)
    if len(found) == 1:
        return found.pop()
    return None


def _form_country(codes: list[str]) -> str | None:
    found = set()
    for code in codes:
        if code in US_REGION_CODES:
            found.add("US")
        elif code in CA_REGION_CODES:
            found.add("CA")
    if len(found) == 1:
        return found.pop()
    return None


def _join_address(street, locality, region, postal, country) -> str | None:
    parts = [part for part in (street, locality, region, postal, country) if part]
    return ", ".join(parts) if parts else None


def _add_coverage(coverage: list[str], code: str) -> None:
    if code and code not in coverage:
        coverage.append(code)


def place_location(record: dict) -> None:
    """Operator place and the regions a page, site name, or search form established."""
    coverage = list(record["legal"]["residency"])
    search_regions = _form_regions(record)
    product = _named_place(record["name"], record["domain"])
    filing = _cppa_index().get(record["domain"]) or (_CPPA_NAMES or {}).get(_fold_org(record["name"]))
    street = locality = postal = region = country = None
    sources = []
    if filing:
        street = filing["street"]
        locality = filing["locality"]
        postal = filing["postal_code"]
        region = filing["region"]
        country = filing["country"]
        sources.append("cppa_registry")
    tld = _tld_country(record["domain"])
    if not country and tld:
        country = tld
        sources.append("domain")
    if product and (country is None or country == product[0]):
        if country is None:
            country = product[0]
        if region is None and product[1]:
            region = product[1]
        sources.append("site_name")
    page_country = _one_country(coverage)
    if not country and page_country:
        country = page_country
        sources.append("page")
    form_country = _form_country(search_regions)
    if not country and form_country:
        country = form_country
        sources.append("search_form")
    elif search_regions:
        sources.append("search_form")
    if region is None and not filing:
        found = _address_index().get(record["domain"])
        if found:
            locality = locality or found["locality"]
            postal = postal or found["postal_code"]
            region = found["region"]
            country = country or found["country"]
            sources.append("page")
    if region is None and len(search_regions) == 1 and (country in (None, "US", "CA")):
        only = search_regions[0]
        if (country in (None, "US") and only in US_REGION_CODES) or (country == "CA" and only in CA_REGION_CODES):
            region = only
            country = country or ("US" if only in US_REGION_CODES else "CA")
    mailed = record["removal"]["postal_address"]
    if mailed and not street:
        sources.append("page")
    if product:
        _add_coverage(coverage, product[0])
        if product[1]:
            _add_coverage(coverage, f"{product[0]}-{product[1]}")
    if country:
        _add_coverage(coverage, country)
    if search_regions and form_country:
        _add_coverage(coverage, form_country)
    formatted = None
    if street or locality or postal:
        formatted = _join_address(street, locality, region, postal, country)
    elif mailed:
        formatted = mailed
    record["location"] = {
        "country": country,
        "region": region,
        "locality": locality,
        "postal_code": postal,
        "street": street,
        "postal_address": formatted,
        "coverage": coverage,
        "search_regions": search_regions,
        "sources": list(dict.fromkeys(sources)),
    }


EMAIL_VERIFY_RE = re.compile(
    r"verification (?:e-?mail|link)|confirm your e-?mail|(?:e-?mail|email) you a (?:link|code)|click the (?:link|button) in (?:the |your )?(?:e-?mail|email)",
    re.I,
)
ID_DOC_RE = re.compile(
    r"(?:upload|provide|submit|attach|send)(?:\s+\w+){0,5}\s+(?:a |your |the )?(?:valid )?(?:government[- ]issued(?: photo)? id|driver'?s licen[cs]e|photo id|passport(?!-)|copy of your (?:id|identification))"
    r"|upload a scan of your id",
    re.I,
)
REMOVAL_PAGE_RE = re.compile(r"opt-?out|privacy|do-not-sell|donotsell|remove|delet|dsar|ccpa|suppress|not-sell", re.I)
V3_LOG = ROOT / "research" / "evidence" / "v3.jsonl"


def _removal_key(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{host}{path}"


def _removalish_page(page: dict) -> bool:
    blob = f"{page.get('url') or ''} {page.get('final_url') or ''} {page.get('role') or ''}"
    return bool(REMOVAL_PAGE_RE.search(blob))


def _slim_service_page(page: dict) -> dict:
    return {
        "url": page.get("url"),
        "final_url": page.get("final_url"),
        "role": page.get("role"),
        "text": (page.get("text") or "")[:2500],
        "forms": [
            {
                "action": form.get("action") or "",
                "method": form.get("method") or "",
                "inputs": [
                    {
                        "name": field.get("name") or "",
                        "type": field.get("type") or "",
                        "canonical": field.get("canonical"),
                        "required": bool(field.get("required")),
                        "placeholder": field.get("placeholder"),
                    }
                    for field in (form.get("inputs") or [])
                ],
            }
            for form in (page.get("forms") or [])
        ],
    }


def _service_pages(raw: dict[str, dict]) -> dict[str, list[dict]]:
    pages: dict[str, list[dict]] = {}
    last: dict[str, dict] = {}
    if V3_LOG.exists():
        with V3_LOG.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    site = json.loads(line)
                except json.JSONDecodeError:
                    continue
                domain = site.get("domain")
                if domain:
                    last[domain] = site
    for domain, site in last.items():
        pages[domain] = [_slim_service_page(page) for page in site.get("pages") or []]
    for domain, item in raw.items():
        pages.setdefault(domain, []).extend(_slim_service_page(page) for page in item.get("pages") or [])
    return pages


def _required_names(fields: list[dict]) -> list[str]:
    names = []
    for field in fields:
        if not field.get("required") or field.get("type") in ("hidden", "submit", "button", "checkbox", "radio"):
            continue
        name = field.get("canonical")
        if name and name not in names:
            names.append(name)
    return names


def attach_submission(records: dict[str, dict], raw: dict[str, dict]) -> None:
    """Copy the removal form's submit URL and verification lines the stored pages already showed."""
    pages = _service_pages(raw)
    by_domain: dict[str, list[dict]] = {}
    for record in records.values():
        by_domain.setdefault(record["domain"], []).append(record)
    for domain, group in by_domain.items():
        email_hit = False
        id_hit = False
        for page in pages.get(domain, []):
            if not _removalish_page(page):
                continue
            text = page.get("text") or ""
            email_hit = email_hit or bool(EMAIL_VERIFY_RE.search(text))
            id_hit = id_hit or bool(ID_DOC_RE.search(text))
        chosen = None
        chosen_score = 0
        for page in pages.get(domain, []):
            form = choose_removal_form(page)
            if not form or not form.get("action"):
                continue
            names = {field.get("name") for field in form["fields"] if field.get("name")}
            for record in group:
                stored = {field.get("name") for field in record["removal"]["form_fields"] if field.get("name")}
                score = len(stored & names)
                if stored and score > chosen_score:
                    chosen = form
                    chosen_score = score
        for record in group:
            removal = record["removal"]
            removal["form_action"] = chosen["action"] if chosen else None
            removal["form_method"] = chosen["method"] if chosen else None
            record["_checks"] = {
                "email_verification": True if email_hit else None,
                "id_document": True if id_hit else None,
            }


def mark_shared(records: dict[str, dict]) -> None:
    groups: dict[str, list[str]] = {}
    for item in records.values():
        token = _removal_key(item["removal"].get("url"))
        if token:
            groups.setdefault(token, []).append(item["id"])
    for item in records.values():
        token = _removal_key(item["removal"].get("url"))
        others = [other for other in groups.get(token, []) if other != item["id"]]
        item["automation"]["shared_removal_ids"] = sorted(others)


JUNK_PROCESSING = re.compile(
    r"positive roi|go live|highly targeted|logs are retained|provides results within|"
    r"confirmation and have not received|list expert|reply within 24 hours|"
    r"\bme within \d+ hours|standard inquiries",
    re.I,
)


def clean_processing(record: dict) -> None:
    """Drop a stored timeline that is marketing, a support reply, or a log-retention line."""
    removal = record["removal"]
    text = removal.get("processing_text") or ""
    if text and JUNK_PROCESSING.search(text):
        removal["processing_text"] = None
        removal["processing_days"] = None


MAIL_HOSTS = (
    ("google", ("google.com", "googlemail.com")),
    ("microsoft", ("outlook.com", "protection.outlook.com")),
    ("proofpoint", ("pphosted.com", "proofpoint.com")),
    ("mimecast", ("mimecast.com",)),
    ("barracuda", ("barracudanetworks.com",)),
    ("cisco", ("iphmx.com",)),
    ("amazon", ("amazonaws.com", "amazonses.com")),
    ("zoho", ("zoho.com",)),
    ("godaddy", ("secureserver.net",)),
    ("proton", ("protonmail.ch", "proton.me")),
    ("cloudflare", ("mx.cloudflare.net",)),
)
DNS_HOSTS = (
    ("cloudflare", ("cloudflare.com",)),
    ("amazon", ("awsdns",)),
    ("godaddy", ("domaincontrol.com",)),
    ("google", ("googledomains.com",)),
    ("namecheap", ("registrar-servers.com",)),
    ("azure", ("azure-dns.com", "azure-dns.net", "azure-dns.org", "azure-dns.info")),
    ("akamai", ("akam.net",)),
    ("dnsmadeeasy", ("dnsmadeeasy.com",)),
)
FORM_TAG_RE = re.compile(r"<form\b([^>]*)>", re.I)
ENCTYPE_RE = re.compile(r"""enctype\s*=\s*["']?([^"'\s>]+)""", re.I)


def _host_provider(hosts: list[str], table: tuple) -> str | None:
    for host in hosts:
        for name, suffixes in table:
            for suffix in suffixes:
                if host == suffix or host.endswith("." + suffix) or (suffix == "awsdns" and "awsdns" in host):
                    return name
    return None


def _dns_encode(name: str) -> bytes:
    parts = []
    for label in name.strip(".").split("."):
        raw = label.encode("idna")
        if len(raw) > 63:
            return b""
        parts.append(bytes([len(raw)]) + raw)
    return b"".join(parts) + b"\x00"


def _dns_read_name(data: bytes, offset: int, depth: int = 0) -> tuple[int, str]:
    labels = []
    jumped = False
    end = offset
    while depth < 8 and offset < len(data):
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                end = offset
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            _ignored, rest = _dns_read_name(data, pointer, depth + 1)
            if rest:
                labels.append(rest)
            break
        offset += 1
        labels.append(data[offset:offset + length].decode("ascii", "replace"))
        offset += length
        if not jumped:
            end = offset
    return end, ".".join(part for part in labels if part)


def _txt_join(data: bytes, start: int, rdlen: int) -> str:
    end = min(len(data), start + rdlen)
    offset = start
    parts = []
    while offset < end:
        length = data[offset]
        offset += 1
        piece = data[offset:offset + length]
        offset += length
        if not piece:
            break
        parts.append(piece.decode("utf-8", "replace"))
    return "".join(parts)[:300]


def _dns_recv_tcp(packet: bytes, server: str) -> bytes | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect((server, 53))
        sock.sendall(struct.pack(">H", len(packet)) + packet)
        header = b""
        while len(header) < 2:
            chunk = sock.recv(2 - len(header))
            if not chunk:
                return None
            header += chunk
        need = struct.unpack(">H", header)[0]
        body = b""
        while len(body) < need:
            chunk = sock.recv(need - len(body))
            if not chunk:
                return None
            body += chunk
        return body
    except OSError:
        return None
    finally:
        sock.close()


def _dns_query(name: str, qtype: int) -> list[str] | None:
    encoded = _dns_encode(name)
    if not encoded:
        return None
    packet = struct.pack(">HHHHHH", random.randrange(1, 65535), 0x0100, 1, 0, 0, 1)
    packet += encoded + struct.pack(">HH", qtype, 1)
    packet += b"\x00" + struct.pack(">HHIH", 41, 1232, 0, 0)
    ident = struct.unpack(">H", packet[:2])[0]
    for server in ("1.1.1.1", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(4)
        try:
            sock.sendto(packet, (server, 53))
            data, _addr = sock.recvfrom(4096)
        except OSError:
            continue
        finally:
            sock.close()
        if len(data) < 12 or struct.unpack(">H", data[:2])[0] != ident:
            continue
        flags, qdcount, ancount = struct.unpack(">HHH", data[2:8])
        if flags & 0x0200:
            data = _dns_recv_tcp(packet, server) or b""
            if len(data) < 12 or struct.unpack(">H", data[:2])[0] != ident:
                continue
            flags, qdcount, ancount = struct.unpack(">HHH", data[2:8])
        rcode = flags & 0x000F
        if rcode == 3:
            return []
        if rcode != 0:
            continue
        offset = 12
        for _ in range(qdcount):
            offset, _ignored = _dns_read_name(data, offset)
            offset += 4
        found = []
        for _ in range(ancount):
            offset, _owner = _dns_read_name(data, offset)
            if offset + 10 > len(data):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
            start = offset + 10
            offset = start + rdlen
            if rtype != qtype or start > len(data):
                continue
            if qtype == 15 and rdlen >= 3:
                _pref, host = _dns_read_name(data, start + 2)
                host = host.lower().rstrip(".")
                if host:
                    found.append(host)
            elif qtype == 2:
                _pref, host = _dns_read_name(data, start)
                host = host.lower().rstrip(".")
                if host:
                    found.append(host)
            elif qtype == 5:
                _end, host = _dns_read_name(data, start)
                host = host.lower().rstrip(".")
                if host:
                    found.append(host)
            elif qtype == 6:
                _end, host = _dns_read_name(data, start)
                host = host.lower().rstrip(".")
                if host:
                    found.append(host)
            elif qtype == 16:
                text = _txt_join(data, start, rdlen)
                if text:
                    found.append(text)
            elif qtype == 28 and rdlen == 16:
                found.append(socket.inet_ntop(socket.AF_INET6, data[start:start + 16]))
            elif qtype == 48:
                found.append("dnskey")
            elif qtype == 257 and rdlen >= 2:
                tag_len = data[start + 1]
                tag_end = start + 2 + tag_len
                if tag_end <= start + rdlen:
                    tag = data[start + 2:tag_end].decode("ascii", "replace").lower()
                    value = data[tag_end:start + rdlen].decode("utf-8", "replace")
                    issuer = value.split(";", 1)[0].strip().lower().rstrip(".")
                    if tag in {"issue", "issuewild"} and issuer:
                        found.append(issuer)
        return found[:12] if qtype == 16 else found[:4]
    return None


def _addresses(domain: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
    except OSError:
        return []
    found = []
    for info in infos:
        ip = info[4][0]
        if ip not in found:
            found.append(ip)
        if len(found) >= 4:
            break
    return found


def _tls_facts(domain: str) -> dict:
    context = ssl.create_default_context()
    try:
        with socket.create_connection((domain, 443), timeout=8) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as secure:
                cert = secure.getpeercert() or {}
                tls_version = secure.version()
    except (OSError, ssl.SSLError):
        return {"tls_org": None, "tls_issuer": None, "tls_names": [], "tls_version": None, "tls_not_before": None, "tls_not_after": None}

    def named(block: tuple, key: str) -> str | None:
        for item in block or ():
            for name, value in item:
                if name == key and value:
                    return str(value)[:120]
        return None

    names = []
    for kind, value in cert.get("subjectAltName") or ():
        if kind != "DNS":
            continue
        host = value.lower().rstrip(".")
        if host == domain or host.endswith("." + domain):
            if host not in names and len(names) < 8:
                names.append(host)
    issuer = named(cert.get("issuer"), "organizationName") or named(cert.get("issuer"), "commonName")
    return {
        "tls_org": named(cert.get("subject"), "organizationName"),
        "tls_issuer": issuer,
        "tls_names": names,
        "tls_version": tls_version if isinstance(tls_version, str) and len(tls_version) <= 16 else None,
        "tls_not_before": _cert_day(cert.get("notBefore")),
        "tls_not_after": _cert_day(cert.get("notAfter")),
    }


def _cert_day(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = time.strptime(" ".join(value.split()), "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return None
    return time.strftime("%Y-%m-%d", parsed)


def _cdn(headers: dict) -> str | None:
    blob = " ".join(str(headers.get(key) or "") for key in ("server", "via", "x-served-by", "x-cache")).lower()
    if headers.get("cf-ray") or "cloudflare" in blob:
        return "cloudflare"
    if headers.get("x-amz-cf-id") or "cloudfront" in blob:
        return "cloudfront"
    if "fastly" in blob:
        return "fastly"
    if "akamai" in blob or headers.get("x-akamai-transformed"):
        return "akamai"
    if headers.get("x-sucuri-id"):
        return "sucuri"
    if headers.get("x-iinfo") or "incapsula" in blob:
        return "imperva"
    return None


def _server_product(headers: dict) -> str | None:
    raw = str(headers.get("server") or "").split("/")[0].strip()
    if raw and len(raw) <= 40 and " " not in raw:
        return raw
    return None


def _form_enctype(html: str) -> str | None:
    tags = FORM_TAG_RE.findall(html or "")
    if not tags:
        return None
    for tag in tags:
        match = ENCTYPE_RE.search(tag)
        if not match:
            continue
        value = match.group(1).lower()
        if "multipart" in value:
            return "multipart"
        if "text/plain" in value:
            return "text"
        if "urlencoded" in value:
            return "urlencoded"
    return "urlencoded"


def _peek_request(fetcher: MappingFetcher, url: str) -> dict:
    empty = {"form_enctype": None, "content_type": None, "hsts": None, "redirect_count": None, "cdn": None, "server": None}
    if not url or not fetcher.allowed(url):
        return empty
    fetcher.wait(urlparse(url).netloc)
    try:
        response = fetcher.s.get(url, timeout=(8, 18), allow_redirects=True, stream=True)
    except Exception:
        return empty
    try:
        headers = {key.lower(): value for key, value in response.headers.items()}
        ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()[:80] or None
        facts = {
            "form_enctype": None,
            "content_type": ctype,
            "hsts": "strict-transport-security" in headers,
            "redirect_count": len(response.history),
            "cdn": _cdn(headers),
            "server": _server_product(headers),
        }
        if response.status_code in (401, 403, 429, 503):
            return facts
        body = b""
        if ctype in (None, "text/html", "application/xhtml+xml"):
            for chunk in response.iter_content(65_536):
                body += chunk
                if len(body) >= 180_000:
                    break
        html = body.decode("utf-8", "replace")
        facts["form_enctype"] = _form_enctype(html)
        return facts
    finally:
        response.close()


def _asn_of(ip: str) -> dict:
    blank = {"asn": None, "asn_country": None, "prefix": None}
    parts = ip.split(".")
    if len(parts) != 4:
        return blank
    rows = _dns_query(".".join(reversed(parts)) + ".origin.asn.cymru.com", 16)
    if not rows:
        return blank
    bits = [part.strip() for part in rows[0].split("|")]
    if len(bits) < 3 or not bits[0].isdigit():
        return blank
    country = bits[2].upper()
    if not (len(country) == 2 and country.isalpha()):
        country = None
    prefix = bits[1] if re.fullmatch(r"[0-9a-fA-F.:]+/\d{1,3}", bits[1]) else None
    return {"asn": bits[0], "asn_country": country, "prefix": prefix}


def _dmarc_policy(domain: str) -> str | None:
    for row in _dns_query(f"_dmarc.{domain}", 16) or []:
        match = re.search(r"(?:^|;)\s*p\s*=\s*(none|quarantine|reject)\b", row, re.I)
        if match:
            return match.group(1).lower()
    return None


def _dkim_selectors(domain: str) -> list[str]:
    found = []
    for selector in ("google", "selector1", "selector2", "default"):
        rows = _dns_query(f"{selector}._domainkey.{domain}", 16)
        if rows:
            found.append(selector)
    return found


_RDAP_LOCK = threading.Lock()


def _rdap(domain: str) -> dict:
    blank = {"registrar": None, "registered": None, "registration_expires": None, "domain_status": []}
    import requests

    try:
        with _RDAP_LOCK:
            time.sleep(0.4)
            response = requests.get(
                f"https://rdap.org/domain/{domain}",
                timeout=12,
                headers={
                    "Accept": "application/rdap+json, application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; data-brokers-list-research/1.0; "
                        "+https://github.com/cvs0/data-brokers-list)"
                    ),
                },
                allow_redirects=True,
            )
    except Exception:
        return blank
    if response.status_code != 200:
        return blank
    try:
        data = response.json()
    except ValueError:
        return blank
    registrar = None
    pool = list(data.get("entities") or [])
    nested = []
    for entity in pool:
        nested.extend(entity.get("entities") or [])
    for entity in [*pool, *nested][:16]:
        roles = {str(role).lower() for role in entity.get("roles") or []}
        if "registrar" not in roles:
            continue
        for row in (entity.get("vcardArray") or [None, []])[1] or []:
            if row and row[0] == "fn" and len(row) > 3 and isinstance(row[3], str):
                registrar = row[3].strip()[:80] or None
                break
        if registrar:
            break

    def event_day(action: str) -> str | None:
        for event in data.get("events") or []:
            if event.get("eventAction") != action:
                continue
            raw = str(event.get("eventDate") or "")
            if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
                return raw[:10]
        return None

    statuses = []
    for status in data.get("status") or []:
        token = str(status).split("#")[-1].strip()[:48]
        if token and token not in statuses and len(statuses) < 6:
            statuses.append(token)
    return {
        "registrar": registrar,
        "registered": event_day("registration"),
        "registration_expires": event_day("expiration"),
        "domain_status": statuses,
    }


def _peek_security(fetcher: MappingFetcher, url: str) -> dict:
    empty = {
        "hsts_max_age": None,
        "hsts_subdomains": None,
        "hsts_preload": None,
        "http3": None,
        "http_version": None,
        "frame_options": None,
        "nosniff": None,
        "referrer_policy": None,
        "csp": None,
        "powered_by": None,
        "cookie_names": [],
    }
    if not url or not fetcher.allowed(url):
        return empty
    fetcher.wait(urlparse(url).netloc)
    try:
        response = fetcher.s.get(url, timeout=(8, 15), allow_redirects=True, stream=True)
    except Exception:
        return empty
    try:
        headers = {key.lower(): value for key, value in response.headers.items()}
        version = {10: "http/1.0", 11: "http/1.1", 20: "h2"}.get(getattr(response.raw, "version", None))
        hsts = headers.get("strict-transport-security") or ""
        age = re.search(r"max-age=(\d+)", hsts, re.I)
        alt = (headers.get("alt-svc") or "").lower()
        frame = (headers.get("x-frame-options") or "").split(",")[0].strip().lower()[:40] or None
        referrer = (headers.get("referrer-policy") or "").split(",")[0].strip().lower()[:40] or None
        powered = (headers.get("x-powered-by") or "").replace("\n", " ").strip()[:40] or None
        cookies = []
        raw_headers = getattr(response.raw, "headers", None)
        rows = raw_headers.getlist("Set-Cookie") if raw_headers is not None and hasattr(raw_headers, "getlist") else []
        if not rows and headers.get("set-cookie"):
            rows = [headers["set-cookie"]]
        for row in rows:
            name = str(row).split("=", 1)[0].strip()
            if name and name not in cookies and len(name) <= 40 and len(cookies) < 6:
                cookies.append(name)
        return {
            "hsts_max_age": int(age.group(1)) if age else None,
            "hsts_subdomains": "includesubdomains" in hsts.lower(),
            "hsts_preload": "preload" in hsts.lower(),
            "http3": "h3" in alt,
            "http_version": version,
            "frame_options": frame,
            "nosniff": "nosniff" in (headers.get("x-content-type-options") or "").lower(),
            "referrer_policy": referrer,
            "csp": "content-security-policy" in headers or "content-security-policy-report-only" in headers,
            "powered_by": powered,
            "cookie_names": cookies,
        }
    finally:
        response.close()


def _network_extra(domain: str, addresses: list[str]) -> dict:
    txt = _dns_query(domain, 16) or []
    spf = next((row[:200] for row in txt if row.lower().startswith("v=spf1")), None)
    cname_rows = _dns_query(domain, 5) or []
    soa_rows = _dns_query(domain, 6) or []
    dnskey = _dns_query(domain, 48)
    ipv4 = next((ip for ip in addresses if ":" not in ip), None)
    asn = _asn_of(ipv4) if ipv4 else {"asn": None, "asn_country": None, "prefix": None}
    tls = _tls_facts(domain) if addresses else {}
    rdap = _rdap(domain)
    return {
        "checked": True,
        "ipv6": (_dns_query(domain, 28) or [])[:4],
        "cname": cname_rows[0] if cname_rows else None,
        "spf": spf,
        "dmarc": _dmarc_policy(domain),
        "caa": (_dns_query(domain, 257) or [])[:4],
        "dnssec": None if dnskey is None else bool(dnskey),
        "soa_primary": soa_rows[0] if soa_rows else None,
        "dkim": _dkim_selectors(domain),
        "asn": asn["asn"],
        "asn_country": asn["asn_country"],
        "prefix": asn["prefix"],
        "tls_version": tls.get("tls_version"),
        "tls_not_before": tls.get("tls_not_before"),
        "tls_not_after": tls.get("tls_not_after"),
        "registrar": rdap["registrar"],
        "registered": rdap["registered"],
        "registration_expires": rdap["registration_expires"],
        "domain_status": rdap["domain_status"],
    }


def place_network(record: dict) -> None:
    """Public DNS, certificate, and removal-request facts. Absence stays null."""
    update = record.pop("_network", None) or {}
    extra = record.pop("_network_extra", None) or {}
    checked = bool(update.get("checked"))
    extra_checked = bool(extra.get("checked"))
    addresses = update.get("addresses") if isinstance(update.get("addresses"), list) else []
    mx = update.get("mx") if isinstance(update.get("mx"), list) else []
    nameservers = update.get("nameservers") if isinstance(update.get("nameservers"), list) else []
    tls_names = update.get("tls_names") if isinstance(update.get("tls_names"), list) else []
    ipv6 = extra.get("ipv6") if extra_checked and isinstance(extra.get("ipv6"), list) else []
    caa = extra.get("caa") if extra_checked and isinstance(extra.get("caa"), list) else []
    dkim = extra.get("dkim") if extra_checked and isinstance(extra.get("dkim"), list) else []
    statuses = extra.get("domain_status") if extra_checked and isinstance(extra.get("domain_status"), list) else []
    record["network"] = {
        "checked": checked,
        "addresses": addresses[:4],
        "ipv6": [str(ip) for ip in ipv6[:4]],
        "cname": extra.get("cname") if extra_checked else None,
        "mx": mx[:4],
        "mail_provider": update.get("mail_provider"),
        "spf": extra.get("spf") if extra_checked else None,
        "dmarc": extra.get("dmarc") if extra_checked else None,
        "dkim": [str(name) for name in dkim[:4]],
        "nameservers": nameservers[:4],
        "dns_provider": update.get("dns_provider"),
        "soa_primary": extra.get("soa_primary") if extra_checked else None,
        "caa": [str(name) for name in caa[:4]],
        "dnssec": extra.get("dnssec") if extra_checked else None,
        "asn": extra.get("asn") if extra_checked else None,
        "asn_country": extra.get("asn_country") if extra_checked else None,
        "prefix": extra.get("prefix") if extra_checked else None,
        "cdn": update.get("cdn"),
        "server": update.get("server"),
        "tls_org": update.get("tls_org"),
        "tls_issuer": update.get("tls_issuer"),
        "tls_names": tls_names[:8],
        "tls_version": extra.get("tls_version") if extra_checked else None,
        "tls_not_before": extra.get("tls_not_before") if extra_checked else None,
        "tls_not_after": extra.get("tls_not_after") if extra_checked else None,
        "registrar": extra.get("registrar") if extra_checked else None,
        "registered": extra.get("registered") if extra_checked else None,
        "registration_expires": extra.get("registration_expires") if extra_checked else None,
        "domain_status": [str(token) for token in statuses[:6]],
    }
    removal = record["removal"]
    target = removal.get("final_url") or removal.get("url") or ""
    host = (urlparse(target).hostname or "").lower().removeprefix("www.") or None
    https = target.startswith("https://") if target else None
    catalog = removal.get("url") or ""
    final = removal.get("final_url") or ""
    if catalog and final:
        redirected = norm_url(catalog) != norm_url(final)
    else:
        redirected = None
    same = None
    if host:
        domain = record["domain"]
        same = host == domain or host.endswith("." + domain)
    record["request"] = {
        "removal_host": host,
        "same_site": same,
        "https": https,
        "redirected": redirected,
        "redirect_count": update.get("redirect_count") if checked else None,
        "form_enctype": update.get("form_enctype") if checked else None,
        "content_type": update.get("content_type") if checked else None,
        "hsts": update.get("hsts") if checked else None,
        "hsts_max_age": extra.get("hsts_max_age") if extra_checked else None,
        "hsts_subdomains": extra.get("hsts_subdomains") if extra_checked else None,
        "hsts_preload": extra.get("hsts_preload") if extra_checked else None,
        "http_version": extra.get("http_version") if extra_checked else None,
        "http3": extra.get("http3") if extra_checked else None,
        "frame_options": extra.get("frame_options") if extra_checked else None,
        "nosniff": extra.get("nosniff") if extra_checked else None,
        "referrer_policy": extra.get("referrer_policy") if extra_checked else None,
        "csp": extra.get("csp") if extra_checked else None,
        "powered_by": extra.get("powered_by") if extra_checked else None,
        "cookie_names": extra.get("cookie_names") if extra_checked and isinstance(extra.get("cookie_names"), list) else [],
    }


FIELD_DETAIL_KEYS = (
    "id", "label", "inputmode", "pattern", "minlength", "maxlength", "min", "max",
    "step", "accept", "multiple", "disabled", "readonly", "checked", "value",
)
SKIP_FIELD_NAMES = {
    "g-recaptcha-response", "h-captcha-response", "cf-turnstile-response", "frc-captcha-solution",
}


def _clip(value, limit: int) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit] or None


def _attr_int(value) -> int | None:
    text = str(value).strip() if value is not None else ""
    if not text.isdigit():
        return None
    number = int(text)
    if number > 100000:
        return None
    return number


def _safe_value(kind: str, value) -> str | None:
    if kind in {"password", "hidden", "file"}:
        return None
    text = _clip(value, 80)
    if not text or "@" in text:
        return None
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "javascript:")):
        return None
    if len(text) > 40 and " " not in text and not re.fullmatch(r"[0-9./:-]+", text):
        return None
    return text


def empty_form_meta() -> dict:
    return {
        "id": None,
        "name": None,
        "autocomplete": None,
        "novalidate": None,
        "submit_label": None,
        "hidden_names": [],
    }


def normalize_field(field: dict) -> None:
    for key in FIELD_DETAIL_KEYS:
        field.setdefault(key, None)


def normalize_form_meta(raw: dict | None, seen: bool) -> dict:
    meta = empty_form_meta()
    if not seen or not isinstance(raw, dict):
        return meta
    meta["id"] = _clip(raw.get("id"), 80)
    meta["name"] = _clip(raw.get("name"), 80)
    meta["autocomplete"] = _clip(raw.get("autocomplete"), 40)
    meta["submit_label"] = _clip(raw.get("submit_label"), 80)
    if raw.get("novalidate") is True or raw.get("novalidate") is False:
        meta["novalidate"] = bool(raw["novalidate"])
    names = []
    for name in raw.get("hidden_names") or []:
        clipped = _clip(name, 80)
        if not clipped or clipped.lower() in SKIP_FIELD_NAMES or clipped in names:
            continue
        names.append(clipped)
        if len(names) >= 12:
            break
    meta["hidden_names"] = names
    return meta


def _detail_from_raw(raw: dict) -> dict:
    kind = ((raw.get("type") or "text").lower() or "text")[:40]
    options = []
    for option in raw.get("options") or []:
        clipped = _clip(option, 80)
        if clipped and clipped not in options:
            options.append(clipped)
        if len(options) >= 320:
            break
    return {
        "name": (_clip(raw.get("name"), 80) or ""),
        "type": kind,
        "label": _clip(raw.get("label"), 80),
        "placeholder": _clip(raw.get("placeholder"), 80),
        "autocomplete": _clip(raw.get("autocomplete"), 40),
        "required": bool(raw.get("required")),
        "id": _clip(raw.get("id"), 80),
        "inputmode": _clip(str(raw.get("inputmode") or "").lower(), 24),
        "pattern": _clip(raw.get("pattern"), 120),
        "minlength": _attr_int(raw.get("minlength")),
        "maxlength": _attr_int(raw.get("maxlength")),
        "min": _clip(raw.get("min"), 40),
        "max": _clip(raw.get("max"), 40),
        "step": _clip(raw.get("step"), 20),
        "accept": _clip(raw.get("accept"), 80),
        "multiple": bool(raw.get("multiple")),
        "disabled": bool(raw.get("disabled")),
        "readonly": bool(raw.get("readonly")),
        "checked": bool(raw.get("checked")),
        "value": _safe_value(kind, raw.get("value")),
        "options": options,
    }


def _control_name(field: dict) -> str:
    return (field.get("name") or "").strip() or (field.get("id") or "").strip()


def _match_incoming(field: dict, incoming: list[dict], used: set[int]) -> int | None:
    """Pair a stored control with the next unread one of the same name or id.

    Radios share a name, so the first stored radio takes the first unread
    radio. The catalog name is often the id when the control had no name.
    """
    name = (field.get("name") or "").strip()
    if name:
        for index, candidate in enumerate(incoming):
            if index in used:
                continue
            if _control_name(candidate) == name or (candidate.get("id") or "").strip() == name:
                return index
    placeholder = (field.get("placeholder") or "").strip().lower()
    if not placeholder:
        return None
    hits = []
    for index, candidate in enumerate(incoming):
        if index in used:
            continue
        other = (candidate.get("placeholder") or "").strip().lower()
        label = (candidate.get("label") or "").strip().lower()
        if placeholder == other or placeholder == label:
            hits.append(index)
    if len(hits) == 1:
        return hits[0]
    return None


def merge_form_fields(stored: list[dict], incoming: list[dict]) -> None:
    """Copy public control attributes onto the stored fields. A longer dropdown list is kept."""
    used: set[int] = set()
    for field in stored:
        index = _match_incoming(field, incoming, used)
        if index is None:
            continue
        used.add(index)
        match = incoming[index]
        for key in FIELD_DETAIL_KEYS:
            field[key] = match.get(key)
        if match.get("placeholder"):
            field["placeholder"] = match["placeholder"]
        if match.get("autocomplete"):
            field["autocomplete"] = match["autocomplete"]
        if match.get("required"):
            field["required"] = True
        options = match.get("options") or []
        if len(options) > len(field.get("options") or []):
            field["options"] = options
    for index, candidate in enumerate(incoming):
        if index in used or len(stored) >= 80:
            continue
        name = _control_name(candidate)
        if not name or name.lower() in SKIP_FIELD_NAMES or (candidate.get("type") or "") == "hidden":
            continue
        label = " ".join(filter(None, [name, candidate.get("label"), candidate.get("placeholder"), candidate.get("type")]))
        field = {
            "name": name[:80],
            "type": (candidate.get("type") or "text")[:40],
            "canonical": canonical_field(label),
            "required": bool(candidate.get("required")),
            "autocomplete": candidate.get("autocomplete"),
            "placeholder": candidate.get("placeholder"),
        }
        for key in FIELD_DETAIL_KEYS:
            field[key] = candidate.get(key)
        if candidate.get("options"):
            field["options"] = candidate["options"]
        stored.append(field)


def place_form_detail(record: dict) -> None:
    detail = record.pop("_form_detail", None)
    for side, key in (("search", "inputs"), ("removal", "form_fields")):
        fields = record[side].get(key) or []
        side_detail = detail.get(side) if isinstance(detail, dict) else None
        seen = isinstance(side_detail, dict) and bool(side_detail.get("seen"))
        if seen and side_detail.get("fields"):
            merge_form_fields(fields, side_detail["fields"])
        for field in fields:
            normalize_field(field)
        meta = side_detail.get("meta") if seen else None
        record[side]["form_meta"] = normalize_form_meta(meta if isinstance(meta, dict) else None, seen and isinstance(meta, dict))


_FIELD_KEYS = (
    "name", "type", "canonical", "required", "autocomplete", "placeholder", "id", "label", "inputmode", "pattern",
    "minlength", "maxlength", "min", "max", "step", "accept", "multiple", "disabled", "readonly", "checked", "value",
)
_SEARCH_FORM_CACHE: dict | None = None


def _search_form_overrides() -> dict:
    global _SEARCH_FORM_CACHE
    if _SEARCH_FORM_CACHE is None:
        _SEARCH_FORM_CACHE = (
            json.loads(SEARCH_FORMS_PATH.read_text(encoding="utf-8")) if SEARCH_FORMS_PATH.exists() else {}
        )
    return _SEARCH_FORM_CACHE


def place_search_form(record: dict) -> None:
    """Replace a missing or wrong public search form with one read from the live page, from research/manual/v4-search-forms.json.

    Each entry records the page it was read from and the evidence: the form's submit URL and method, its controls, and
    the URL a synthetic name search landed on. A GET search whose page builds the result URL in script stores that URL
    as url_pattern, with {control} placeholders. Nothing else about the broker changes. A stored CAPTCHA marker is
    cleared only when the entry says the rendered search page showed no widget. An entry marked not_a_person_search
    clears a stored form that is not a search for a person (a contact, demo, sign-up, or opt-out form, or a site search),
    so the broker reads as having no public search, the same as a page where none was found. That includes a form that
    only forwards the name to another company's search through an affiliate or tracking link; that company has its own
    entry. An entry marked paywall keeps the stored form and sets search.paywall: a synthetic search showed that the
    site returns no records without an account or a purchase (an empty results page that links to a paid report, or a
    page that says so).
    """
    entry = _search_form_overrides().get(record["id"])
    if entry and entry.get("paywall"):
        # The search runs, but the site shows no records until the visitor registers or buys a report.
        record["search"]["paywall"] = True
    if entry and entry.get("not_a_person_search"):
        # The stored "search" is a contact, sign-up, demo, or opt-out form (or a site search): not a person search.
        # Its state or province list still says where the broker works, including a control only the manual tags name.
        place_search_fields(record)
        record["_scanned_search_regions"] = _form_regions(record)
        record["search"].update(
            {
                "available": False,
                "method": "none",
                "page_url": None,
                "form_action": None,
                "inputs": [],
                "url_pattern": None,
                "directory_prefixes": [],
                "form_meta": {"id": None, "name": None, "autocomplete": None, "novalidate": None, "submit_label": None, "hidden_names": []},
                "captcha_provider": None,
                "captcha_sitekey": None,
                "captcha_detail": None,
            }
        )
        record.pop("_captcha_search_checked", None)
        return
    if not entry or not entry.get("form"):
        return
    form = entry["form"]
    search = record["search"]
    # The scanned form's state or province list still says where the site searches.
    record["_scanned_search_regions"] = _form_regions(record)
    search["available"] = True
    search["method"] = form["method"]
    search["page_url"] = form["page_url"]
    search["form_action"] = form.get("form_action")
    search["url_pattern"] = form.get("url_pattern")
    inputs = []
    for raw in form["inputs"]:
        field = {key: raw.get(key) for key in _FIELD_KEYS}
        field["required"] = bool(raw.get("required"))
        field["multiple"] = bool(raw.get("multiple", False))
        field["disabled"] = bool(raw.get("disabled", False))
        field["readonly"] = bool(raw.get("readonly", False))
        field["checked"] = bool(raw.get("checked", False))
        if raw.get("options"):
            field["options"] = list(raw["options"])
        inputs.append(field)
    search["inputs"] = inputs
    search["form_meta"] = {
        "id": form.get("form_id"),
        "name": form.get("form_name"),
        "autocomplete": None,
        "novalidate": None,
        "submit_label": form.get("submit_label"),
        "hidden_names": list(form.get("hidden_names") or []),
    }
    if entry.get("captcha_widget") is False:
        search["captcha_provider"] = None
        search["captcha_sitekey"] = None
        search["captcha_detail"] = None
        record["_captcha_search_checked"] = True


def place_search_country(record: dict) -> None:
    """The country a search-form entry established from the live page, when no other source had one.

    An entry may carry only a country: the live search form lists one country's states, or the site says where it
    searches, while the stored form kept no choices.
    """
    regions = record.pop("_scanned_search_regions", None)
    if regions and not record["location"]["search_regions"]:
        record["location"]["search_regions"] = regions
        if "search_form" not in record["location"]["sources"]:
            record["location"]["sources"].append("search_form")
        # The form's states or provinces name one country, as they would have if the form were still stored.
        form_country = _form_country(regions)
        if form_country and not record["location"].get("country"):
            record["location"]["country"] = form_country
            if form_country not in record["location"]["coverage"]:
                record["location"]["coverage"].append(form_country)
    entry = _search_form_overrides().get(record["id"]) or {}
    country = entry.get("country")
    if country and not record["location"].get("country"):
        record["location"]["country"] = country
        if country not in record["location"]["coverage"]:
            record["location"]["coverage"].append(country)
        if "manual" not in record["location"]["sources"]:
            record["location"]["sources"].append("manual")


def place_search_fields(record: dict) -> None:
    """Tag a stored search control the label rules left untagged, from research/manual/v4-search-fields.json.

    Only a control with no canonical value is tagged. A control the scan already tagged keeps its value.
    """
    manual = _search_field_overrides().get(record["id"]) or {}
    for field in record["search"].get("inputs") or []:
        entry = manual.get(field.get("name"))
        if entry and field.get("canonical") is None:
            field["canonical"] = entry["canonical"]


_SEARCH_FIELD_CACHE: dict | None = None


def _search_field_overrides() -> dict:
    global _SEARCH_FIELD_CACHE
    if _SEARCH_FIELD_CACHE is None:
        _SEARCH_FIELD_CACHE = (
            json.loads(SEARCH_FIELDS_PATH.read_text(encoding="utf-8")) if SEARCH_FIELDS_PATH.exists() else {}
        )
    return _SEARCH_FIELD_CACHE


def annotate(record: dict) -> None:
    """Add facts a removal client can use without re-reading the pages."""
    clean_processing(record)
    place_form_detail(record)
    place_search_form(record)
    place_search_fields(record)
    place_location(record)
    place_search_country(record)
    place_network(record)
    search = record["search"]
    search.setdefault("captcha_provider", None)
    search.setdefault("captcha_sitekey", None)
    search_checked = record.pop("_captcha_search_checked", None)
    search["captcha_checked"] = search_checked if search_checked is not None else None
    removal = record["removal"]
    captcha = removal["captcha"]
    removal_checked = record.pop("_captcha_removal_checked", None)
    captcha["checked"] = removal_checked if removal_checked is not None else None
    if captcha["checked"] is not True:
        captcha["checked_url"] = None
    else:
        captcha.setdefault("checked_url", None)
    captcha["detail"] = merge_captcha_detail(captcha.get("provider"), captcha.get("detail"))
    search["captcha_detail"] = merge_captcha_detail(search.get("captcha_provider"), search.get("captcha_detail"))
    removal.setdefault("form_action", None)
    removal.setdefault("form_method", None)
    checks = record.pop("_checks", {})
    usable = []
    seen = set()
    for row in removal["emails"]:
        if row["confidence"] in USABLE_EMAIL and row["email"] not in seen:
            usable.append(row["email"])
            seen.add(row["email"])
    form_ok = bool(removal["form_fields"]) and not removal["captcha"]["provider"]
    email_ok = bool(usable)
    captcha = search_captcha(record)
    fillable = bool(search.get("available") and search.get("inputs") and not captcha)
    if form_ok:
        path = "form"
    elif email_ok:
        path = "email"
    else:
        path = None
    record["automation"] = {
        "search_scanned": search.get("available") is not None,
        "search_fillable": fillable,
        "search_captcha": bool(captcha),
        "search_fields": _named_fields(search.get("inputs") or [], PERSON_FIELDS) if search.get("available") else [],
        "removal_form_fillable": form_ok,
        "removal_email_usable": email_ok,
        "removal_fields": _named_fields(removal.get("form_fields") or []),
        "removal_path": path,
        "fully_automatable": bool(fillable and path),
        "usable_emails": usable,
        "search_required": _required_names(search.get("inputs") or []),
        "removal_required": _required_names(removal.get("form_fields") or []),
        "email_verification": checks.get("email_verification"),
        "id_document": checks.get("id_document"),
        "shared_removal_ids": [],
        "removal_captcha": bool(removal["captcha"]["provider"]),
        "captcha_blocks_form": bool(removal["form_fields"] and removal["captcha"]["provider"]),
        "captcha_email_fallback": False,
        "removal_blocker": None,
    }
    record["automation"]["captcha_email_fallback"] = bool(
        record["automation"]["captcha_blocks_form"] and path == "email"
    )
    record["automation"]["removal_blocker"] = removal_blocker(record)


MAPS_LOG = ROOT / "research" / "evidence" / "broker-maps.jsonl"
AGENT_RE = re.compile(r"\bauthori[sz]ed (?:agent|representative)\b", re.I)
SCRAPE_WORD = r"(?:scrap(?:e|ing)|crawlers?|spiders?|automated (?:means|tools?|access)|robots?(?!\.txt))"
AUTO_BEFORE = re.compile(
    rf"[^.]{{0,140}}\b(?:may not|shall not|must not|prohibit(?:ed|s)?|forbidden|not permitted|not allowed|without (?:our )?prior consent|without (?:our )?consent)\b[^.]{{0,140}}\b{SCRAPE_WORD}\b[^.]*",
    re.I,
)
SELLS_NO = re.compile(
    r"\bwe (?:do not|don't|does not) sell\b|\bdoes not sell (?:your |any |consumers' )?personal information\b",
    re.I,
)
SELLS_YES = re.compile(
    r"\bwe (?:may|might|can) sell (?:your |consumers' )?(?:personal information|personal data)\b"
    r"|\bwe sell (?:your |consumers' )?personal information\b",
    re.I,
)
CHILDREN_NO = re.compile(
    r"[^.]{0,40}\b(?:do not|does not|don't) knowingly collect\b[^.]{0,80}\b(?:child|children|under 13|age of 13)\b"
    r"|[^.]{0,30}\bnot directed (?:to|at) children\b"
    r"|[^.]{0,30}\bnot intended for (?:children|minors)\b",
    re.I,
)
PROC_CONTEXT = re.compile(r"request|delet|opt-?out|opt out|\bremov|\berase|\bsuppress", re.I)
COMMON_PRIVACY = ("/privacy-policy", "/privacy", "/privacy-notice", "/legal/privacy")
COMMON_TERMS = ("/terms-of-service", "/terms-of-use", "/terms", "/legal/terms")


def _snip(match: re.Match) -> str:
    return re.sub(r"\s+", " ", match.group(0)).strip()[:240]


def _path_has(url: str, token: str) -> bool:
    return token in (urlparse(url).path or "").lower()


def _is_privacy(url: str, role: str) -> bool:
    return role == "privacy" or _path_has(url, "privacy")


def _is_terms(url: str, role: str) -> bool:
    path = (urlparse(url).path or "").lower()
    if re.search(r"(?:^|/)terms?(?:[-/]|$)", path):
        return True
    return role == "legal" and "term" in path


def _is_legalish(url: str, role: str) -> bool:
    if role in {"privacy", "legal", "opt_out"}:
        return True
    path = (urlparse(url).path or "").lower()
    return any(token in path for token in ("privacy", "term", "opt-out", "optout", "do-not-sell", "donotsell", "ccpa", "gdpr", "legal"))


def extract_legal(text: str) -> dict:
    """Facts a privacy or terms page states. Absence is not a fact."""
    if not text or len(text) < 80:
        return {}
    found: dict = {}
    residency = [code for code, pattern in JURIS if re.search(pattern, text, re.I)]
    if residency:
        found["residency"] = residency
    categories = [name for name, pattern in DATA_CATEGORIES if re.search(pattern, text, re.I)]
    if categories:
        found["data_categories"] = categories
    if AGENT_RE.search(text):
        found["authorized_agent"] = True
    auto = clause(text)
    if not auto:
        match = AUTO_BEFORE.search(text)
        if match:
            auto = _snip(match)[:400]
    if auto:
        found["automation_clause"] = auto
        found["automation_prohibited"] = True
    denial = SELLS_NO.search(text)
    admission = SELLS_YES.search(text)
    if denial and not admission:
        found["sells_personal_information"] = False
        found["sells_clause"] = _snip(denial)
    elif admission and not denial:
        found["sells_personal_information"] = True
        found["sells_clause"] = _snip(admission)
    if CHILDREN_NO.search(text):
        found["children_not_directed"] = True
    for match in PROC_RE.finditer(text):
        sentence = match.group(0)
        if not PROC_CONTEXT.search(sentence):
            continue
        found["processing_text"] = re.sub(r"\s+", " ", sentence).strip()[:220]
        found["processing_days"] = processing_days(found["processing_text"])
        break
    return found


def merge_legal(record: dict, found: dict) -> None:
    """Fill empty legal facts. Do not replace a fact the record already states."""
    if not found:
        return
    legal = record["legal"]
    legal.setdefault("sells_personal_information", None)
    legal.setdefault("sells_clause", None)
    legal.setdefault("children_not_directed", None)
    if found.get("privacy_policy_url") and not legal.get("privacy_policy_url"):
        legal["privacy_policy_url"] = found["privacy_policy_url"]
    if found.get("terms_url") and not legal.get("terms_url"):
        legal["terms_url"] = found["terms_url"]
    for code in found.get("residency") or []:
        if code not in legal["residency"]:
            legal["residency"].append(code)
    for name in found.get("data_categories") or []:
        if name not in legal["data_categories"]:
            legal["data_categories"].append(name)
    if found.get("authorized_agent") is True and legal.get("authorized_agent") is not True:
        legal["authorized_agent"] = True
    if found.get("automation_clause") and not legal.get("automation_clause"):
        legal["automation_clause"] = found["automation_clause"]
        legal["automation_prohibited"] = True
    if found.get("sells_personal_information") is not None and legal.get("sells_personal_information") is None:
        clause_text = found.get("sells_clause") or ""
        sells = found.get("sells_personal_information")
        if sells is True and "personal" not in clause_text.lower():
            sells = None
        if sells is not None:
            legal["sells_personal_information"] = sells
            legal["sells_clause"] = clause_text or None
    if found.get("children_not_directed") is True and legal.get("children_not_directed") is not True:
        legal["children_not_directed"] = True
    removal = record["removal"]
    if found.get("processing_text") and not removal.get("processing_text") and PROC_CONTEXT.search(found["processing_text"]):
        removal["processing_text"] = found["processing_text"]
        removal["processing_days"] = found.get("processing_days")


def _remember(bucket: list[str], url: str, domain: str) -> None:
    if not url or not str(url).startswith("http") or not same_host(domain, url):
        return
    if url not in bucket and len(bucket) < 8:
        bucket.append(url)


def _map_policy_urls() -> dict[str, dict[str, list[str]]]:
    found: dict[str, dict[str, list[str]]] = {}
    if not MAPS_LOG.exists():
        return found
    with MAPS_LOG.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            domain = item.get("domain")
            if not domain:
                continue
            bucket = found.setdefault(domain, {"privacy": [], "terms": [], "alive_privacy": [], "alive_terms": []})
            for page in item.get("pages") or []:
                url = page.get("final_url") or page.get("url") or ""
                role = page.get("role") or ""
                if _is_privacy(url, role):
                    _remember(bucket["privacy"], url, domain)
                    if page.get("liveness") == "alive":
                        _remember(bucket["alive_privacy"], url, domain)
                if _is_terms(url, role):
                    _remember(bucket["terms"], url, domain)
                    if page.get("liveness") == "alive":
                        _remember(bucket["alive_terms"], url, domain)
            for link in item.get("link_map") or []:
                url = link.get("url") or ""
                role = link.get("role") or ""
                if _is_privacy(url, role):
                    _remember(bucket["privacy"], url, domain)
                if _is_terms(url, role):
                    _remember(bucket["terms"], url, domain)
            for url in (item.get("sitemap") or {}).get("privacy_urls") or []:
                _remember(bucket["privacy"], url, domain)
    return found


def _v3_pages() -> dict[str, list[dict]]:
    pages: dict[str, list[dict]] = {}
    if not V3_LOG.exists():
        return pages
    with V3_LOG.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            domain = item.get("domain")
            if domain and item.get("pages"):
                pages[domain] = item["pages"]
    return pages


def attach_legal(records: dict[str, dict], raw: dict[str, dict]) -> None:
    """Re-read stored privacy and terms text, and keep policy URLs already loaded."""
    for record in records.values():
        legal = record["legal"]
        legal.setdefault("sells_personal_information", None)
        legal.setdefault("sells_clause", None)
        legal.setdefault("children_not_directed", None)
    by_domain: dict[str, list[dict]] = {}
    for record in records.values():
        by_domain.setdefault(record["domain"], []).append(record)
    stored = _v3_pages()
    mapped = _map_policy_urls()
    for domain, group in by_domain.items():
        pages = list(stored.get(domain) or [])
        pages.extend((raw.get(domain) or {}).get("pages") or [])
        parts = []
        privacy_url = None
        privacy_len = -1
        terms_url = None
        terms_len = -1
        for page in pages:
            if page.get("liveness") != "alive":
                continue
            url = page.get("final_url") or page.get("url") or ""
            if not url or not same_host(domain, url):
                continue
            role = page.get("role") or ""
            text = page.get("text") or ""
            if _is_privacy(url, role) and len(text) >= privacy_len:
                privacy_url = url
                privacy_len = len(text)
            if _is_terms(url, role) and len(text) >= terms_len:
                terms_url = url
                terms_len = len(text)
            if _is_legalish(url, role) and text:
                parts.append(text)
        known = mapped.get(domain) or {}
        if not privacy_url and known.get("alive_privacy"):
            privacy_url = known["alive_privacy"][0]
        if not terms_url and known.get("alive_terms"):
            terms_url = known["alive_terms"][0]
        found = extract_legal("\n".join(parts)[:32000])
        if privacy_url:
            found["privacy_policy_url"] = privacy_url
        if terms_url:
            found["terms_url"] = terms_url
        for record in group:
            merge_legal(record, found)


def _policy_rank(url: str) -> tuple:
    path = (urlparse(url).path or "").lower().rstrip("/")
    if path.endswith("privacy-policy") or path.endswith("privacypolicy"):
        return (0, len(path))
    if path.endswith("/privacy") or path == "/privacy":
        return (1, len(path))
    if "privacy" in path:
        return (2, len(path))
    return (3, len(path))


def _html_policy(html: str) -> tuple[str, str, list[str]]:
    doc = Doc()
    try:
        doc.feed(html or "")
        doc.close()
    except Exception:
        return "", "", []
    text = re.sub(r"\s+", " ", " ".join(doc.text)).strip()[:24000]
    links = []
    for href, label in doc.anchors:
        if "term" in f"{href} {label}".lower() and href not in links:
            links.append(href)
    return text, doc.title or "", links


def _read_policy(fetcher: MappingFetcher, url: str, domain: str) -> dict | None:
    res = fetcher.get(url)
    if res.get("skipped"):
        return {"skip": "robots"}
    if res.get("error"):
        return {"skip": "error"}
    status = res.get("status")
    if status in (401, 403, 429, 503):
        return {"skip": "blocked", "status": status}
    if status in (404, 410) or not res.get("html"):
        return None
    final = res.get("final_url") or url
    if not same_host(domain, final):
        return None
    text, title, links = _html_policy(res.get("html") or "")
    if _challenge(title, text) or _hard_error(title, text):
        return {"skip": "blocked", "status": status}
    if len(text) < 400:
        return None
    return {"url": final, "text": text, "links": links, "status": status}


def _first_policy(fetcher: MappingFetcher, urls: list[str], domain: str) -> tuple[dict | None, str | None]:
    blocked = None
    for url in urls:
        opened = _read_policy(fetcher, url, domain)
        if not opened:
            continue
        if opened.get("skip"):
            blocked = opened["skip"]
            if blocked == "blocked":
                return None, blocked
            continue
        return opened, None
    return None, blocked


def crawl_legal(limit: int) -> None:
    """Fetch one unread privacy page, and a terms page when automation is still unstated. robots.txt applies."""
    records = base_records()
    raw = load_done(LOG_PATH)
    apply_log(records, raw)
    attach_legal(records, raw)
    stored = _v3_pages()
    mapped = _map_policy_urls()
    by_domain: dict[str, dict] = {}
    for record in records.values():
        by_domain.setdefault(record["domain"], record)
    jobs = []
    for domain, record in by_domain.items():
        if (raw.get(domain) or {}).get("legal_done"):
            continue
        if record.get("liveness") in {"dead", "likely_dead"}:
            continue
        legal = record["legal"]
        pages = list(stored.get(domain) or [])
        pages.extend((raw.get(domain) or {}).get("pages") or [])
        privacy_len = 0
        terms_len = 0
        for page in pages:
            url = page.get("final_url") or page.get("url") or ""
            if page.get("liveness") != "alive":
                continue
            text_len = len(page.get("text") or "")
            role = page.get("role") or ""
            if _is_privacy(url, role):
                privacy_len = max(privacy_len, text_len)
            if _is_terms(url, role):
                terms_len = max(terms_len, text_len)
        known = mapped.get(domain) or {}
        privacy_urls: list[str] = []
        if privacy_len < 2000:
            if legal.get("privacy_policy_url"):
                _remember(privacy_urls, legal["privacy_policy_url"], domain)
            for url in known.get("privacy") or []:
                _remember(privacy_urls, url, domain)
            if not privacy_urls:
                origin = record.get("homepage") or f"https://{domain}/"
                for path in COMMON_PRIVACY:
                    _remember(privacy_urls, urljoin(origin, path), domain)
        terms_urls: list[str] = []
        if not legal.get("automation_clause") and (terms_len < 2000 or terms_len >= 7500):
            if legal.get("terms_url"):
                _remember(terms_urls, legal["terms_url"], domain)
            elif terms_len < 2000:
                for url in known.get("terms") or []:
                    _remember(terms_urls, url, domain)
                if not terms_urls and privacy_len < 2000:
                    origin = record.get("homepage") or f"https://{domain}/"
                    for path in COMMON_TERMS:
                        _remember(terms_urls, urljoin(origin, path), domain)
        privacy_urls.sort(key=_policy_rank)
        if not privacy_urls and not terms_urls:
            continue
        jobs.append((domain, privacy_urls[:4], terms_urls[:3]))
    if limit:
        jobs = jobs[:limit]
    print(f"legal fetches: {len(jobs)}", flush=True)
    if not jobs:
        return
    fetcher = MappingFetcher()
    import threading
    lock = threading.Lock()

    def one(job: tuple) -> None:
        domain, privacy_urls, terms_urls = job
        parts = []
        privacy_url = None
        terms_url = None
        skip = None
        opened, skip = _first_policy(fetcher, privacy_urls, domain)
        if opened:
            privacy_url = opened["url"]
            parts.append(opened["text"])
            if not terms_urls:
                for href in opened.get("links") or []:
                    absolute = urljoin(opened["url"], href)
                    if same_host(domain, absolute):
                        _remember(terms_urls, absolute, domain)
        if terms_urls and not (opened and (clause(opened["text"]) or AUTO_BEFORE.search(opened["text"]))):
            terms, term_skip = _first_policy(fetcher, terms_urls[:3], domain)
            if terms:
                terms_url = terms["url"]
                parts.append(terms["text"])
            elif not opened:
                skip = skip or term_skip
        found = extract_legal("\n".join(parts))
        if privacy_url:
            found["privacy_policy_url"] = privacy_url
        if terms_url:
            found["terms_url"] = terms_url
        entry = {
            "domain": domain,
            "checked_at": utc_now(),
            "pass": "legal",
            "skipped": None if found else skip,
            "legal_update": found or None,
        }
        with lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        label = "legal" if found else (skip or "unread")
        print(f"{label} {domain}", flush=True)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(one, job) for job in jobs]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                print(f"error {type(exc).__name__}: {exc}", flush=True)


SEARCH_TEST_OUTCOMES = {
    "results", "submitted", "no_results", "unfilled", "challenge",
    "captcha", "http_error", "robots", "error",
}


def empty_search_test() -> dict:
    return {
        "outcome": None,
        "http_status": None,
        "path_prefix": None,
        "fields_sent": [],
        "missing": [],
        "page_kind": None,
        "result_count": None,
        "checked_at": None,
    }


def load_search_tests() -> dict[str, dict]:
    """Last search-test line per catalog id. The log is append-only."""
    done: dict[str, dict] = {}
    if not SEARCH_TEST_LOG.exists():
        return done
    for line in SEARCH_TEST_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("id"):
            done[item["id"]] = item
    return done


def attach_search_tests(records: dict[str, dict]) -> None:
    """Copy the latest synthetic search onto each broker. Untested brokers stay null."""
    tests = load_search_tests()
    for item in records.values():
        row = tests.get(item["id"])
        if not row:
            item["search_test"] = empty_search_test()
            continue
        outcome = row.get("outcome")
        if outcome not in SEARCH_TEST_OUTCOMES:
            outcome = None
        status = row.get("status")
        if not isinstance(status, int):
            status = None
        prefix = row.get("path_prefix") or None
        folded = prefix.lower() if isinstance(prefix, str) else ""
        if (
            status == 200
            and outcome == "submitted"
            and folded in {"results", "result", "namesearch", "name_search"}
            and row.get("page_kind") in {None, "results"}
        ):
            outcome = "results"
        elif outcome == "results" and folded in {"hc", "help", "support", "404", "licenses"}:
            outcome = "submitted"
        if isinstance(prefix, str) and re.fullmatch(r"alder[-_ ]?northwind", prefix, re.I):
            prefix = "lookup"
        elif not isinstance(prefix, str) or not prefix:
            prefix = None
        kind = row.get("page_kind")
        if kind not in {"results", "empty", "loader", "form", "other"}:
            kind = None
        count = row.get("result_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            count = None
        if kind == "empty" or outcome == "no_results":
            outcome = "no_results"
            kind = "empty"
            if count is None:
                count = 0
        elif outcome == "results" and kind is None:
            kind = "results"
        item["search_test"] = {
            "outcome": outcome,
            "http_status": status,
            "path_prefix": prefix[:40] if prefix else None,
            "fields_sent": [str(name) for name in (row.get("fields_sent") or []) if name][:40],
            "missing": [str(name) for name in (row.get("missing") or []) if name][:40],
            "page_kind": kind,
            "result_count": count,
            "checked_at": row.get("checked_at") if isinstance(row.get("checked_at"), str) else None,
        }


def compile_report(records: dict[str, dict], raw: dict[str, dict] | None = None) -> dict:
    if raw is None:
        raw = load_done(LOG_PATH)
    attach_submission(records, raw)
    attach_legal(records, raw)
    for item in records.values():
        annotate(item)
    attach_search_tests(records)
    mark_shared(records)
    counts = {
        "with_related_email": sum(1 for item in records.values() if any(row["confidence"] == "related_domain" for row in item["removal"]["emails"])),
        "with_form": sum(1 for item in records.values() if item["removal"]["form_fields"]),
        "with_email": sum(1 for item in records.values() if item["removal"]["emails"]),
        "with_extracted_email": sum(1 for item in records.values() if any(row["confidence"] == "extracted" for row in item["removal"]["emails"])),
        "with_stated_email": sum(1 for item in records.values() if any(row["confidence"] == "stated_removal" for row in item["removal"]["emails"])),
        "no_channel_remaining": sum(1 for item in records.values() if no_channel(item)),
        "with_search": sum(1 for item in records.values() if item["search"]["available"]),
        "with_search_fillable": sum(1 for item in records.values() if item["automation"]["search_fillable"]),
        "search_unscanned": sum(1 for item in records.values() if not item["automation"]["search_scanned"]),
        "fully_automatable": sum(1 for item in records.values() if item["automation"]["fully_automatable"]),
        "search_tested": sum(1 for item in records.values() if item["search_test"]["outcome"]),
        "search_results": sum(1 for item in records.values() if item["search_test"]["outcome"] == "results"),
        "search_submitted": sum(1 for item in records.values() if item["search_test"]["outcome"] == "submitted"),
        "search_no_results": sum(1 for item in records.values() if item["search_test"]["outcome"] == "no_results"),
        "search_unfilled": sum(1 for item in records.values() if item["search_test"]["outcome"] == "unfilled"),
        "search_challenge": sum(1 for item in records.values() if item["search_test"]["outcome"] == "challenge"),
        "search_result_count": sum(1 for item in records.values() if item["search_test"]["result_count"] is not None),
        "search_empty": sum(1 for item in records.values() if item["search_test"]["page_kind"] == "empty"),
        "search_page_results": sum(1 for item in records.values() if item["search_test"]["page_kind"] == "results"),
        "search_page_loader": sum(1 for item in records.values() if item["search_test"]["page_kind"] == "loader"),
        "search_page_form": sum(1 for item in records.values() if item["search_test"]["page_kind"] == "form"),
        "with_country": sum(1 for item in records.values() if item["location"]["country"]),
        "with_region": sum(1 for item in records.values() if item["location"]["region"]),
        "with_locality": sum(1 for item in records.values() if item["location"]["locality"]),
        "with_removal_action": sum(1 for item in records.values() if item["removal"]["form_action"]),
        "shared_removal": sum(1 for item in records.values() if item["automation"]["shared_removal_ids"]),
        "email_verification": sum(1 for item in records.values() if item["automation"]["email_verification"]),
        "id_document": sum(1 for item in records.values() if item["automation"]["id_document"]),
        "with_privacy": sum(1 for item in records.values() if item["legal"]["privacy_policy_url"]),
        "with_terms": sum(1 for item in records.values() if item["legal"]["terms_url"]),
        "with_automation_clause": sum(1 for item in records.values() if item["legal"]["automation_clause"]),
        "with_authorized_agent": sum(1 for item in records.values() if item["legal"]["authorized_agent"]),
        "with_residency": sum(1 for item in records.values() if item["legal"]["residency"]),
        "sells_personal_information": sum(1 for item in records.values() if item["legal"]["sells_personal_information"] is True),
        "does_not_sell": sum(1 for item in records.values() if item["legal"]["sells_personal_information"] is False),
        "children_not_directed": sum(1 for item in records.values() if item["legal"]["children_not_directed"]),
        "with_mx": sum(1 for item in records.values() if item["network"]["mx"]),
        "with_mail_provider": sum(1 for item in records.values() if item["network"]["mail_provider"]),
        "with_cdn": sum(1 for item in records.values() if item["network"]["cdn"]),
        "with_tls_org": sum(1 for item in records.values() if item["network"]["tls_org"]),
        "removal_offsite": sum(1 for item in records.values() if item["request"]["same_site"] is False),
        "with_hsts": sum(1 for item in records.values() if item["request"]["hsts"]),
        "with_form_enctype": sum(1 for item in records.values() if item["request"]["form_enctype"]),
        "with_field_detail": sum(1 for item in records.values() if any(field.get("multiple") is not None for field in [*item["removal"]["form_fields"], *item["search"]["inputs"]])),
        "with_field_label": sum(1 for item in records.values() if any(field.get("label") for field in [*item["removal"]["form_fields"], *item["search"]["inputs"]])),
        "with_field_constraint": sum(1 for item in records.values() if any(any(field.get(key) for key in ("inputmode", "pattern", "minlength", "maxlength", "min", "max", "step", "accept")) for field in [*item["removal"]["form_fields"], *item["search"]["inputs"]])),
        "with_submit_label": sum(1 for item in records.values() if item["removal"]["form_meta"]["submit_label"] or item["search"]["form_meta"]["submit_label"]),
        "with_hidden_names": sum(1 for item in records.values() if item["removal"]["form_meta"]["hidden_names"] or item["search"]["form_meta"]["hidden_names"]),
        "with_ipv6": sum(1 for item in records.values() if item["network"]["ipv6"]),
        "with_spf": sum(1 for item in records.values() if item["network"]["spf"]),
        "with_dmarc": sum(1 for item in records.values() if item["network"]["dmarc"]),
        "dmarc_reject": sum(1 for item in records.values() if item["network"]["dmarc"] == "reject"),
        "with_dkim": sum(1 for item in records.values() if item["network"]["dkim"]),
        "with_caa": sum(1 for item in records.values() if item["network"]["caa"]),
        "with_dnssec": sum(1 for item in records.values() if item["network"]["dnssec"]),
        "with_asn": sum(1 for item in records.values() if item["network"]["asn"]),
        "with_registrar": sum(1 for item in records.values() if item["network"]["registrar"]),
        "with_tls_version": sum(1 for item in records.values() if item["network"]["tls_version"]),
        "with_http3": sum(1 for item in records.values() if item["request"]["http3"]),
        "with_csp": sum(1 for item in records.values() if item["request"]["csp"]),
        "with_hsts_preload": sum(1 for item in records.values() if item["request"]["hsts_preload"]),
        "removal_captcha": sum(1 for item in records.values() if item["removal"]["captcha"]["provider"]),
        "removal_captcha_sitekey": sum(1 for item in records.values() if item["removal"]["captcha"]["sitekey"]),
        "removal_captcha_checked": sum(1 for item in records.values() if item["removal"]["captcha"]["checked"] is True),
        "search_captcha_widget": sum(1 for item in records.values() if item["search"].get("captcha_provider")),
        "search_captcha_sitekey": sum(1 for item in records.values() if item["search"].get("captcha_sitekey")),
        "search_captcha_checked": sum(1 for item in records.values() if item["search"].get("captcha_checked") is True),
        "captcha_blocks_form": sum(1 for item in records.values() if item["automation"]["captcha_blocks_form"]),
        "captcha_email_fallback": sum(1 for item in records.values() if item["automation"]["captcha_email_fallback"]),
        "removal_captcha_checkbox": sum(1 for item in records.values() if (item["removal"]["captcha"].get("detail") or {}).get("mode") == "checkbox"),
        "removal_captcha_invisible": sum(1 for item in records.values() if (item["removal"]["captcha"].get("detail") or {}).get("mode") == "invisible"),
        "removal_captcha_score": sum(1 for item in records.values() if (item["removal"]["captcha"].get("detail") or {}).get("mode") == "score"),
        "removal_captcha_action": sum(1 for item in records.values() if (item["removal"]["captcha"].get("detail") or {}).get("action")),
        "removal_captcha_theme": sum(1 for item in records.values() if (item["removal"]["captcha"].get("detail") or {}).get("theme")),
        "removal_captcha_enterprise": sum(1 for item in records.values() if (item["removal"]["captcha"].get("detail") or {}).get("enterprise") is True),
        "search_captcha_checkbox": sum(1 for item in records.values() if (item["search"].get("captcha_detail") or {}).get("mode") == "checkbox"),
        "search_captcha_invisible": sum(1 for item in records.values() if (item["search"].get("captcha_detail") or {}).get("mode") == "invisible"),
        "search_captcha_score": sum(1 for item in records.values() if (item["search"].get("captcha_detail") or {}).get("mode") == "score"),
        "removal_blockers": {
            kind: sum(1 for item in records.values() if item["automation"]["removal_blocker"] == kind)
            for kind in BLOCKER_WHY
        },
        "lookup_search": sum(1 for item in records.values() if item["category"] in LOOKUP and item["search"]["available"]),
        "lookup_search_fillable": sum(
            1 for item in records.values()
            if item["category"] in LOOKUP and item["automation"]["search_fillable"]
        ),
        "liveness": dict(sorted(Counter(item["liveness"] for item in records.values()).items())),
    }
    return {
        "schema_version": "4.0.0",
        "checked_at": utc_now(),
        "method": (
            "v3 records, with privacy and removal mailboxes on another domain kept as related_domain. "
            "Brokers whose loaded pages published no removal channel were crawled again for unfetched "
            "same-site opt-out and privacy URLs, then the catalog URL and homepage were opened once in a browser "
            "when those pages still showed no form. That recrawl honors robots.txt. A later pass reads the catalog URL "
            "and homepage of brokers whose robots.txt had disallowed the research client. A search pass reads "
            "homepages whose public search form was missing, including pages robots.txt had disallowed, and stores a name, phone, or address form. "
            "Those passes submit no form, no login, and no CAPTCHA. "
            "A legal pass re-reads stored privacy and terms text and fetches an unread privacy or terms page when robots.txt allows it. "
            "A network pass looks up public DNS and the site certificate, and reads response headers for the removal URL when robots.txt allows it. "
            "A later pass adds IPv6, SPF, DMARC, DKIM selectors, CAA, DNSSEC, the address block, certificate dates, RDAP registration, and the removal response's security headers. "
            "A captcha pass re-reads the removal URL and the search page for a widget. A browser reads the page when the static HTML has no widget and the page has a removal form, a stored CAPTCHA, or a public search form. "
            "A later pass reads the public widget settings, including whether it is a checkbox, an invisible check, or a score. "
            "A form pass re-reads stored search and removal forms for labels, limits, autocomplete, the submit button, and hidden field names. A browser opens when the static HTML does not contain the stored fields. Nothing is submitted. "
            "A search test then submits one synthetic query on each broker whose search and removal are both fillable. It does not consult robots.txt. Removal forms are not submitted and CAPTCHAs are not solved. "
            "search_test is on every broker. It stores the outcome, status, field names, the first path segment, the page kind, and a result count when the page states one. A loader page is followed to the count endpoint. The response body, record text, and listing URLs are not stored."
        ),
        "broker_count": len(records),
        "counts": counts,
        "brokers": records,
    }


def removal_blocker(item: dict) -> str | None:
    """Why this broker has no automatable removal path. None when a form or usable email exists."""
    if item["automation"]["removal_path"]:
        return None
    removal = item["removal"]
    channels = set(removal["channels"])
    if removal["form_fields"] and removal["captcha"]["provider"]:
        return "captcha_form"
    if removal["emails"] and all(row["confidence"] == "uncertain" for row in removal["emails"]):
        return "generic_inbox"
    if "phone" in channels and "postal_mail" in channels:
        return "phone_postal"
    if "official_portal" in channels or removal.get("portal"):
        return "portal"
    if "phone" in channels:
        return "phone"
    if "postal_mail" in channels:
        return "postal"
    live = item["liveness"]
    if live == "blocked":
        return "bot_wall"
    if live == "error":
        return "fetch_error"
    if live == "opt_out_missing":
        return "opt_out_gone"
    if live == "robots_disallowed":
        return "robots"
    if removal["captcha"]["provider"]:
        return "captcha_no_form"
    return "no_channel"


BLOCKER_WHY = {
    "captcha_form": "Removal form is behind a CAPTCHA widget and no usable removal email was found",
    "generic_inbox": "Only a generic inbox was found, and the page does not say to use it for removal",
    "phone_postal": "Phone and a mailing address are the only channels",
    "portal": "An official privacy portal is linked and its fields were not stored",
    "phone": "The only published channel is a phone number",
    "postal": "The only published channel is a mailing address",
    "bot_wall": "The opt-out URL returned a bot wall and a browser retry did not reveal a removal channel",
    "fetch_error": "The opt-out URL failed before a page was read",
    "opt_out_gone": "The catalog opt-out URL returned 404 or 410",
    "robots": "robots.txt disallows the client. A later read still found no removal channel",
    "captcha_no_form": "A CAPTCHA widget is on the removal page and no form fields or usable email were stored",
    "no_channel": "The page loaded and a deeper look still found no form, email, phone, postal address, or portal",
}


def write_blockers(report: dict) -> None:
    fields = [
        "id", "name", "domain", "category", "liveness", "blocker", "why",
        "captcha", "captcha_sitekey", "captcha_mode", "captcha_checked", "search_captcha", "captcha_blocks_form",
        "emails", "channels",
    ]
    ranked = sorted(
        (item for item in report["brokers"].values() if removal_blocker(item)),
        key=lambda item: (item["category"], item["name"].lower(), item["id"]),
    )
    with BLOCKERS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in ranked:
            kind = removal_blocker(item)
            writer.writerow({
                "id": item["id"],
                "name": item["name"],
                "domain": item["domain"],
                "category": item["category"],
                "liveness": item["liveness"],
                "blocker": kind,
                "why": BLOCKER_WHY[kind],
                "captcha": item["removal"]["captcha"]["provider"] or "",
                "captcha_sitekey": item["removal"]["captcha"]["sitekey"] or "",
                "captcha_mode": (item["removal"]["captcha"].get("detail") or {}).get("mode") or "",
                "captcha_checked": "" if item["removal"]["captcha"]["checked"] is None else str(item["removal"]["captcha"]["checked"]).lower(),
                "search_captcha": item["search"]["captcha_provider"] or "",
                "captcha_blocks_form": "true" if item["automation"]["captcha_blocks_form"] else "",
                "emails": ";".join(row["email"] for row in item["removal"]["emails"]),
                "channels": ";".join(item["removal"]["channels"]),
            })


def write_outputs(report: dict) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    fields = [
        "id", "name", "domain", "category", "liveness", "channels", "email", "email_confidence",
        "captcha", "form_fields", "search_available", "search_fillable", "search_fields",
        "removal_path", "fully_automatable", "search_outcome", "search_http_status", "search_path_prefix",
        "search_page_kind", "search_result_count",
        "country", "region", "locality", "coverage",
        "removal_action", "search_required", "removal_required", "shared_removal",
        "privacy_policy", "terms", "sells", "authorized_agent", "children_not_directed",
        "mail_provider", "cdn", "removal_host", "same_site",
        "asn", "registrar", "dmarc", "dnssec", "tls_version", "http3",
        "removal_submit", "search_submit", "hidden_fields",
        "blocker", "captcha_sitekey", "captcha_mode", "search_captcha", "search_captcha_mode", "captcha_blocks_form", "captcha_email_fallback",
    ]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in report["brokers"].values():
            emails = item["removal"]["emails"]
            writer.writerow({
                "id": item["id"],
                "name": item["name"],
                "domain": item["domain"],
                "category": item["category"],
                "liveness": item["liveness"],
                "channels": ";".join(item["removal"]["channels"]),
                "email": emails[0]["email"] if emails else "",
                "email_confidence": emails[0]["confidence"] if emails else "",
                "captcha": item["removal"]["captcha"]["provider"] or "",
                "form_fields": len(item["removal"]["form_fields"]),
                "search_available": "" if item["search"]["available"] is None else str(item["search"]["available"]).lower(),
                "search_fillable": str(item["automation"]["search_fillable"]).lower(),
                "search_fields": ";".join(item["automation"]["search_fields"]),
                "removal_path": item["automation"]["removal_path"] or "",
                "fully_automatable": str(item["automation"]["fully_automatable"]).lower(),
                "search_outcome": item["search_test"]["outcome"] or "",
                "search_http_status": "" if item["search_test"]["http_status"] is None else item["search_test"]["http_status"],
                "search_path_prefix": item["search_test"]["path_prefix"] or "",
                "search_page_kind": item["search_test"]["page_kind"] or "",
                "search_result_count": "" if item["search_test"]["result_count"] is None else item["search_test"]["result_count"],
                "country": item["location"]["country"] or "",
                "region": item["location"]["region"] or "",
                "locality": item["location"]["locality"] or "",
                "coverage": ";".join(item["location"]["coverage"]),
                "removal_action": item["removal"]["form_action"] or "",
                "search_required": ";".join(item["automation"]["search_required"]),
                "removal_required": ";".join(item["automation"]["removal_required"]),
                "shared_removal": ";".join(item["automation"]["shared_removal_ids"]),
                "privacy_policy": item["legal"]["privacy_policy_url"] or "",
                "terms": item["legal"]["terms_url"] or "",
                "sells": "" if item["legal"]["sells_personal_information"] is None else str(item["legal"]["sells_personal_information"]).lower(),
                "authorized_agent": "true" if item["legal"]["authorized_agent"] else "",
                "children_not_directed": "true" if item["legal"]["children_not_directed"] else "",
                "mail_provider": item["network"]["mail_provider"] or "",
                "cdn": item["network"]["cdn"] or "",
                "removal_host": item["request"]["removal_host"] or "",
                "same_site": "" if item["request"]["same_site"] is None else str(item["request"]["same_site"]).lower(),
                "asn": item["network"]["asn"] or "",
                "registrar": item["network"]["registrar"] or "",
                "dmarc": item["network"]["dmarc"] or "",
                "dnssec": "" if item["network"]["dnssec"] is None else str(item["network"]["dnssec"]).lower(),
                "tls_version": item["network"]["tls_version"] or "",
                "http3": "" if item["request"]["http3"] is None else str(item["request"]["http3"]).lower(),
                "removal_submit": item["removal"]["form_meta"]["submit_label"] or "",
                "search_submit": item["search"]["form_meta"]["submit_label"] or "",
                "hidden_fields": len(item["removal"]["form_meta"]["hidden_names"]) + len(item["search"]["form_meta"]["hidden_names"]),
                "blocker": item["automation"]["removal_blocker"] or "",
                "captcha_sitekey": item["removal"]["captcha"]["sitekey"] or "",
                "captcha_mode": (item["removal"]["captcha"].get("detail") or {}).get("mode") or "",
                "search_captcha": item["search"]["captcha_provider"] or "",
                "search_captcha_mode": (item["search"].get("captcha_detail") or {}).get("mode") or "",
                "captcha_blocks_form": "true" if item["automation"]["captcha_blocks_form"] else "",
                "captcha_email_fallback": "true" if item["automation"]["captcha_email_fallback"] else "",
            })
    counts = report["counts"]
    lines = [
        "# v4 broker profiles",
        "",
        "v4 starts from the v3 record for every catalog broker. v3 stays the full public-page scan.",
        "",
        f"Check date: {report['checked_at']}. Brokers: {report['broker_count']}.",
        "",
        report["method"],
        "",
        "The file is [`data/v4/brokers.json`](../data/v4/brokers.json). A flat view is [`data/v4/brokers.csv`](../data/v4/brokers.csv). Schema: [`data/schema/broker-v4.schema.json`](../data/schema/broker-v4.schema.json). Rebuild with `python scripts/profiles/scan_v4.py`. A finished log can be republished with `python scripts/profiles/scan_v4.py --compile-only`.",
        "",
        "| Fact | Brokers |",
        "| --- | ---: |",
        f"| Related-domain privacy email | {counts['with_related_email']} |",
        f"| Any removal email | {counts['with_email']} |",
        f"| On-domain extracted email | {counts['with_extracted_email']} |",
        f"| Generic inbox the page says to use for removal | {counts['with_stated_email']} |",
        f"| Removal form fields | {counts['with_form']} |",
        f"| Still no channel after the recrawl | {counts['no_channel_remaining']} |",
        f"| Public search form | {counts['with_search']} |",
        f"| Search a machine can fill | {counts['with_search_fillable']} |",
        f"| Lookup search form | {counts['lookup_search']} |",
        f"| Lookup search a machine can fill | {counts['lookup_search_fillable']} |",
        f"| Search not established | {counts['search_unscanned']} |",
        f"| Search and removal both fillable | {counts['fully_automatable']} |",
        f"| Synthetic search submitted | {counts['search_tested']} |",
        f"| Synthetic search reached a results page | {counts['search_results']} |",
        f"| Synthetic search submitted without a results page | {counts['search_submitted']} |",
        f"| Synthetic search found nobody | {counts['search_no_results']} |",
        f"| Synthetic search left unfilled | {counts['search_unfilled']} |",
        f"| Synthetic search hit a bot wall | {counts['search_challenge']} |",
        f"| Search page stated a result count | {counts['search_result_count']} |",
        f"| Search page said nobody matched | {counts['search_empty']} |",
        f"| Search page is a results page | {counts['search_page_results']} |",
        f"| Search stopped on a loader | {counts['search_page_loader']} |",
        f"| Search stayed on the form | {counts['search_page_form']} |",
        f"| Country established | {counts['with_country']} |",
        f"| State or province established | {counts['with_region']} |",
        f"| City established | {counts['with_locality']} |",
        f"| Removal form submit URL | {counts['with_removal_action']} |",
        f"| Shares a removal URL | {counts['shared_removal']} |",
        f"| Opt-out page asks for email verification | {counts['email_verification']} |",
        f"| Opt-out page asks for an ID document | {counts['id_document']} |",
        f"| Privacy policy URL | {counts['with_privacy']} |",
        f"| Terms URL | {counts['with_terms']} |",
        f"| Automation prohibited on a page we read | {counts['with_automation_clause']} |",
        f"| Authorized agent accepted | {counts['with_authorized_agent']} |",
        f"| Residency named | {counts['with_residency']} |",
        f"| Page says it sells personal information | {counts['sells_personal_information']} |",
        f"| Page says it does not sell personal information | {counts['does_not_sell']} |",
        f"| Page says it is not directed at children | {counts['children_not_directed']} |",
        f"| Mail exchange found | {counts['with_mx']} |",
        f"| Mail provider named by the exchange | {counts['with_mail_provider']} |",
        f"| CDN named by response headers | {counts['with_cdn']} |",
        f"| Certificate names an organization | {counts['with_tls_org']} |",
        f"| IPv6 address | {counts['with_ipv6']} |",
        f"| SPF record | {counts['with_spf']} |",
        f"| DMARC policy | {counts['with_dmarc']} |",
        f"| DMARC reject | {counts['dmarc_reject']} |",
        f"| DKIM selector answered | {counts['with_dkim']} |",
        f"| CAA issuer published | {counts['with_caa']} |",
        f"| DNSSEC key | {counts['with_dnssec']} |",
        f"| Network ASN | {counts['with_asn']} |",
        f"| Registrar from RDAP | {counts['with_registrar']} |",
        f"| TLS version | {counts['with_tls_version']} |",
        f"| Removal response advertises HTTP/3 | {counts['with_http3']} |",
        f"| Removal response sends a content security policy | {counts['with_csp']} |",
        f"| HSTS asks to be preloaded | {counts['with_hsts_preload']} |",
        f"| Removal URL is on another host | {counts['removal_offsite']} |",
        f"| Removal response sends HSTS | {counts['with_hsts']} |",
        f"| Removal form encoding stated | {counts['with_form_enctype']} |",
        f"| Stored form re-read for control attributes | {counts['with_field_detail']} |",
        f"| Form control has a label | {counts['with_field_label']} |",
        f"| Form control states a limit or mode | {counts['with_field_constraint']} |",
        f"| Submit button text stored | {counts['with_submit_label']} |",
        f"| Hidden field names stored | {counts['with_hidden_names']} |",
        f"| Removal page has a CAPTCHA widget | {counts['removal_captcha']} |",
        f"| Removal CAPTCHA site key stored | {counts['removal_captcha_sitekey']} |",
        f"| Removal page loaded by the CAPTCHA pass | {counts['removal_captcha_checked']} |",
        f"| Search page has a CAPTCHA widget | {counts['search_captcha_widget']} |",
        f"| Search CAPTCHA site key stored | {counts['search_captcha_sitekey']} |",
        f"| Search page loaded by the CAPTCHA pass | {counts['search_captcha_checked']} |",
        f"| Removal form blocked by a CAPTCHA | {counts['captcha_blocks_form']} |",
        f"| Usable email stands in for a CAPTCHA form | {counts['captcha_email_fallback']} |",
        f"| Removal CAPTCHA is a checkbox | {counts['removal_captcha_checkbox']} |",
        f"| Removal CAPTCHA is invisible | {counts['removal_captcha_invisible']} |",
        f"| Removal CAPTCHA is a score | {counts['removal_captcha_score']} |",
        f"| Removal CAPTCHA names an action | {counts['removal_captcha_action']} |",
        f"| Removal CAPTCHA names a theme | {counts['removal_captcha_theme']} |",
        f"| Removal CAPTCHA is enterprise | {counts['removal_captcha_enterprise']} |",
        f"| Search CAPTCHA is a checkbox | {counts['search_captcha_checkbox']} |",
        f"| Search CAPTCHA is invisible | {counts['search_captcha_invisible']} |",
        f"| Search CAPTCHA is a score | {counts['search_captcha_score']} |",
        "",
        "Brokers with no automatable removal path are listed in [`data/v4/removal-blockers.csv`](../data/v4/removal-blockers.csv). `automation.removal_blocker` is null when a captcha-free form or a usable email exists. Otherwise it is one of the reasons below.",
        "",
        "| Blocker | Brokers | Why |",
        "| --- | ---: | --- |",
        *[
            f"| `{kind}` | {counts['removal_blockers'][kind]} | {BLOCKER_WHY[kind]} |"
            for kind in BLOCKER_WHY
        ],
        "",
        "Each broker keeps every v3 field. `automation` is computed from those fields: `search_scanned` is true once a loaded page established whether a public name, phone, or address form exists. `search_fillable` means that form is stored and its page has no CAPTCHA. `removal_path` is `form` when the removal form has stored fields and no CAPTCHA, otherwise `email` when a usable removal address is stored. `fully_automatable` is both. `usable_emails` keeps `extracted`, `related_domain`, and `stated_removal` addresses. `uncertain` support and info inboxes stay off that list. `search_required` and `removal_required` are the canonical fields the HTML marks required. `removal.form_action` is the form's submit URL. `form_meta` is the form element: its id, name, autocomplete, whether it sets novalidate, the submit button text, and hidden field names. Hidden values are not stored. Each control also keeps `id`, `label`, `inputmode`, `pattern`, `minlength`, `maxlength`, `min`, `max`, `step`, `accept`, `multiple`, `disabled`, `readonly`, `checked`, and `value` when a later read of that form saw them. `value` is the HTML default, not something a person typed, and password, file, email, and token defaults are left null. A longer dropdown list already stored is kept. Null means that form was not read. `shared_removal_ids` lists other catalog brokers that publish the same removal URL. `email_verification` and `id_document` are true only when an opt-out or privacy page says the request needs that check.",
        "",
        "`location` is on every broker. `country` and `region` come from a California Privacy Protection Agency filing matched by domain or exact business name, a country-code domain, a state, province, or country in the site name, a single country named on a page, a city-state-ZIP on a page, or a search form whose choices are states or provinces. `coverage` keeps the page's residency codes plus that place. `search_regions` lists the state and province codes stored on the search form. `locality` is the city from a filing or from that page address. Null means none of those sources established the fact.",
        "",
        "`legal.sells_personal_information` is true only when a privacy or terms page says the company sells personal information, and false only when the page says it does not. The sell flag stays null until a page states that. `children_not_directed` is true only when the page says the service is not directed at children or does not knowingly collect from children under 13. `authorized_agent` is true when the page says an authorized agent or representative may submit the request. Empty legal fields stay null.",
        "",
        "`network` is the public DNS, certificate, and registration record for the catalog domain. `ipv6`, `spf`, `dmarc`, `dkim`, `caa`, and `dnssec` come from DNS. `dkim` lists selectors that answered (`google`, `selector1`, `selector2`, `default`); the key itself is not stored. `dmarc` is only the policy (`none`, `quarantine`, or `reject`). `asn`, `asn_country`, and `prefix` come from the address block of the first IPv4 address. `registrar`, `registered`, `registration_expires`, and `domain_status` come from RDAP and do not include a person's name or email. `tls_version`, `tls_not_before`, and `tls_not_after` come from the certificate handshake. `request` is how the stored removal URL is reached. Header facts (`hsts_max_age`, `hsts_subdomains`, `hsts_preload`, `http_version`, `http3`, `frame_options`, `nosniff`, `referrer_policy`, `csp`, `powered_by`, `cookie_names`) are taken from that response when robots.txt allows it. Cookie values are not stored. A robots.txt disallow leaves those header facts null.",
        "",
        "A CAPTCHA provider is stored only when the removal or search page contains the widget: the script, the iframe, or the widget element. A privacy policy that merely names a CAPTCHA is not a widget. `removal.captcha.checked` is true when that pass loaded the removal URL, false when robots.txt, a challenge, or an error stopped the read, and null when the pass did not run. `checked_url` is the page that loaded, including a page with no widget. `search.captcha_checked` is the same result for a public search page. `removal.captcha.detail` and `search.captcha_detail` are the public widget settings: `mode` is `checkbox`, `invisible`, or `score`; `size`, `theme`, `action`, `badge`, `language`, and `appearance` are stored only when the widget states them; `enterprise` is true when the script is reCAPTCHA Enterprise; `api_domain` and `script_path` are the widget script; `token_field` is the response field name when that field is in the page; `callback` is the widget's `data-callback`. Null means the widget did not state that fact. `automation.removal_captcha` is true when the removal page has a widget. `captcha_blocks_form` is true when stored form fields sit behind that widget. `captcha_email_fallback` is true when that form is blocked and a usable email is the removal path instead. The site key is the public key in the page. Nothing here solves a CAPTCHA.",
        "",
        "`related_domain` is a privacy, opt-out, removal, or DPO mailbox published on a page we read, whose host is not the catalog domain. `stated_removal` is a generic inbox the same page tells people to use for a deletion, opt-out, or do-not-sell request. Other support@ and info@ addresses stay `uncertain`.",
        "",
        "The recrawl covers brokers whose v3 pages had loaded and still had no form, email, phone, postal address, portal, or CAPTCHA. It requests up to four unfetched same-site opt-out or privacy URLs, then opens the catalog URL and the homepage in a browser when a form is still missing.",
        "",
        "A machine can fill a search when the stored inputs are present and that page has no CAPTCHA. The form has to ask for a name, phone number, or address. `search.captcha_provider` is the CAPTCHA on the search page. It is separate from the removal CAPTCHA. Lookup search is people-search, background-checks, and public-records. A search control the label rules left untagged can take a canonical value from [`research/manual/v4-search-fields.json`](../research/manual/v4-search-fields.json), which records the page evidence for each one. A control the scan already tagged keeps its value. A search form the scan missed or stored wrong (robots.txt kept it off the page, the controls have no name attribute, or another form was stored) can be replaced from [`research/manual/v4-search-forms.json`](../research/manual/v4-search-forms.json), which keeps the form read from the rendered page, where a synthetic search landed, and the date. `url_pattern` holds a result URL the page builds in script, with `{control}` placeholders. An entry there marked `not_a_person_search` clears a stored form that is not a search for a person: a contact, demo, or opt-out form, a site search, or a form that only forwards the name to another company through an affiliate or tracking link. An entry marked `paywall` keeps the form and sets `search.paywall` when a synthetic search returned no records without an account or a purchase.",
        "",
        "`search_test` is on every broker. `outcome` is `results` when the response is a results page, `no_results` when the site said nobody matched, `submitted` when the form was accepted and the page was still a form, a loader, or something else, `unfilled` when a required field had no synthetic value, and `challenge` when the response was a bot wall. `page_kind` is `results`, `empty`, `loader`, `form`, or `other`. `result_count` is the number the page stated, or 0 when it stated that nobody matched, and null when the page stated no number. `http_status` is the response status. `path_prefix` is the first segment of the response path. `fields_sent` and `missing` are field names only. Brokers that were not tested keep every value null. The query, the response body, record text, and listing URLs are not stored.",
        "",
    ]
    DOC_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_blockers(report)
    print(json.dumps(counts, indent=2), flush=True)


def publish() -> None:
    records = base_records()
    raw = load_done(LOG_PATH)
    apply_log(records, raw)
    write_outputs(compile_report(records, raw))


OPT_JS = """
() => [...document.querySelectorAll("a")]
  .filter((el) => /opt[- ]?out|do not sell|privacy request|remove my|delete my|your privacy choices|donotsell/i.test(el.textContent || ""))
  .map((el) => el.href)
  .filter((href) => href && href.startsWith("http"))
  .slice(0, 3)
"""


def crawl_missing(limit: int) -> None:
    from playwright.async_api import async_playwright

    records = base_records()
    domains = sorted({item["domain"] for item in records.values() if no_channel(item)})
    print(f"no-channel domains: {len(domains)}", flush=True)
    if limit:
        domains = domains[:limit]
    done = load_done(LOG_PATH)
    pending = [domain for domain in domains if domain not in done]
    print(f"pending: {len(pending)}", flush=True)
    if not pending:
        return
    maps = json.loads(MAPS_PATH.read_text(encoding="utf-8")).get("domains") or {}
    parents = load_parents()
    fetcher = MappingFetcher()
    catalog_url = {}
    home_url = {}
    for item in records.values():
        catalog_url.setdefault(item["domain"], item["removal"]["url"])
        home_url.setdefault(item["domain"], item.get("homepage") or f"https://{item['domain']}/")

    async def open_url(page, url: str):
        if not await asyncio.to_thread(fetcher.allowed, url):
            return None
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            return None
        status = response.status if response else None
        if status in (404, 410):
            return None
        await page.wait_for_timeout(1500)
        data = await page.evaluate(RENDER_JS)
        if _challenge(data.get("title") or "", data.get("text") or ""):
            await page.wait_for_timeout(5000)
            data = await page.evaluate(RENDER_JS)
            if _challenge(data.get("title") or "", data.get("text") or ""):
                return None
        title = data.get("title") or ""
        text = data.get("text") or ""
        if _hard_error(title, text) or (not title and len(text) < 40):
            return None
        links = await page.evaluate(OPT_JS)
        return {"status": status, "final_url": page.url, "data": data, "links": links or []}

    async def one(browser, domain: str) -> None:
        pages = []
        found = False
        for url in candidate_urls(domain, next(item for item in records.values() if item["domain"] == domain), maps.get(domain) or {}):
            res = await asyncio.to_thread(fetcher.get, url)
            if res.get("skipped") or res.get("error"):
                continue
            parsed = analyze(res, domain, parents)
            if parsed.get("liveness") != "alive":
                continue
            parsed["role"] = classify_role(parsed.get("final_url") or url, parsed.get("title") or "")
            pages.append(slim_page(parsed))
            if pick_removal_form(parsed) or any(core_privacy(row["email"]) for row in parsed.get("emails") or []):
                found = True
                break
        if not found:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
                ),
                locale="en-CA",
                viewport={"width": 1366, "height": 768},
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = await context.new_page()
            try:
                urls = [catalog_url.get(domain), home_url.get(domain)]
                links: list[str] = []
                for url in urls:
                    if not url:
                        continue
                    opened = await open_url(page, url)
                    if not opened or not same_host(domain, opened["final_url"]):
                        links = (opened or {}).get("links") or links
                        continue
                    signals = signals_from_render(opened["data"], domain, parents, opened["final_url"])
                    built = page_from_signals(url, opened["final_url"], opened.get("status"), signals)
                    pages.append(slim_page(built))
                    links = opened.get("links") or links
                    if pick_removal_form(built) or any(core_privacy(row["email"]) for row in signals["emails"]):
                        found = True
                        break
                if not found:
                    for link in links[:1]:
                        if not same_host(domain, link):
                            continue
                        opened = await open_url(page, link)
                        if not opened or not same_host(domain, opened["final_url"]):
                            continue
                        signals = signals_from_render(opened["data"], domain, parents, opened["final_url"])
                        built = page_from_signals(link, opened["final_url"], opened.get("status"), signals)
                        pages.append(slim_page(built))
                        if pick_removal_form(built):
                            found = True
                        break
            finally:
                await context.close()
        entry = {"domain": domain, "checked_at": utc_now(), "found_form": found, "pages": pages}
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"{'form' if found else 'none'} {domain} pages={len(pages)}", flush=True)

    async def run() -> None:
        sem = asyncio.Semaphore(3)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])

            async def wrapped(domain: str) -> None:
                async with sem:
                    try:
                        await one(browser, domain)
                    except Exception as exc:
                        print(f"error {domain} {type(exc).__name__}: {exc}", flush=True)
                    await asyncio.sleep(0.2)

            await asyncio.gather(*(wrapped(domain) for domain in pending))
            await browser.close()

    asyncio.run(run())


def _field_items(form: dict) -> list[dict]:
    kept = []
    for field in search_inputs(form):
        item = {
            "name": field.get("name") or "",
            "type": field.get("type") or "text",
            "canonical": field.get("canonical"),
            "required": bool(field.get("required")),
            "autocomplete": field.get("autocomplete"),
            "placeholder": field.get("placeholder"),
        }
        if field.get("options"):
            item["options"] = field["options"]
        kept.append(item)
    return kept


LEAD_FORM = re.compile(
    r"\bcompany\b|\borganization\b|job title|work email|business email|\bmessage\b|how did you hear|terms of service|i agree",
    re.I,
)
DIRECTORY_SEARCH = re.compile(r"business name|what service do you need|find a business|for example:", re.I)
PEOPLE_URL = re.compile(r"people-search|people_search|name_search|/name/|person-search|name/landing", re.I)


def _usable_search(domain: str, update: dict) -> bool:
    page_url = update.get("page_url") or ""
    if not same_host(domain, page_url):
        return False
    inputs = [field for field in (update.get("inputs") or []) if field.get("type") != "hidden"]
    if not inputs:
        return False
    blob = " ".join(
        f"{field.get('placeholder') or ''} {field.get('name') or ''} {field.get('canonical') or ''}"
        for field in inputs
    )
    if re.search(r"\bip address\b", blob, re.I):
        return False
    canons = {field.get("canonical") for field in inputs}
    person = canons & {"first_name", "last_name", "full_name", "phone", "street_address"}
    if LEAD_FORM.search(blob):
        return bool(person and PEOPLE_URL.search(page_url))
    if DIRECTORY_SEARCH.search(blob) and not (canons & {"first_name", "last_name", "phone"}):
        return False
    if person <= {"full_name"} and all(
        re.fullmatch(r"search.*", f"{field.get('placeholder') or ''} {field.get('name') or ''}".strip(), re.I)
        for field in inputs
    ):
        return False
    if person:
        return True
    return bool(PERSON_SEARCH.search(blob))


def _search_update(domain: str, page: dict) -> dict | None:
    page_url = page.get("final_url") or page.get("url") or ""
    if not page_url or not same_host(domain, page_url) or OPT_OUT_URL.search(page_url):
        return None
    text = page.get("text") or ""
    for form in page.get("forms") or []:
        if not is_search_form(form, page_url, text):
            continue
        labels = " ".join(
            f"{field.get('placeholder') or ''} {field.get('name') or ''} {field.get('canonical') or ''}"
            for field in form.get("inputs") or []
        )
        if not PERSON_SEARCH.search(f"{labels} {text[:600]}"):
            continue
        return {
            "available": True,
            "method": "http_form_post" if form.get("method") == "post" else "http_get",
            "page_url": page_url,
            "form_action": form.get("action"),
            "inputs": _field_items(form),
            "url_pattern": search_pattern(form),
            "captcha_provider": page.get("captcha_provider"),
        }
    return None


def _real_page(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return ".well-known/sgcaptcha" not in lowered and "/cdn-cgi/challenge-platform/" not in lowered


def _none_update(page_url: str) -> dict:
    return {"available": False, "method": "none", "page_url": page_url, "captcha_provider": None}


def crawl_search(limit: int) -> None:
    """Read lookup homepages that have no stored search form. Does not submit a query."""
    from playwright.async_api import async_playwright

    class OpenFetcher(MappingFetcher):
        def allowed(self, url: str) -> bool:
            return True

    records = base_records()
    apply_log(records, load_done(LOG_PATH))
    by_domain: dict[str, list[dict]] = {}
    for item in records.values():
        by_domain.setdefault(item["domain"], []).append(item)
    domains = sorted(
        domain for domain, group in by_domain.items()
        if any(row["liveness"] != "dead" and row["search"]["available"] is not True for row in group)
    )
    last_outcome: dict[str, str] = {}
    if LOG_PATH.exists():
        with LOG_PATH.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("pass") == "search" and item.get("domain"):
                    last_outcome[item["domain"]] = item.get("outcome") or ""
    # Retry a domain whose last search pass never loaded a page. A finished "none" or "form" stays.
    pending = [domain for domain in domains if last_outcome.get(domain) in (None, "unread")]
    print(f"domains missing a search form: {len(domains)}; pending: {len(pending)}", flush=True)
    if limit:
        pending = pending[:limit]
    if not pending:
        return
    raw = load_done(LOG_PATH)
    parents = load_parents()
    fetcher = OpenFetcher()

    def urls_for(domain: str, record: dict) -> list[str]:
        pool = [
            record.get("homepage") or f"https://{domain}/",
            f"https://{domain}/",
            f"https://www.{domain}/",
        ]
        pool.extend(f"https://{domain}{path}" for path in SEARCH_PATHS)
        pool.extend(f"https://www.{domain}{path}" for path in SEARCH_PATHS)
        chosen = []
        seen = set()
        for url in pool:
            if not url or not url.startswith("http"):
                continue
            key = norm_url(url)
            if key in seen or not same_host(domain, url):
                continue
            seen.add(key)
            chosen.append(url)
            if len(chosen) >= 6:
                break
        return chosen

    async def open_url(page, url: str):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            return None
        status = response.status if response else None
        if status in (404, 410):
            return None
        await page.wait_for_timeout(1500)
        data = await page.evaluate(RENDER_JS)
        if _challenge(data.get("title") or "", data.get("text") or ""):
            await page.wait_for_timeout(5000)
            data = await page.evaluate(RENDER_JS)
            if _challenge(data.get("title") or "", data.get("text") or ""):
                return {"challenge": True, "final_url": page.url}
        title = data.get("title") or ""
        text = data.get("text") or ""
        if _hard_error(title, text) or (not title and len(text) < 40):
            return None
        return {"status": status, "final_url": page.url, "data": data}

    async def one(browser, domain: str, lock: asyncio.Lock) -> None:
        group = by_domain[domain]
        record = group[0]
        update = None
        loaded = None
        for page in (raw.get(domain) or {}).get("pages") or []:
            update = _search_update(domain, page)
            if update and update.get("inputs"):
                break
            update = None
        if not update:
            for url in urls_for(domain, record):
                res = await asyncio.to_thread(fetcher.get, url)
                if res.get("skipped") or res.get("error"):
                    continue
                parsed = analyze(res, domain, parents)
                if parsed.get("liveness") != "alive":
                    continue
                loaded = parsed.get("final_url") or url
                update = _search_update(domain, parsed)
                if update and update.get("inputs"):
                    break
                update = None
        if not (update and update.get("inputs")):
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
                ),
                locale="en-CA",
                viewport={"width": 1366, "height": 768},
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = await context.new_page()
            try:
                follow: list[str] = []
                for url in urls_for(domain, record)[:4]:
                    opened = await open_url(page, url)
                    if not opened:
                        continue
                    if opened.get("challenge"):
                        continue
                    if not same_host(domain, opened["final_url"]):
                        continue
                    signals = signals_from_render(opened["data"], domain, parents, opened["final_url"])
                    built = page_from_signals(url, opened["final_url"], opened.get("status"), signals)
                    loaded = opened["final_url"]
                    update = _search_update(domain, built)
                    follow = [link for link in (opened["data"].get("people") or []) if same_host(domain, link)]
                    if update and update.get("inputs"):
                        break
                    update = None
                if not (update and update.get("inputs")):
                    for link in follow[:2]:
                        opened = await open_url(page, link)
                        if not opened or opened.get("challenge") or not same_host(domain, opened["final_url"]):
                            continue
                        signals = signals_from_render(opened["data"], domain, parents, opened["final_url"])
                        built = page_from_signals(link, opened["final_url"], opened.get("status"), signals)
                        loaded = opened["final_url"]
                        update = _search_update(domain, built)
                        if update and update.get("inputs"):
                            break
                        update = None
            finally:
                await context.close()
        if not (update and update.get("inputs")):
            if loaded and not _real_page(loaded):
                loaded = None
            update = _none_update(loaded) if loaded else None
        outcome = "form" if update and update.get("available") else ("none" if update else "unread")
        entry = {
            "domain": domain,
            "checked_at": utc_now(),
            "pass": "search",
            "outcome": outcome,
            "search_update": update,
        }
        async with lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"{outcome} {domain}", flush=True)

    async def run() -> None:
        sem = asyncio.Semaphore(3)
        lock = asyncio.Lock()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])

            async def wrapped(domain: str) -> None:
                async with sem:
                    try:
                        await one(browser, domain, lock)
                    except Exception as exc:
                        print(f"error {domain} {type(exc).__name__}: {exc}", flush=True)
                    await asyncio.sleep(0.2)

            await asyncio.gather(*(wrapped(domain) for domain in pending))
            await browser.close()

    asyncio.run(run())


def crawl_network(limit: int) -> None:
    """Look up DNS and the certificate, and read removal-URL headers when robots.txt allows."""
    records = base_records()
    raw = load_done(LOG_PATH)
    by_domain: dict[str, dict] = {}
    for record in records.values():
        by_domain.setdefault(record["domain"], record)
    jobs = []
    for domain, record in by_domain.items():
        if (raw.get(domain) or {}).get("network_done"):
            continue
        target = record["removal"].get("final_url") or record["removal"].get("url") or record.get("homepage") or f"https://{domain}/"
        jobs.append((domain, target))
    if limit:
        jobs = jobs[:limit]
    print(f"network lookups: {len(jobs)}", flush=True)
    if not jobs:
        return
    fetcher = MappingFetcher()
    import threading
    lock = threading.Lock()

    def one(job: tuple) -> None:
        domain, url = job
        addresses = _addresses(domain)
        mx = _dns_query(domain, 15)
        nameservers = _dns_query(domain, 2)
        tls = _tls_facts(domain) if addresses else {"tls_org": None, "tls_issuer": None, "tls_names": []}
        peeked = _peek_request(fetcher, url)
        update = {
            "checked": True,
            "addresses": addresses,
            "mx": mx or [],
            "mail_provider": _host_provider(mx or [], MAIL_HOSTS),
            "nameservers": nameservers or [],
            "dns_provider": _host_provider(nameservers or [], DNS_HOSTS),
            "cdn": peeked.get("cdn"),
            "server": peeked.get("server"),
            "tls_org": tls.get("tls_org"),
            "tls_issuer": tls.get("tls_issuer"),
            "tls_names": tls.get("tls_names") or [],
            "form_enctype": peeked.get("form_enctype"),
            "content_type": peeked.get("content_type"),
            "hsts": peeked.get("hsts"),
            "redirect_count": peeked.get("redirect_count"),
        }
        entry = {
            "domain": domain,
            "checked_at": utc_now(),
            "pass": "network",
            "network_update": update,
        }
        with lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        label = update["mail_provider"] or update["cdn"] or ("mx" if update["mx"] else "network")
        print(f"{label} {domain}", flush=True)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(one, job) for job in jobs]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                print(f"error {type(exc).__name__}: {exc}", flush=True)


def crawl_network_extra(limit: int) -> None:
    """Add IPv6, mail policy, CAA, DNSSEC, ASN, certificate dates, RDAP, and security headers."""
    records = base_records()
    raw = load_done(LOG_PATH)
    apply_log(records, raw)
    by_domain: dict[str, dict] = {}
    for record in records.values():
        by_domain.setdefault(record["domain"], record)
    jobs = []
    for domain, record in by_domain.items():
        done = raw.get(domain) or {}
        if done.get("network_extra_done"):
            extra = done.get("network_extra") or {}
            if extra.get("registrar") or done.get("rdap_retried_again"):
                continue
            tld = domain.rsplit(".", 1)[-1]
            if tld in {"us", "io", "co", "eu", "neustar"}:
                continue
            jobs.append((domain, None, None, extra))
            continue
        url = record["removal"].get("final_url") or record["removal"].get("url") or record.get("homepage") or f"https://{domain}/"
        stored = (raw.get(domain) or {}).get("network_update") or {}
        addresses = stored.get("addresses") if isinstance(stored.get("addresses"), list) else []
        jobs.append((domain, url, addresses, None))
    if limit:
        jobs = jobs[:limit]
    print(f"network detail lookups: {len(jobs)}", flush=True)
    if not jobs:
        return
    fetcher = MappingFetcher()
    lock = threading.Lock()

    def one(job: tuple) -> None:
        domain, url, addresses, prior = job
        if prior is not None:
            update = dict(prior)
            update.update(_rdap(domain))
            entry = {
                "domain": domain,
                "checked_at": utc_now(),
                "pass": "network_extra",
                "rdap_retried": True,
                "rdap_retried_again": True,
                "network_extra": update,
            }
        else:
            if not addresses:
                addresses = _addresses(domain)
            update = _network_extra(domain, addresses)
            update.update(_peek_security(fetcher, url))
            entry = {
                "domain": domain,
                "checked_at": utc_now(),
                "pass": "network_extra",
                "network_extra": update,
            }
        with lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        label = update.get("asn") or update.get("registrar") or update.get("dmarc") or "network"
        print(f"{label} {domain}", flush=True)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(one, job) for job in jobs]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                print(f"error {type(exc).__name__}: {exc}", flush=True)


CAPTCHA_JS = """
() => {
  const html = (document.documentElement.innerHTML || "").slice(0, 200000);
  const srcs = [...document.scripts].map((el) => el.src).filter(Boolean);
  const iframes = [...document.querySelectorAll("iframe")].map((el) => el.src).filter(Boolean);
  const text = ((document.body && document.body.innerText) || "").replace(/\\s+/g, " ").trim().slice(0, 500);
  const widgets = [...document.querySelectorAll(".g-recaptcha, .h-captcha, .cf-turnstile, .frc-captcha, [data-sitekey], [data-pkey]")].slice(0, 6).map((el) => {
    const out = {};
    for (const attr of el.attributes) {
      if (attr.value && attr.value.length < 120 && /^(data-|class$)/.test(attr.name)) out[attr.name] = attr.value;
    }
    return out;
  });
  return { html, srcs, iframes, widgets, title: document.title || "", text };
}
"""


def _captcha_blob(data: dict) -> str:
    parts = [data.get("html") or "", *(data.get("srcs") or []), *(data.get("iframes") or [])]
    for widget in data.get("widgets") or []:
        if not isinstance(widget, dict):
            continue
        for key, value in widget.items():
            if isinstance(key, str) and isinstance(value, str):
                parts.append(f'{key}="{value[:120]}"')
    return "\n".join(parts)


def _html_challenge(html: str) -> bool:
    text = re.sub(r"<[^>]+>", " ", html[:8000])
    return _challenge("", text)


def _read_captcha_html(fetcher: MappingFetcher, url: str) -> dict:
    blank = {"seen": False, "provider": None, "sitekey": None, "page": None, "detail": None}
    if not url or not fetcher.allowed(url):
        return blank
    res = fetcher.get(url)
    if res.get("skipped") or res.get("error") or res.get("status") in (401, 403, 429, 503):
        return blank
    final = res.get("final_url") or url
    html = res.get("html") or ""
    if not _real_page(final) or len(html) < 400 or _html_challenge(html):
        return blank
    found = captcha_widget(html)
    return {
        "seen": True,
        "provider": found["provider"],
        "sitekey": found["sitekey"],
        "detail": found["detail"],
        "page": res.get("final_url") or url,
    }


def crawl_captcha(limit: int) -> None:
    """Re-read removal and search pages for a real CAPTCHA widget. robots.txt applies. Nothing is solved."""
    from playwright.async_api import async_playwright

    records = base_records()
    raw = load_done(LOG_PATH)
    apply_log(records, raw)
    by_domain: dict[str, dict] = {}
    for record in records.values():
        by_domain.setdefault(record["domain"], record)
    jobs = []
    for domain, record in by_domain.items():
        done = raw.get(domain) or {}
        stored = done.get("captcha_update") or {}
        needs_key = False
        if done.get("captcha_done") and not done.get("captcha_keys_checked"):
            for side in ("removal", "search"):
                row = stored.get(side) or {}
                if row.get("provider") and not row.get("sitekey"):
                    needs_key = True
        if done.get("captcha_done") and not needs_key:
            continue
        if record.get("liveness") in {"dead", "likely_dead"}:
            continue
        removal_url = record["removal"].get("final_url") or record["removal"].get("url")
        search_url = record["search"].get("page_url") if record["search"].get("available") else None
        jobs.append((domain, record, removal_url, search_url))
    if limit:
        jobs = jobs[:limit]
    print(f"captcha reads: {len(jobs)}", flush=True)
    if not jobs:
        return
    fetcher = MappingFetcher()

    async def run() -> None:
        lock = asyncio.Lock()
        static_sem = asyncio.Semaphore(6)
        browser_sem = asyncio.Semaphore(3)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])

            async def read_browser(url: str) -> dict:
                blank = {"seen": False, "provider": None, "sitekey": None, "page": None, "detail": None}
                if not url or not await asyncio.to_thread(fetcher.allowed, url):
                    return blank
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
                    ),
                    locale="en-CA",
                    viewport={"width": 1366, "height": 768},
                )
                page = await context.new_page()
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    status = response.status if response else None
                    if status in (401, 403, 429, 503):
                        return blank
                    await page.wait_for_timeout(1500)
                    data = await page.evaluate(CAPTCHA_JS)
                    if not _real_page(page.url) or _challenge(data.get("title") or "", data.get("text") or ""):
                        return blank
                    blob = _captcha_blob(data)
                    if len(blob) < 400:
                        return blank
                    found = captcha_widget(blob)
                    return {
                        "seen": True,
                        "provider": found["provider"],
                        "sitekey": found["sitekey"],
                        "detail": found["detail"],
                        "page": page.url,
                    }
                except Exception:
                    return blank
                finally:
                    await context.close()

            async def one(domain: str, record: dict, removal_url: str | None, search_url: str | None) -> None:
                async with static_sem:
                    removal = await asyncio.to_thread(_read_captcha_html, fetcher, removal_url or "")
                    search = {"seen": False, "provider": None, "sitekey": None, "page": None}
                    if search_url:
                        search = await asyncio.to_thread(_read_captcha_html, fetcher, search_url)

                async def fill_key(current: dict, url: str) -> dict:
                    if not (current.get("seen") and current.get("provider") and not current.get("sitekey")):
                        return current
                    async with browser_sem:
                        opened = await read_browser(url)
                    if opened.get("seen") and opened.get("sitekey"):
                        current["sitekey"] = opened["sitekey"]
                        if opened.get("provider"):
                            current["provider"] = opened["provider"]
                    return current

                removal = await fill_key(removal, removal.get("page") or removal_url or "")
                search = await fill_key(search, search.get("page") or search_url or "")
                if removal.get("seen") and not removal.get("provider") and (
                    record["removal"]["form_fields"] or record["removal"]["captcha"]["provider"]
                ):
                    async with browser_sem:
                        opened = await read_browser(removal.get("page") or removal_url or "")
                    if opened.get("seen"):
                        removal = opened
                    else:
                        removal["seen"] = False
                elif removal.get("seen") and not removal.get("provider"):
                    removal["seen"] = False
                if search.get("seen") and not search.get("provider") and record["search"].get("available"):
                    async with browser_sem:
                        opened = await read_browser(search.get("page") or search_url or "")
                    if opened.get("seen"):
                        search = opened
                    else:
                        search["seen"] = False
                elif search.get("seen") and not search.get("provider"):
                    search["seen"] = False
                update = {}
                if removal.get("seen"):
                    update["removal"] = {
                        "seen": True,
                        "provider": removal.get("provider"),
                        "sitekey": removal.get("sitekey"),
                        "detail": removal.get("detail"),
                        "page": removal.get("page"),
                    }
                if search_url and search.get("seen"):
                    update["search"] = {
                        "seen": True,
                        "provider": search.get("provider"),
                        "sitekey": search.get("sitekey"),
                        "detail": search.get("detail"),
                        "page": search.get("page"),
                    }
                entry = {
                    "domain": domain,
                    "checked_at": utc_now(),
                    "pass": "captcha",
                    "captcha_keys_checked": True,
                    "captcha_update": update or None,
                }
                async with lock:
                    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with LOG_PATH.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                found = (update.get("removal") or {}).get("provider") or (update.get("search") or {}).get("provider")
                print(f"{found or 'clear' if update else 'unread'} {domain}", flush=True)

            async def wrapped(job: tuple) -> None:
                try:
                    await one(*job)
                except Exception as exc:
                    print(f"error {job[0]} {type(exc).__name__}: {exc}", flush=True)

            await asyncio.gather(*(wrapped(job) for job in jobs))
            await browser.close()

    asyncio.run(run())


def _prefer_widget(static: dict, opened: dict) -> dict:
    """Keep a rendered widget, and fill any setting the static page already stated."""
    if not isinstance(opened.get("detail"), dict):
        return static
    if not isinstance(static.get("detail"), dict):
        return opened
    detail = dict(opened["detail"])
    for key, value in static["detail"].items():
        if detail.get(key) is None and value is not None:
            detail[key] = value
    chosen = dict(opened)
    chosen["detail"] = detail
    chosen["seen"] = True
    return chosen


def crawl_captcha_detail(limit: int) -> None:
    """Read public widget settings where a CAPTCHA provider is already stored. robots.txt applies. Nothing is solved."""
    from playwright.async_api import async_playwright

    records = base_records()
    raw = load_done(LOG_PATH)
    apply_log(records, raw)
    by_domain: dict[str, dict] = {}
    for record in records.values():
        by_domain.setdefault(record["domain"], record)
    jobs = []
    for domain, record in by_domain.items():
        done = raw.get(domain) or {}
        stored = done.get("captcha_detail") or {}
        needs_render = False
        if done.get("captcha_detail_done") and not done.get("captcha_detail_rendered"):
            for side, provider in (
                ("removal", record["removal"]["captcha"].get("provider")),
                ("search", record["search"].get("captcha_provider")),
            ):
                row = ((stored.get(side) or {}).get("detail") or {})
                if provider and not row.get("mode"):
                    needs_render = True
        if done.get("captcha_detail_done") and not needs_render:
            continue
        if record.get("liveness") in {"dead", "likely_dead"}:
            continue
        removal_url = None
        if record["removal"]["captcha"].get("provider"):
            removal_url = (
                record["removal"]["captcha"].get("checked_url")
                or record["removal"].get("final_url")
                or record["removal"].get("url")
            )
        search_url = record["search"].get("page_url") if record["search"].get("captcha_provider") else None
        if not removal_url and not search_url:
            continue
        jobs.append((domain, removal_url, search_url))
    if limit:
        jobs = jobs[:limit]
    print(f"captcha detail reads: {len(jobs)}", flush=True)
    if not jobs:
        return
    fetcher = MappingFetcher()

    async def run() -> None:
        lock = asyncio.Lock()
        static_sem = asyncio.Semaphore(6)
        browser_sem = asyncio.Semaphore(3)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])

            async def read_browser(url: str) -> dict:
                blank = {"seen": False, "provider": None, "sitekey": None, "page": None, "detail": None}
                if not url or not await asyncio.to_thread(fetcher.allowed, url):
                    return blank
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
                    ),
                    locale="en-CA",
                    viewport={"width": 1366, "height": 768},
                )
                page = await context.new_page()
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    status = response.status if response else None
                    if status in (401, 403, 429, 503):
                        return blank
                    await page.wait_for_timeout(1500)
                    data = await page.evaluate(CAPTCHA_JS)
                    if not _real_page(page.url) or _challenge(data.get("title") or "", data.get("text") or ""):
                        return blank
                    blob = _captcha_blob(data)
                    if len(blob) < 400:
                        return blank
                    found = captcha_widget(blob)
                    return {
                        "seen": True,
                        "provider": found["provider"],
                        "sitekey": found["sitekey"],
                        "detail": found["detail"],
                        "page": page.url,
                    }
                except Exception:
                    return blank
                finally:
                    await context.close()

            async def one(domain: str, removal_url: str | None, search_url: str | None) -> None:
                async with static_sem:
                    removal = (
                        await asyncio.to_thread(_read_captcha_html, fetcher, removal_url)
                        if removal_url else {"seen": False, "detail": None}
                    )
                    search = (
                        await asyncio.to_thread(_read_captcha_html, fetcher, search_url)
                        if search_url else {"seen": False, "detail": None}
                    )
                if removal_url and not (removal.get("detail") or {}).get("mode"):
                    async with browser_sem:
                        opened = await read_browser(removal_url)
                    if opened.get("seen") and isinstance(opened.get("detail"), dict):
                        removal = _prefer_widget(removal, opened)
                if search_url and not (search.get("detail") or {}).get("mode"):
                    async with browser_sem:
                        opened = await read_browser(search_url)
                    if opened.get("seen") and isinstance(opened.get("detail"), dict):
                        search = _prefer_widget(search, opened)
                update = {}
                if removal.get("seen") and isinstance(removal.get("detail"), dict):
                    update["removal"] = {"seen": True, "detail": removal["detail"]}
                if search.get("seen") and isinstance(search.get("detail"), dict):
                    update["search"] = {"seen": True, "detail": search["detail"]}
                entry = {
                    "domain": domain,
                    "checked_at": utc_now(),
                    "pass": "captcha_detail",
                    "captcha_detail_rendered": True,
                    "captcha_detail": update or None,
                }
                async with lock:
                    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with LOG_PATH.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                mode = ((update.get("removal") or {}).get("detail") or {}).get("mode") or (
                    (update.get("search") or {}).get("detail") or {}
                ).get("mode")
                print(f"{mode or 'detail' if update else 'unread'} {domain}", flush=True)

            async def wrapped(job: tuple) -> None:
                try:
                    await one(*job)
                except Exception as exc:
                    print(f"error {job[0]} {type(exc).__name__}: {exc}", flush=True)

            await asyncio.gather(*(wrapped(job) for job in jobs))
            await browser.close()

    asyncio.run(run())


class _FormReader(HTMLParser):
    """Public form controls from static HTML. Hidden values are not kept."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._form = None
        self._select = None
        self._in_option = False
        self._opt_value = ""
        self._opt_text: list[str] = []
        self._labels: list[dict] = []
        self._by_for: dict[str, str] = {}
        self._button = None
        self._textarea = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = {key.lower(): ("" if value is None else value) for key, value in attrs}
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "form" and self._form is None:
            self._form = {
                "id": a.get("id") or "",
                "name": a.get("name") or "",
                "autocomplete": a.get("autocomplete") or "",
                "novalidate": "novalidate" in a,
                "action": a.get("action") or "",
                "submit_label": None,
                "hidden_names": [],
                "fields": [],
            }
            return
        if tag == "label":
            self._labels.append({"for": a.get("for") or "", "parts": [], "controls": []})
            return
        if tag == "button" and self._form is not None:
            self._button = {"type": (a.get("type") or "submit").lower(), "value": a.get("value") or "", "parts": []}
            return
        if self._form is None:
            return
        if tag == "select":
            field = self._control(a, "select")
            self._select = field
            self._form["fields"].append(field)
            if self._labels:
                self._labels[-1]["controls"].append(field)
            return
        if tag == "option" and self._select is not None:
            self._close_option()
            self._in_option = True
            self._opt_value = a.get("value") or ""
            self._opt_text = []
            if "selected" in a and not self._select.get("value"):
                self._select["value"] = a.get("value") or ""
            return
        if tag == "textarea":
            field = self._control(a, "textarea")
            self._textarea = field
            self._form["fields"].append(field)
            if self._labels:
                self._labels[-1]["controls"].append(field)
            return
        if tag != "input":
            return
        kind = (a.get("type") or "text").lower()
        if kind == "hidden":
            name = (a.get("name") or "").strip()
            if name and len(self._form["hidden_names"]) < 12:
                self._form["hidden_names"].append(name)
            field = self._control(a, kind)
            field["value"] = ""
            self._form["fields"].append(field)
            return
        if kind in {"submit", "button", "image", "reset"}:
            if kind == "submit" and not self._form["submit_label"]:
                self._form["submit_label"] = a.get("value") or ""
            return
        field = self._control(a, kind)
        self._form["fields"].append(field)
        if self._labels:
            self._labels[-1]["controls"].append(field)

    def _control(self, a: dict, kind: str) -> dict:
        return {
            "name": a.get("name") or "",
            "id": a.get("id") or "",
            "type": kind,
            "placeholder": a.get("placeholder") or "",
            "label": a.get("aria-label") or "",
            "required": "required" in a,
            "autocomplete": a.get("autocomplete") or "",
            "inputmode": a.get("inputmode") or "",
            "pattern": a.get("pattern") or "",
            "minlength": a.get("minlength"),
            "maxlength": a.get("maxlength"),
            "min": a.get("min") or "",
            "max": a.get("max") or "",
            "step": a.get("step") or "",
            "accept": a.get("accept") or "",
            "multiple": "multiple" in a,
            "disabled": "disabled" in a,
            "readonly": "readonly" in a,
            "checked": "checked" in a,
            "value": "" if kind in {"password", "file"} else (a.get("value") or ""),
            "options": [],
        }

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
            return
        if tag == "label" and self._labels:
            item = self._labels.pop()
            text = re.sub(r"\s+", " ", " ".join(item["parts"])).strip()
            if item["for"] and text:
                self._by_for[item["for"]] = text
            for control in item["controls"]:
                if text and not control.get("label"):
                    control["label"] = text
            return
        if tag == "button" and self._button is not None:
            if self._form is not None and self._button["type"] not in {"button", "reset"} and not self._form["submit_label"]:
                self._form["submit_label"] = self._button["value"] or " ".join(self._button["parts"])
            self._button = None
            return
        if tag == "option":
            self._close_option()
            return
        if tag == "select":
            self._close_option()
            self._select = None
            return
        if tag == "textarea" and self._textarea is not None:
            text = "".join(self._textarea.pop("_parts", [])).strip()
            if text:
                self._textarea["value"] = text
            self._textarea = None
            return
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_option:
            self._opt_text.append(data.strip())
        if self._button is not None:
            self._button["parts"].append(data.strip())
        if self._textarea is not None:
            self._textarea.setdefault("_parts", []).append(data)
        for item in self._labels:
            item["parts"].append(data.strip())

    def _close_option(self) -> None:
        if not self._in_option or self._select is None:
            self._in_option = False
            return
        self._in_option = False
        if len(self._select["options"]) >= 320:
            return
        label = option_label(self._opt_value, " ".join(self._opt_text))
        if label and label not in self._select["options"]:
            self._select["options"].append(label[:80])

    def finish(self) -> list[dict]:
        for form in self.forms:
            for field in form["fields"]:
                if not field.get("label") and field.get("id") and self._by_for.get(field["id"]):
                    field["label"] = self._by_for[field["id"]]
        return self.forms


def _pack_form(raw: dict, page_url: str) -> dict:
    fields = []
    for item in (raw.get("fields") or [])[:80]:
        kind = (item.get("type") or "text").lower()
        if kind in {"submit", "button", "image", "reset"}:
            continue
        token = ((item.get("name") or "").strip() or (item.get("id") or "").strip()).lower()
        if token in SKIP_FIELD_NAMES:
            continue
        fields.append(_detail_from_raw(item))
    action = raw.get("action") or ""
    if action.lower().startswith(("javascript:", "mailto:")):
        action = ""
    absolute = urljoin(page_url, action).split("#", 1)[0] if action else (page_url or "")
    return {
        "action": absolute,
        "id": raw.get("id") or "",
        "name": raw.get("name") or "",
        "autocomplete": raw.get("autocomplete") or "",
        "novalidate": bool(raw.get("novalidate")),
        "submit_label": raw.get("submit_label") or "",
        "hidden_names": list(raw.get("hidden_names") or [])[:12],
        "fields": fields,
    }


def _read_forms(html: str, page_url: str) -> list[dict]:
    reader = _FormReader()
    try:
        reader.feed(html or "")
        reader.close()
    except Exception:
        return []
    return [_pack_form(form, page_url) for form in reader.finish()]


def _pick_form(forms: list[dict], stored_fields: list[dict], form_action: str | None) -> dict | None:
    stored_names = {_control_name(field) for field in stored_fields if _control_name(field)}
    want = (form_action or "").split("#", 1)[0].rstrip("/")
    best = None
    best_score = None
    for form in forms:
        names = {_control_name(field) for field in form["fields"] if _control_name(field)}
        overlap = len(names & stored_names)
        action = (form.get("action") or "").split("#", 1)[0].rstrip("/")
        action_hit = bool(want and action and (action == want or action.endswith(want) or want.endswith(action)))
        if stored_names:
            if overlap == 0:
                continue
            score = overlap * 10 + (5 if action_hit else 0)
        elif not form["fields"]:
            continue
        else:
            score = -abs(len(form["fields"]) - len(stored_fields)) + (3 if action_hit else 0)
        if best_score is None or score > best_score:
            best = form
            best_score = score
    return best


def _coverage(form: dict | None, stored_fields: list[dict]) -> float:
    names = {_control_name(field) for field in stored_fields if _control_name(field)}
    if not names:
        return 1.0 if form and form.get("fields") else 0.0
    if not form:
        return 0.0
    got = {_control_name(field) for field in form["fields"]}
    return len(names & got) / len(names)


def _side_payload(forms: list[dict], stored_fields: list[dict], form_action: str | None, seen: bool) -> dict:
    if not seen:
        return {"seen": False, "meta": None, "fields": []}
    form = _pick_form(forms, stored_fields, form_action)
    if not form:
        return {"seen": True, "meta": None, "fields": []}
    return {
        "seen": True,
        "meta": {
            "id": form.get("id"),
            "name": form.get("name"),
            "autocomplete": form.get("autocomplete"),
            "novalidate": bool(form.get("novalidate")),
            "submit_label": form.get("submit_label"),
            "hidden_names": form.get("hidden_names") or [],
        },
        "fields": form["fields"],
    }


def _read_form_html(fetcher: MappingFetcher, url: str) -> dict:
    blank = {"seen": False, "forms": []}
    if not url or not fetcher.allowed(url):
        return blank
    res = fetcher.get(url)
    if res.get("skipped") or res.get("error") or res.get("status") in (401, 403, 429, 503):
        return blank
    final = res.get("final_url") or url
    html = res.get("html") or ""
    if not _real_page(final) or len(html) < 400 or _html_challenge(html):
        return blank
    return {"seen": True, "forms": _read_forms(html, final)}


FORM_JS = r"""
() => {
  const clip = (value, n) => (value || "").replace(/\s+/g, " ").trim().slice(0, n);
  const labelOf = (el) => {
    const fromFor = el.labels && [...el.labels].map((label) => label.innerText || "").join(" ");
    return clip(fromFor || el.getAttribute("aria-label") || "", 80);
  };
  const optionLabel = (opt) => {
    const value = (opt.value || "").trim();
    const text = (opt.textContent || "").replace(/\s+/g, " ").trim();
    if (value && !/^\d+$/.test(value) && !/^(select|choose|none)$/i.test(value)) return value.slice(0, 80);
    if (/^(select|choose|select state|select a state|choose state)$/i.test(text)) return "";
    return (text || value).slice(0, 80);
  };
  const take = (el) => {
    const tag = el.tagName;
    const type = (el.getAttribute("type") || (tag === "TEXTAREA" ? "textarea" : tag === "SELECT" ? "select" : "text")).toLowerCase();
    let value = "";
    if (tag === "SELECT") {
      const selected = [...el.options].find((opt) => opt.defaultSelected);
      value = selected ? (selected.value || "") : "";
    } else if (type !== "password" && type !== "hidden" && type !== "file") {
      value = el.defaultValue || "";
    }
    const options = tag === "SELECT" ? [...el.options].slice(0, 320).map(optionLabel).filter(Boolean) : [];
    return {
      name: el.getAttribute("name") || "",
      id: el.getAttribute("id") || "",
      type,
      placeholder: clip(el.getAttribute("placeholder") || "", 80),
      label: labelOf(el),
      required: el.required === true,
      autocomplete: clip(el.getAttribute("autocomplete") || "", 40),
      inputmode: clip(el.getAttribute("inputmode") || "", 24),
      pattern: clip(el.getAttribute("pattern") || "", 120),
      minlength: el.getAttribute("minlength"),
      maxlength: el.getAttribute("maxlength"),
      min: clip(el.getAttribute("min") || "", 40),
      max: clip(el.getAttribute("max") || "", 40),
      step: clip(el.getAttribute("step") || "", 20),
      accept: clip(el.getAttribute("accept") || "", 80),
      multiple: el.multiple === true,
      disabled: el.disabled === true,
      readonly: el.readOnly === true,
      checked: el.checked === true,
      value,
      options,
    };
  };
  const forms = [...document.querySelectorAll("form")].slice(0, 8).map((form) => {
    const controls = [...form.querySelectorAll("input, textarea, select, button")].slice(0, 120);
    const fields = [];
    const hidden = [];
    let submit = "";
    for (const el of controls) {
      const type = (el.getAttribute("type") || (el.tagName === "BUTTON" ? "submit" : "")).toLowerCase();
      if (el.tagName === "BUTTON" || ["submit", "button", "image", "reset"].includes(type)) {
        if (!submit && type !== "button" && type !== "reset") submit = clip(el.getAttribute("value") || el.innerText || "", 80);
        continue;
      }
      if (type === "hidden") {
        const name = (el.getAttribute("name") || "").slice(0, 80);
        if (name && hidden.length < 12) hidden.push(name);
        if (fields.length < 80) {
          const item = take(el);
          item.value = "";
          fields.push(item);
        }
        continue;
      }
      if (fields.length < 80) fields.push(take(el));
    }
    return {
      id: clip(form.getAttribute("id") || "", 80),
      name: clip(form.getAttribute("name") || "", 80),
      autocomplete: clip(form.getAttribute("autocomplete") || "", 40),
      novalidate: form.noValidate === true,
      action: form.getAttribute("action") || "",
      submit_label: submit,
      hidden_names: hidden,
      fields,
    };
  });
  const title = (document.title || "").slice(0, 140);
  const text = ((document.body && document.body.innerText) || "").replace(/\s+/g, " ").trim().slice(0, 400);
  return { title, text, forms };
}
"""


def _choose_forms(static: dict, browser: dict | None, stored: list[dict], action: str | None) -> dict:
    static_form = _pick_form(static.get("forms") or [], stored, action) if static.get("seen") else None
    browser_form = _pick_form((browser or {}).get("forms") or [], stored, action) if browser and browser.get("seen") else None
    if _coverage(browser_form, stored) > _coverage(static_form, stored):
        return browser or static
    return static


def _side_incomplete(stored: list[dict], row: dict | None, liveness: str | None) -> bool:
    """True when a stored form still has a control the last read did not describe."""
    if not stored:
        return False
    if not isinstance(row, dict) or not row.get("seen"):
        return liveness != "robots_disallowed"
    incoming = row.get("fields") or []
    if not incoming:
        return True
    used: set[int] = set()
    for field in stored:
        index = _match_incoming(field, incoming, used)
        if index is None:
            return True
        used.add(index)
    return False


def _keep_richer(previous: dict | None, fresh: dict, stored: list[dict]) -> dict:
    """Keep the earlier read when a retry matches fewer stored controls."""
    if not isinstance(previous, dict) or not previous.get("fields"):
        return fresh
    if not fresh.get("fields"):
        return previous
    used: set[int] = set()
    old_hits = 0
    for field in stored:
        index = _match_incoming(field, previous["fields"], used)
        if index is not None:
            used.add(index)
            old_hits += 1
    used = set()
    new_hits = 0
    for field in stored:
        index = _match_incoming(field, fresh["fields"], used)
        if index is not None:
            used.add(index)
            new_hits += 1
    return fresh if new_hits >= old_hits else previous


def crawl_form_detail(limit: int) -> None:
    """Re-read stored search and removal forms for labels, limits, and hidden field names. Nothing is submitted."""
    from playwright.async_api import async_playwright

    records = base_records()
    raw = load_done(LOG_PATH)
    apply_log(records, raw)
    by_domain: dict[str, dict] = {}
    for record in records.values():
        by_domain.setdefault(record["domain"], record)
    jobs = []
    for domain, record in by_domain.items():
        if record.get("liveness") in {"dead", "likely_dead"}:
            continue
        removal_fields = record["removal"].get("form_fields") or []
        search_fields = record["search"].get("inputs") or [] if record["search"].get("available") else []
        if not removal_fields and not search_fields:
            continue
        done = raw.get(domain) or {}
        if done.get("form_detail_done"):
            detail = done.get("form_detail") if isinstance(done.get("form_detail"), dict) else {}
            removal_gap = _side_incomplete(removal_fields, detail.get("removal"), record.get("liveness"))
            search_gap = _side_incomplete(search_fields, detail.get("search"), record.get("liveness"))
            if not removal_gap and not search_gap:
                continue
        removal_url = record["removal"].get("final_url") or record["removal"].get("url") if removal_fields else None
        search_url = record["search"].get("page_url") if search_fields else None
        jobs.append((domain, removal_url, search_url, removal_fields, search_fields, record["removal"].get("form_action"), record["search"].get("form_action")))
    if limit:
        jobs = jobs[:limit]
    print(f"form detail reads: {len(jobs)}", flush=True)
    if not jobs:
        return
    fetcher = MappingFetcher()

    async def run() -> None:
        lock = asyncio.Lock()
        static_sem = asyncio.Semaphore(6)
        browser_sem = asyncio.Semaphore(3)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])

            async def read_browser(url: str) -> dict:
                blank = {"seen": False, "forms": []}
                if not url or not await asyncio.to_thread(fetcher.allowed, url):
                    return blank
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
                    ),
                    locale="en-CA",
                    viewport={"width": 1366, "height": 768},
                )
                page = await context.new_page()
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    status = response.status if response else None
                    if status in (401, 403, 429, 503):
                        return blank
                    await page.wait_for_timeout(1200)
                    data = await page.evaluate(FORM_JS)
                    if not _real_page(page.url) or _challenge(data.get("title") or "", data.get("text") or ""):
                        return blank
                    return {"seen": True, "forms": [_pack_form(form, page.url) for form in data.get("forms") or []]}
                except Exception:
                    return blank
                finally:
                    await context.close()

            static_tasks: dict[str, asyncio.Task] = {}
            browser_tasks: dict[str, asyncio.Task] = {}

            async def static_for(url: str) -> dict:
                if url not in static_tasks:
                    async def run() -> dict:
                        async with static_sem:
                            return await asyncio.to_thread(_read_form_html, fetcher, url)
                    static_tasks[url] = asyncio.create_task(run())
                return await static_tasks[url]

            async def browser_for(url: str) -> dict:
                if url not in browser_tasks:
                    async def run() -> dict:
                        async with browser_sem:
                            return await read_browser(url)
                    browser_tasks[url] = asyncio.create_task(run())
                return await browser_tasks[url]

            async def read_side(url: str, stored: list[dict], action: str | None) -> dict:
                if not url:
                    return {"seen": False, "forms": []}
                static = await static_for(url)
                covered = _coverage(_pick_form(static.get("forms") or [], stored, action), stored) if static.get("seen") else 0
                if static.get("seen") and covered >= 0.5:
                    return static
                if not await asyncio.to_thread(fetcher.allowed, url):
                    return static
                opened = await browser_for(url)
                if not static.get("seen"):
                    return opened if opened.get("seen") else static
                return _choose_forms(static, opened, stored, action)

            async def one(domain: str, removal_url, search_url, removal_fields, search_fields, removal_action, search_action) -> None:
                removal_read = await read_side(removal_url or "", removal_fields, removal_action)
                search_read = await read_side(search_url or "", search_fields, search_action)
                prior = (raw.get(domain) or {}).get("form_detail") if isinstance((raw.get(domain) or {}).get("form_detail"), dict) else {}
                update = {
                    "removal": _keep_richer(prior.get("removal"), _side_payload(removal_read.get("forms") or [], removal_fields, removal_action, bool(removal_read.get("seen"))), removal_fields) if removal_fields else {"seen": False, "meta": None, "fields": []},
                    "search": _keep_richer(prior.get("search"), _side_payload(search_read.get("forms") or [], search_fields, search_action, bool(search_read.get("seen"))), search_fields) if search_fields else {"seen": False, "meta": None, "fields": []},
                }
                entry = {
                    "domain": domain,
                    "checked_at": utc_now(),
                    "pass": "form_detail",
                    "form_detail": update,
                }
                async with lock:
                    await asyncio.to_thread(_append_jsonl, entry)
                label = ((update["removal"].get("meta") or {}).get("submit_label") if update["removal"].get("seen") else None) or ((update["search"].get("meta") or {}).get("submit_label") if update["search"].get("seen") else None) or ("form" if update["removal"].get("seen") or update["search"].get("seen") else "unread")
                print(f"{label} {domain}", flush=True)

            async def wrapped(job: tuple) -> None:
                try:
                    await one(*job)
                except Exception as exc:
                    print(f"error {job[0]} {type(exc).__name__}: {exc}", flush=True)

            await asyncio.gather(*(wrapped(job) for job in jobs))
            await browser.close()

    asyncio.run(run())


def _append_jsonl(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-only", action="store_true")
    parser.add_argument("--search-missing", action="store_true", help="Read lookup homepages that have no stored search form")
    parser.add_argument("--legal-missing", action="store_true", help="Read unread privacy and terms pages when robots.txt allows")
    parser.add_argument("--network", action="store_true", help="Look up DNS, the certificate, and removal-URL response headers")
    parser.add_argument("--network-extra", action="store_true", help="Add IPv6, mail policy, CAA, DNSSEC, ASN, certificate dates, RDAP, and security headers")
    parser.add_argument("--captcha", action="store_true", help="Re-read removal and search pages for a CAPTCHA widget")
    parser.add_argument("--captcha-detail", action="store_true", help="Read public widget settings where a CAPTCHA provider is already stored")
    parser.add_argument("--form-detail", action="store_true", help="Re-read stored search and removal forms for labels, limits, and hidden field names")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fresh", action="store_true", help="Delete the v4 evidence log before crawling. Does not touch v3.")
    args = parser.parse_args()
    if args.fresh and LOG_PATH.exists() and not args.compile_only:
        LOG_PATH.unlink()
    if args.search_missing and not args.compile_only:
        crawl_search(args.limit)
    elif args.legal_missing and not args.compile_only:
        crawl_legal(args.limit)
    elif args.network and not args.compile_only:
        crawl_network(args.limit)
    elif args.network_extra and not args.compile_only:
        crawl_network_extra(args.limit)
    elif args.captcha and not args.compile_only:
        crawl_captcha(args.limit)
    elif args.captcha_detail and not args.compile_only:
        crawl_captcha_detail(args.limit)
    elif args.form_detail and not args.compile_only:
        crawl_form_detail(args.limit)
    elif not args.compile_only:
        crawl_missing(args.limit)
    publish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
