#!/usr/bin/env python3
"""Map each catalog broker's public pages, liveness, and published emails.

One crawl per catalog domain (the host on the catalog row, not the parent
registrable domain). Rows that list the same host share one map. Seeds are
the homepage and that domain's catalog URLs. From those pages the crawl
follows same-site links that look like privacy, opt-out, contact, legal, or
about pages, and reads robots.txt plus a bounded slice of any sitemap.

Writes:
- research/evidence/broker-maps.jsonl  (one object per domain, resumable)
- data/v1/broker-maps.json              (compiled maps, one entry per catalog id)
- data/v1/broker-liveness.csv
- data/v1/broker-emails.csv
- docs/broker-maps.md

Rules match the rest of this repo: robots.txt is honored, forms are not
submitted, logins and CAPTCHAs are not touched, and a bot wall is recorded
as blocked rather than dead. People-search listing URLs are counted in a
path histogram and are not stored.

Dead means the hostname does not resolve, the catalog URL is a parked
domain, or both the homepage and the catalog URL are HTTP 404/410 (including
a soft 404). A live site with a 404 opt-out URL is opt_out_missing, not dead.
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
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse

import requests

from catalog import ROOT, assign_ids, load_catalog
from rescrape_fields import (
    BAD_EMAIL,
    CHALLENGE,
    EMAIL_RE,
    PRIVACY_LOCALS,
    Fetcher,
    parse,
    registrable,
)

LOG_PATH = ROOT / "research" / "evidence" / "broker-maps.jsonl"
MAPS_PATH = ROOT / "data" / "v1" / "broker-maps.json"
LIVENESS_CSV = ROOT / "data" / "v1" / "broker-liveness.csv"
EMAILS_CSV = ROOT / "data" / "v1" / "broker-emails.csv"
DOC_PATH = ROOT / "docs" / "broker-maps.md"
V1_DATASET = ROOT / "data" / "v1" / "broker-opt-outs.enriched.json"
FILLS_PATH = ROOT / "data" / "v1" / "field-fills.json"

HOST_DELAY = 1.25
TIMEOUT = (8, 18)
MAX_BYTES = 900_000
MAX_DISCOVERY_PAGES = 12
MAX_STORED_LINKS = 200
MAX_SITEMAP_FILES = 3
MAX_SITEMAP_LOCS = 4000
WORKERS = 6

PROFILE_PATH = re.compile(
    r"/(people|person|persons|profiles?|names?|phones?|emails?|addresses|search-results?|background-check)/",
    re.I,
)
PARKED_MARKERS = (
    "domain is for sale",
    "buy this domain",
    "this domain may be for sale",
    "this webpage is parked",
    "domain parking",
    "hugedomains.com",
    "sedoparking",
    "inquire about this domain",
    "this domain name is for sale",
)
ROLE_ORDER = {"opt_out": 0, "privacy": 1, "contact": 2, "legal": 3, "about": 4, "home": 5, "other": 9}
LOC_RE = re.compile(r"<loc>\s*([^<]+)\s*</loc>", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class MappingFetcher(Fetcher):
    """Same politeness and robots rules as the field rescrape, with error text kept."""

    def wait(self, host: str) -> None:
        with self.lock:
            now = time.monotonic()
            ready = self.next.get(host, now)
            self.next[host] = max(ready, now) + HOST_DELAY
        delay = ready - now
        if delay > 0:
            time.sleep(delay)

    def get(self, url: str) -> dict:
        if not self.allowed(url):
            return {"url": url, "skipped": "robots"}
        self.wait(urlparse(url).netloc)
        try:
            with self.s.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True) as response:
                body = b""
                for chunk in response.iter_content(65_536):
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
                return {
                    "url": url,
                    "final_url": response.url,
                    "status": response.status_code,
                    "ctype": response.headers.get("content-type", ""),
                    "html": body.decode("utf-8", errors="replace"),
                }
        except requests.RequestException as exc:
            return {"url": url, "error": f"{type(exc).__name__}: {exc}"[:240]}


def is_dns(error: str | None) -> bool:
    text = (error or "").lower()
    return any(
        token in text
        for token in (
            "nameresolution",
            "name or service not known",
            "nodename nor servname",
            "getaddrinfo",
            "no address associated",
            "failed to resolve",
            "name resolution",
            "errno 11001",
            "temporary failure in name resolution",
        )
    )


def is_parked(title: str, text: str) -> bool:
    blob = f"{title}\n{text[:2500]}".lower()
    if not any(marker in blob for marker in PARKED_MARKERS):
        return False
    return len(text) < 5000 or "for sale" in title.lower() or "parked" in blob


def is_soft_404(title: str, text: str) -> bool:
    if re.search(r"\b(404|page not found|doesn't exist|does not exist)\b", title, re.I):
        return True
    head = text[:500].lower()
    return "not found" in head and "404" in head and len(text) < 2500


MULTI_SUFFIXES = (
    "gouv.qc.ca",
    "gov.bc.ca",
    "gov.on.ca",
    "gov.ab.ca",
    "gc.ca",
    "bc.ca",
    "ab.ca",
    "mb.ca",
    "nb.ca",
    "nl.ca",
    "ns.ca",
    "nt.ca",
    "nu.ca",
    "on.ca",
    "pe.ca",
    "qc.ca",
    "sk.ca",
    "yk.ca",
)
PRODUCT_NOISE = ("tempered", "iphone", "screen-protect", "phone-case", "glass")


def bare(host: str | None) -> str:
    return (host or "").lower().removeprefix("www.")


def site_key(host: str) -> str:
    """Registrable site, without collapsing a public suffix such as bc.ca."""
    host = bare(host)
    for suffix in sorted(MULTI_SUFFIXES, key=len, reverse=True):
        if host == suffix or host.endswith("." + suffix):
            labels = suffix.count(".") + 1
            parts = host.split(".")
            if len(parts) > labels:
                return ".".join(parts[-(labels + 1) :])
            return host
    return registrable(host)


def classify_role(url: str, anchor: str) -> str:
    path = (urlparse(url).path or "").lower().replace("_", "-")
    segments = [segment for segment in path.split("/") if segment]
    anchor_text = " ".join((anchor or "").lower().split())
    noisy = any(word in path or word in anchor_text for word in PRODUCT_NOISE)

    for segment in segments:
        if any(key in segment for key in ("opt-out", "optout", "do-not-sell", "donotsell", "dnsmpi")):
            return "opt_out"
        if segment in {"removal", "suppression"} or segment.startswith(("opt-out", "optout")):
            return "opt_out"
        if segment == "privacy" or segment.startswith("privacy-") or segment.endswith("-privacy") or any(
            key in segment for key in ("privacy-policy", "privacy-notice", "privacy-center", "privacy-request", "privacy-choices", "privacy-rights")
        ):
            return "privacy"
        if segment in {"contact", "contact-us", "contactus"} or segment.startswith("contact-"):
            return "contact"
        if segment in {"legal", "terms", "terms-of-service", "terms-of-use", "imprint"} or segment.startswith(("legal-", "terms-")):
            return "legal"
        if segment in {"gdpr", "ccpa", "cpra"} or "your-rights" in segment or "consumer-rights" in segment:
            return "legal"
        if segment in {"about", "about-us", "company", "faq", "help", "support"} or segment.startswith("about-"):
            return "about"
    if noisy or len(anchor_text) > 90 or not anchor_text:
        return "other"
    if any(key in anchor_text for key in ("opt out", "opt-out", "do not sell", "delete my", "remove my")):
        return "opt_out"
    if "privacy" in anchor_text:
        return "privacy"
    if "contact" in anchor_text:
        return "contact"
    if any(key in anchor_text for key in ("legal", "terms of", "gdpr", "ccpa", "your rights")):
        return "legal"
    if any(key in anchor_text for key in ("about us", "about", "faq", "support", "help")):
        return "about"
    return "other"


def path_depth(url: str) -> int:
    path = urlparse(url).path.strip("/")
    return 0 if not path else len([part for part in path.split("/") if part])


def is_profile_url(url: str) -> bool:
    return bool(PROFILE_PATH.search(urlparse(url).path or ""))


def same_site(url: str, domain: str) -> bool:
    host = urlparse(url).hostname or ""
    if not host:
        return False
    return site_key(host) == site_key(domain)


def norm_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme.lower()}://{host}{path}{query}"


def prefix_of(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return "/" + parts[0] if parts else "/"


def keep_link(url: str, role: str) -> bool:
    if is_profile_url(url):
        return False
    if role != "other":
        return True
    return path_depth(url) <= 2


def load_parents() -> set[str]:
    path = ROOT / "data" / "v1" / "parent-companies.json"
    if not path.exists():
        return set()
    blob = path.read_text(encoding="utf-8").lower()
    return {registrable(item) for item in re.findall(r"[a-z0-9-]+\.(?:com|net|org|us|io|co)", blob)}


def filter_email(email: str, domain: str, parents: set[str]) -> tuple[str, str] | None:
    cleaned = email.strip().strip(".").lower()
    if "@" not in cleaned or any(bad in cleaned for bad in BAD_EMAIL):
        return None
    local, host = cleaned.split("@", 1)
    if not re.search(r"[a-z]", local) or len(local) > 64 or len(host) > 80:
        return None
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return None
    same = site_key(host) == site_key(domain) or registrable(host) in parents
    privacy_at = next((index for index, key in enumerate(PRIVACY_LOCALS) if key in local), None)
    if same:
        confidence = "extracted" if privacy_at is not None and privacy_at < 17 else "uncertain"
        return cleaned, confidence
    if privacy_at is not None and privacy_at < 20:
        return cleaned, "uncertain"
    return None


def collect_emails(parsed: dict | None, domain: str, parents: set[str]) -> list[dict]:
    if not parsed:
        return []
    found: list[tuple[str, str, str]] = []
    for email in parsed.get("mailtos") or []:
        found.append((email, "mailto", ""))
    for match in EMAIL_RE.finditer(parsed.get("text") or ""):
        found.append((match.group(1), "text", ""))
    kept: list[dict] = []
    seen: set[str] = set()
    for email, via, _note in found:
        decision = filter_email(email, domain, parents)
        if not decision or decision[0] in seen:
            continue
        seen.add(decision[0])
        kept.append({"email": decision[0], "confidence": decision[1], "via": via})
        if len(kept) >= 20:
            break
    return kept


def page_liveness(res: dict, parsed: dict | None) -> str:
    if res.get("skipped") == "robots":
        return "robots_disallowed"
    if res.get("error"):
        return "dead" if is_dns(res["error"]) else "error"
    status = res.get("status")
    title = (parsed or {}).get("title") or ""
    text = (parsed or {}).get("text") or ""
    if parsed and (parsed.get("blocked") or status in (401, 403, 429, 451, 503)):
        return "blocked"
    if status in (401, 403, 429, 451, 503):
        return "blocked"
    if "human verification" in f"{title} {text[:500]}".lower():
        return "blocked"
    if status in (404, 410) or (parsed and is_soft_404(title, text)):
        return "dead"
    if parsed and is_parked(title, text):
        return "parked"
    if isinstance(status, int) and 200 <= status < 400:
        return "alive"
    return "error"


def slim_page(res: dict, role: str, origin: str, domain: str, parents: set[str]) -> tuple[dict, dict | None]:
    if "html" in res and "html" not in (res.get("ctype") or "").lower():
        head = (res.get("html") or "")[:600].lower()
        if "<html" in head or head.lstrip().startswith("<!doctype html"):
            res["ctype"] = "text/html"
    parsed = parse(res) if "html" in res else None
    if parsed and res.get("status") in (401, 403, 429, 503):
        parsed["blocked"] = True
    elif parsed and any(marker in ((parsed.get("title") or "") + " " + (parsed.get("text") or "")[:3000]).lower() for marker in CHALLENGE):
        parsed["blocked"] = True
    liveness = page_liveness(res, parsed)
    emails = [] if liveness in {"blocked", "robots_disallowed", "dead", "error"} else collect_emails(parsed, domain, parents)
    record = {
        "url": res.get("url"),
        "final_url": res.get("final_url"),
        "status": res.get("status"),
        "liveness": liveness,
        "role": role,
        "origin": origin,
        "title": ((parsed or {}).get("title") or "")[:140],
        "error": res.get("error"),
        "skipped": res.get("skipped"),
        "emails": emails,
    }
    return record, parsed if liveness == "alive" else None


def absolute_links(parsed: dict, domain: str) -> list[tuple[str, str, str]]:
    base = parsed.get("url") or ""
    found = []
    seen: set[str] = set()
    for href, text in parsed.get("links") or []:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base, href).split("#", 1)[0]
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ("http", "https"):
            continue
        if not same_site(url, domain):
            continue
        key = norm_url(url)
        if key in seen:
            continue
        seen.add(key)
        anchor = " ".join((text or "").split())[:100]
        found.append((url, anchor, classify_role(url, anchor)))
    return found


def looks_like_robots(body: str) -> bool:
    head = body[:800].lower()
    if "<html" in head or "<!doctype html" in head:
        return False
    return "user-agent" in head or "sitemap:" in head


def sitemap_locs(fetcher: MappingFetcher, domain: str, base_url: str | None = None) -> dict:
    summary: dict = {"found": False, "files": [], "loc_count": 0, "prefix_counts": {}, "privacy_urls": []}
    origins: list[str] = []
    if base_url:
        parsed = urlparse(base_url)
        if parsed.scheme and parsed.netloc:
            origins.append(f"{parsed.scheme}://{parsed.netloc}")
    origins.extend((f"https://{domain}", f"https://www.{domain}"))
    origins = list(dict.fromkeys(origins))
    robots_body = ""
    for origin in origins:
        robots = fetcher.get(origin + "/robots.txt")
        body = robots.get("html") or ""
        if robots.get("status") and robots["status"] < 400 and looks_like_robots(body):
            robots_body = body
            break
    sitemap_urls: list[str] = []
    for line in robots_body.splitlines():
        if line.lower().startswith("sitemap:"):
            sitemap_urls.append(line.split(":", 1)[1].strip())
    if not sitemap_urls:
        sitemap_urls = [origins[0] + "/sitemap.xml", origins[0] + "/sitemap_index.xml"]

    prefixes: Counter[str] = Counter()
    notable: list[str] = []
    archived: list[str] = []
    files_read = 0
    pending = list(dict.fromkeys(sitemap_urls))
    seen_files: set[str] = set()

    def prefer(url: str) -> int:
        lowered = url.lower()
        if any(token in lowered for token in ("people", "profile", "phone", "name", "person")):
            return 3
        if any(token in lowered for token in ("privacy", "page", "post", "legal", "site")):
            return 0
        return 1

    pending.sort(key=prefer)
    while pending and files_read < MAX_SITEMAP_FILES and summary["loc_count"] < MAX_SITEMAP_LOCS:
        url = pending.pop(0)
        if url in seen_files or not same_site(url, domain):
            continue
        seen_files.add(url)
        res = fetcher.get(url)
        body = res.get("html") or ""
        if res.get("error") or res.get("skipped") or not body or (res.get("status") or 0) >= 400:
            continue
        if "<html" in body[:500].lower() and "<urlset" not in body.lower() and "<sitemapindex" not in body.lower():
            continue
        files_read += 1
        summary["found"] = True
        summary["files"].append(url)
        locs = [unescape(item.strip()) for item in LOC_RE.findall(body)]
        child_maps = [item for item in locs if item.lower().endswith(".xml") or "sitemap" in item.lower()]
        page_locs = [item for item in locs if item not in child_maps]
        if child_maps and not page_locs:
            child_maps.sort(key=prefer)
            pending = child_maps + pending
            continue
        for loc in page_locs:
            if summary["loc_count"] >= MAX_SITEMAP_LOCS:
                break
            summary["loc_count"] += 1
            prefixes[prefix_of(loc)] += 1
            role = classify_role(loc, "")
            if role in {"opt_out", "privacy", "contact", "legal"}:
                bucket = archived if "archive" in loc.lower() else notable
                if loc not in bucket:
                    bucket.append(loc)
    summary["prefix_counts"] = dict(prefixes.most_common(40))
    summary["privacy_urls"] = (notable + archived)[:40]
    summary["loc_count_capped"] = summary["loc_count"] >= MAX_SITEMAP_LOCS
    return summary


def entry_status(catalog: dict, home: dict | None) -> tuple[str, str]:
    catalog_state = catalog.get("liveness")
    home_state = (home or {}).get("liveness")
    redirected = bool(
        catalog.get("final_url")
        and catalog.get("url")
        and norm_url(catalog["final_url"]) != norm_url(catalog["url"])
        and catalog_state == "alive"
    )
    if catalog_state == "dead" and is_dns(catalog.get("error")):
        return "dead", "catalog hostname does not resolve"
    if home_state == "dead" and home and is_dns(home.get("error")) and catalog_state in {"dead", "error", "parked"}:
        return "dead", "hostname does not resolve"
    if catalog_state == "parked" or (home_state == "parked" and catalog_state in {"dead", "parked", "error"}):
        return "dead", "domain is parked"
    if catalog_state == "dead" and home_state in {"dead", "parked"}:
        return "dead", "homepage and catalog URL are gone"
    if catalog_state == "dead":
        return "opt_out_missing", "catalog URL returned 404 or 410; the site itself still responded"
    if catalog_state == "blocked":
        return "blocked", "catalog URL returned a bot wall or HTTP 401/403/429/503"
    if catalog_state == "robots_disallowed":
        return "robots_disallowed", "robots.txt disallows this client"
    if catalog_state == "error" and home_state in {"dead", "parked"}:
        return "likely_dead", "homepage is gone and the catalog URL failed before returning a status"
    if catalog_state == "error":
        status_code = catalog.get("status")
        if status_code:
            return "error", f"catalog URL returned HTTP {status_code}"
        return "error", catalog.get("error") or "catalog URL could not be fetched"
    if catalog_state == "alive" and redirected:
        return "alive", "catalog URL redirected and the final page loaded"
    if catalog_state == "alive":
        return "alive", "catalog URL loaded"
    return "error", catalog_state or "unknown"


def crawl_domain(fetcher: MappingFetcher, domain: str, catalog_urls: list[str], parents: set[str]) -> dict:
    seeds: list[tuple[str, str, str]] = [(f"https://{domain}/", "home", "seed")]
    for url in catalog_urls:
        role = classify_role(url, "")
        seeds.append((url, role if role != "other" else "opt_out", "catalog"))

    pages: list[dict] = []
    seen_pages: set[str] = set()
    link_map: list[dict] = []
    seen_links: set[str] = set()
    prefixes: Counter[str] = Counter()
    queue: list[tuple[str, str, str]] = []

    def remember_links(parsed: dict | None) -> None:
        if not parsed:
            return
        for url, anchor, role in absolute_links(parsed, domain):
            prefixes[prefix_of(url)] += 1
            key = norm_url(url)
            if key in seen_links:
                continue
            seen_links.add(key)
            if keep_link(url, role) and len(link_map) < MAX_STORED_LINKS:
                link_map.append({"url": url, "anchor": anchor, "role": role})
            if role != "other":
                queue.append((url, role, "link"))

    def discovery_count() -> int:
        return sum(1 for page in pages if page["origin"] in {"link", "sitemap"})

    def fetch_one(url: str, role: str, origin: str) -> None:
        key = norm_url(url)
        if key in seen_pages:
            if origin == "catalog":
                prior = next(page for page in pages if norm_url(page["url"]) == key)
                alias = dict(prior)
                alias["url"] = url
                alias["origin"] = "catalog"
                alias["role"] = role
                pages.append(alias)
            return
        if origin in {"link", "sitemap"} and discovery_count() >= MAX_DISCOVERY_PAGES:
            return
        seen_pages.add(key)
        record, parsed = slim_page(fetcher.get(url), role, origin, domain, parents)
        pages.append(record)
        remember_links(parsed)

    for url, role, origin in seeds:
        fetch_one(url, role, origin)

    queue.sort(key=lambda item: ROLE_ORDER.get(item[1], 9))
    index = 0
    while index < len(queue) and discovery_count() < MAX_DISCOVERY_PAGES:
        url, role, origin = queue[index]
        index += 1
        fetch_one(url, role, origin)

    home_page = next((page for page in pages if page["origin"] == "seed" and page["role"] == "home"), None)
    if home_page and is_dns(home_page.get("error")):
        sitemap = {"found": False, "files": [], "loc_count": 0, "prefix_counts": {}, "privacy_urls": [], "loc_count_capped": False}
    else:
        sitemap = sitemap_locs(fetcher, domain, (home_page or {}).get("final_url") or f"https://{domain}/")
    for url in sitemap.get("privacy_urls") or []:
        if discovery_count() >= MAX_DISCOVERY_PAGES:
            break
        fetch_one(url, classify_role(url, ""), "sitemap")

    emails: dict[str, dict] = {}
    for page in pages:
        for item in page.get("emails") or []:
            current = emails.get(item["email"])
            record = {**item, "source_url": page.get("final_url") or page.get("url")}
            if current is None or (current["confidence"] != "extracted" and record["confidence"] == "extracted"):
                emails[item["email"]] = record

    home = next((page for page in pages if page["origin"] == "seed" and page["role"] == "home"), None)
    catalog_fetches = {
        page["url"]: page for page in pages if page["origin"] == "catalog"
    }
    site_state = (home or {}).get("liveness") or "error"
    return {
        "domain": domain,
        "checked_at": utc_now(),
        "site_liveness": site_state,
        "homepage": home,
        "pages": pages,
        "link_map": link_map,
        "link_prefix_counts": dict(prefixes.most_common(40)),
        "sitemap": sitemap,
        "catalog_fetches": catalog_fetches,
        "emails": sorted(emails.values(), key=lambda item: (item["confidence"] != "extracted", item["email"])),
    }


def catalog_domain(row: dict) -> str:
    return bare(row.get("domain") or urlparse(row["url"]).hostname or "")


def group_rows(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[catalog_domain(row)].append(row)
    return grouped


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
            if item.get("domain"):
                done[item["domain"]] = item
    return done


def scrape(args: argparse.Namespace) -> dict[str, dict]:
    rows = assign_ids(load_catalog())
    grouped = group_rows(rows)
    domains = sorted(grouped)
    if args.ids:
        wanted = {catalog_domain(row) for row in rows if row["id"] in set(args.ids.split(","))}
        domains = [domain for domain in domains if domain in wanted]
    if args.limit:
        domains = domains[: args.limit]
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and LOG_PATH.exists() and not args.ids and not args.limit:
        LOG_PATH.unlink()
    done = load_done(LOG_PATH)
    if args.fresh:
        done = {domain: item for domain, item in done.items() if domain not in set(domains)}
    pending = [domain for domain in domains if domain not in done]
    print(f"domains: {len(domains)} pending: {len(pending)} already mapped: {len(domains) - len(pending)}", flush=True)
    parents = load_parents()
    fetcher = MappingFetcher()
    lock = threading.Lock()
    finished = 0
    with LOG_PATH.open("a", encoding="utf-8") as log, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(crawl_domain, fetcher, domain, list(dict.fromkeys(row["url"] for row in grouped[domain])), parents): domain
            for domain in pending
        }
        for future in as_completed(futures):
            domain = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # keep the run going; this domain can be retried
                result = {"domain": domain, "checked_at": utc_now(), "error": repr(exc)[:240], "pages": [], "emails": [], "catalog_fetches": {}}
            with lock:
                log.write(json.dumps(result, ensure_ascii=False) + "\n")
                log.flush()
                done[domain] = result
                finished += 1
                if finished % 25 == 0 or finished == len(pending):
                    print(f"mapped {finished}/{len(pending)}", flush=True)
    return load_done(LOG_PATH)


def email_ok(item: dict, domain: str, parents: set[str]) -> bool:
    return filter_email(item.get("email") or "", domain, parents) is not None


def best_email(emails: list[dict]) -> dict | None:
    extracted = [item for item in emails if item.get("confidence") == "extracted"]
    pool = extracted or list(emails)
    return pool[0] if pool else None


def compile_maps(raw: dict[str, dict]) -> dict:
    rows = assign_ids(load_catalog())
    parents = load_parents()
    entries = {}
    for row in rows:
        domain = catalog_domain(row)
        site = raw.get(domain) or {}
        catalog = (site.get("catalog_fetches") or {}).get(row["url"])
        if catalog is None:
            catalog = next(
                (page for page in site.get("pages") or [] if page.get("origin") == "catalog" and norm_url(page.get("url") or "") == norm_url(row["url"])),
                None,
            )
        if not site:
            status, detail = "not_checked", "no crawl result in this log"
        elif catalog is None:
            status, detail = "error", "catalog URL was not fetched"
        else:
            status, detail = entry_status(catalog, site.get("homepage"))
        emails = [item for item in (site.get("emails") or []) if email_ok(item, domain, parents)]
        primary = best_email(emails)
        final_url = (catalog or {}).get("final_url")
        redirected = bool(catalog and final_url and norm_url(final_url) != norm_url(row["url"]) and catalog.get("liveness") == "alive")
        same_host = bool(redirected and same_site(final_url, urlparse(row["url"]).hostname or ""))
        entries[row["id"]] = {
            "id": row["id"],
            "name": row["name"],
            "category": row["category"],
            "domain": domain,
            "catalog_url": row["url"],
            "liveness": status,
            "liveness_detail": detail,
            "catalog_http_status": (catalog or {}).get("status"),
            "catalog_final_url": final_url,
            "redirect": (
                {"final_url": final_url, "same_site": same_host}
                if redirected
                else None
            ),
            "site_liveness": site.get("site_liveness"),
            "primary_email": (primary or {}).get("email"),
            "primary_email_confidence": (primary or {}).get("confidence"),
            "primary_email_source_url": (primary or {}).get("source_url"),
            "checked_at": site.get("checked_at"),
        }
    page_keys = ("url", "final_url", "status", "liveness", "role", "origin", "title")
    needed = {item["domain"] for item in entries.values()}
    domains = {}
    for domain, site in sorted(raw.items()):
        if domain not in needed:
            continue
        domains[domain] = {
            "site_liveness": site.get("site_liveness"),
            "checked_at": site.get("checked_at"),
            "error": site.get("error"),
            "emails": [item for item in (site.get("emails") or []) if email_ok(item, domain, parents)],
            "pages": [{key: page.get(key) for key in page_keys} for page in site.get("pages") or []],
            "link_map": site.get("link_map") or [],
            "link_prefix_counts": site.get("link_prefix_counts") or {},
            "sitemap": site.get("sitemap") or {},
        }
    counts = Counter(item["liveness"] for item in entries.values())
    with_email = sum(1 for item in entries.values() if item["primary_email"])
    report = {
        "schema": "broker-maps/1.0",
        "checked_at": utc_now(),
        "method": (
            "Public pages only. Homepage, catalog URL, same-site privacy/opt-out/contact/legal/about links, "
            "robots.txt, and a bounded sitemap slice. robots.txt honored. No form submitted, no login, no CAPTCHA. "
            "A bot wall is blocked, not dead. People-search listing URLs are counted by path prefix and not stored. "
            "Dead means the hostname does not resolve, the domain is parked, or both the homepage and the catalog URL are gone. "
            "Rows that list the same catalog domain share one page map."
        ),
        "catalog_rows": len(entries),
        "domains_mapped": len(domains),
        "liveness_counts": dict(sorted(counts.items())),
        "entries_with_email": with_email,
        "entries": entries,
        "domains": domains,
    }
    return report


def write_csvs(report: dict) -> None:
    fields = [
        "id", "name", "domain", "liveness", "liveness_detail", "catalog_url",
        "catalog_http_status", "catalog_final_url", "site_liveness",
        "primary_email", "primary_email_confidence", "email_count", "page_count",
    ]
    with LIVENESS_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in report["entries"].values():
            site = report["domains"].get(item["domain"]) or {}
            writer.writerow({
                "id": item["id"],
                "name": item["name"],
                "domain": item["domain"],
                "liveness": item["liveness"],
                "liveness_detail": item["liveness_detail"],
                "catalog_url": item["catalog_url"],
                "catalog_http_status": item["catalog_http_status"] if item["catalog_http_status"] is not None else "",
                "catalog_final_url": item["catalog_final_url"] or "",
                "site_liveness": item["site_liveness"] or "",
                "primary_email": item["primary_email"] or "",
                "primary_email_confidence": item["primary_email_confidence"] or "",
                "email_count": len(site.get("emails") or []),
                "page_count": len(site.get("pages") or []),
            })
    with EMAILS_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "name", "domain", "email", "confidence", "via", "source_url"])
        writer.writeheader()
        for item in report["entries"].values():
            for email in (report["domains"].get(item["domain"]) or {}).get("emails") or []:
                writer.writerow({
                    "id": item["id"],
                    "name": item["name"],
                    "domain": item["domain"],
                    "email": email["email"],
                    "confidence": email["confidence"],
                    "via": email.get("via") or "",
                    "source_url": email.get("source_url") or "",
                })


def write_doc(report: dict) -> None:
    counts = report["liveness_counts"]
    lines = [
        "# Broker page maps",
        "",
        "These maps were built for the catalog on the check date below. Rows added or removed in `opt-outs.csv` after that date are not in this file until the map is rebuilt.",
        "",
        f"Check date: {report['checked_at']}. Catalog rows: {report['catalog_rows']}. Domains crawled: {report['domains_mapped']}.",
        "",
        report["method"],
        "",
        "`opt-outs.csv` was not rewritten. HTTP 403, 429, and challenge pages stay `blocked`. They are not dead pages.",
        "",
        "Page maps are stored once per domain in [`data/v1/broker-maps.json`](../data/v1/broker-maps.json) under `domains`. "
        "Catalog rows point at that domain. "
        "One row per broker is in [`data/v1/broker-liveness.csv`](../data/v1/broker-liveness.csv). "
        "Every published address is in [`data/v1/broker-emails.csv`](../data/v1/broker-emails.csv). "
        "Rebuild with `python scripts/research/map_brokers.py`. A finished log can be republished with `python scripts/research/map_brokers.py --compile-only`.",
        "",
        "## Liveness",
        "",
        "| Status | Brokers |",
        "| --- | ---: |",
    ]
    for key in ("alive", "blocked", "opt_out_missing", "dead", "likely_dead", "robots_disallowed", "error", "not_checked"):
        if counts.get(key):
            lines.append(f"| `{key}` | {counts[key]} |")
    for key, value in counts.items():
        if key not in {"alive", "blocked", "opt_out_missing", "dead", "likely_dead", "robots_disallowed", "error", "not_checked"}:
            lines.append(f"| `{key}` | {value} |")
    lines.extend([
        "",
        f"Brokers with at least one published email: **{report['entries_with_email']}** of {report['catalog_rows']}.",
        "",
        "`extracted` means the address is on the broker's own domain (or a known parent domain) and the local part is a privacy, opt-out, or removal mailbox. `uncertain` is a generic mailbox such as info@ or support@, or a privacy address on some other domain, and needs a look before you send anything.",
        "",
    ])

    def section(title: str, status: str) -> None:
        chosen = [item for item in report["entries"].values() if item["liveness"] == status]
        lines.extend([f"## {title}", ""])
        if not chosen:
            lines.extend(["None in this pass.", ""])
            return
        lines.extend(["| Broker | Domain | Catalog URL | What we saw |", "| --- | --- | --- | --- |"])
        for item in sorted(chosen, key=lambda row: row["name"].lower()):
            detail = (item["liveness_detail"] or "").replace("|", "/")
            lines.append(f"| {item['name'].replace('|', '/')} | `{item['domain']}` | {item['catalog_url']} | {detail} |")
        lines.append("")

    section("Dead", "dead")
    section("Opt-out URL missing", "opt_out_missing")
    section("Likely dead", "likely_dead")
    section("Fetch errors", "error")

    redirected = [item for item in report["entries"].values() if item.get("redirect")]
    lines.extend([
        "## Redirects",
        "",
        f"{len(redirected)} catalog URLs loaded after a redirect. Same-site path changes: {sum(1 for item in redirected if item['redirect']['same_site'])}. Other host: {sum(1 for item in redirected if not item['redirect']['same_site'])}.",
        "",
    ])
    other = [item for item in redirected if not item["redirect"]["same_site"]]
    if other:
        lines.extend(["| Broker | Catalog URL | Final URL |", "| --- | --- | --- |"])
        for item in sorted(other, key=lambda row: row["name"].lower()):
            final = item["redirect"]["final_url"]
            if len(final) > 140:
                final = final[:137] + "..."
            lines.append(f"| {item['name'].replace('|', '/')} | {item['catalog_url']} | {final} |")
        lines.append("")
    DOC_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def merge_emails(report: dict) -> int:
    """Fill privacy_contact_email where v1 left it empty. This pass's own fills are updated to match the map."""
    if not V1_DATASET.exists() or not FILLS_PATH.exists():
        return 0
    records = {record["id"]: record for record in json.loads(V1_DATASET.read_text(encoding="utf-8"))["records"]}
    fills = json.loads(FILLS_PATH.read_text(encoding="utf-8"))
    added = 0
    stamp = today()
    for item in report["entries"].values():
        if (records.get(item["id"]) or {}).get("discovery", {}).get("privacy_contact_email"):
            continue
        bucket = fills.setdefault("entries", {}).setdefault(item["id"], {})
        current = bucket.get("privacy_contact_email")
        primary = None
        if item.get("primary_email"):
            primary = {
                "value": item["primary_email"],
                "source_url": item.get("primary_email_source_url"),
                "checked_at": stamp,
                "confidence": item.get("primary_email_confidence"),
                "evidence": None,
            }
        if current and current.get("checked_at") == stamp:
            if primary is None:
                bucket.pop("privacy_contact_email", None)
                if not bucket:
                    fills["entries"].pop(item["id"], None)
                added += 1
            elif current.get("value") != primary["value"] or current.get("confidence") != primary["confidence"]:
                bucket["privacy_contact_email"] = primary
                added += 1
            continue
        if primary is None:
            continue
        if current and current.get("confidence") == "extracted":
            continue
        if current and current.get("confidence") == "uncertain" and primary["confidence"] != "extracted":
            continue
        bucket["privacy_contact_email"] = primary
        added += 1
    counts: dict[str, int] = {}
    for entry in fills.get("entries", {}).values():
        for key, value in entry.items():
            if isinstance(value, dict) and "confidence" in value:
                label = f"{key}:{value['confidence']}"
                counts[label] = counts.get(label, 0) + 1
    fills["generated_at"] = utc_now()
    fills["counts"] = dict(sorted(counts.items()))
    fills["entries"] = dict(sorted(fills["entries"].items()))
    FILLS_PATH.write_text(json.dumps(fills, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return added


def publish(raw: dict[str, dict]) -> dict:
    report = compile_maps(raw)
    MAPS_PATH.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    write_csvs(report)
    write_doc(report)
    added = merge_emails(report)
    print(json.dumps({"liveness": report["liveness_counts"], "with_email": report["entries_with_email"], "fills_added": added}, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--limit", type=int, default=0, help="map only the first N domains, in sorted order")
    parser.add_argument("--ids", help="comma-separated catalog ids")
    parser.add_argument("--fresh", action="store_true", help="ignore checkpoint rows for the domains in this run")
    parser.add_argument("--compile-only", action="store_true", help="rebuild the JSON, CSV, and doc from the jsonl log")
    args = parser.parse_args()
    if args.compile_only:
        publish(load_done(LOG_PATH))
        return 0
    raw = scrape(args)
    if args.limit or args.ids:
        print("partial run; compile with --compile-only after the full scrape", flush=True)
        return 0
    publish(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
