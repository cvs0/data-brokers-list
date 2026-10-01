#!/usr/bin/env python3
"""Fetch each catalog URL once and store extracted evidence.

This does not submit forms, send email, create accounts, or solve CAPTCHAs.
A 403 or 429 is recorded as an access observation. Re-running skips URLs
already present in the evidence log unless --refetch is set.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import ssl
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from catalog import ROOT, assign_ids, load_catalog

EVIDENCE_PATH = ROOT / "research" / "evidence" / "url-evidence.jsonl"
CHECKPOINT_DIR = ROOT / "research" / "checkpoints"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_BYTES = 1_000_000
TIMEOUT = 18
HOST_DELAY = 0.6

PILOT_URLS = [
    "https://www.beenverified.com/app/optout/search",
    "https://suppression.peopleconnect.us/",
    "https://www.truthfinder.com/opt-out/",
    "https://www.spokeo.com/optout",
    "https://www.whitepages.com/suppression-requests",
    "https://www.truepeoplesearch.com/removal",
    "https://www.fastpeoplesearch.com/optout",
    "https://radaris.com/control/privacy",
    "https://www.acxiom.com/optout/",
    "https://liveramp.com/privacy/my-privacy-choices/",
    "https://adsrvr.org/",
    "https://www.dmachoice.org/",
    "https://www.youronlinechoices.com/uk/your-ad-choices",
    "https://www.youradchoices.ca/choices",
    "https://www.lnnte-dncl.gc.ca/en/",
    "https://www.gov.uk/electoral-register/opt-out-of-the-open-register",
    "https://privacy.ca.gov/drop/",
    "https://www.donotcall.gov/",
    "https://myprivacy.equifax.com/opt-in-opt-out/personal-info",
    "https://consumer.risk.lexisnexis.com/opt",
    "https://www.optoutprescreen.com/",
]

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
SKIP_EMAIL_PARTS = (
    "example.com",
    "sentry.io",
    "wixpress.com",
    "schema.org",
    "godaddy.com",
    "cloudflare.com",
    "jquery.com",
    "png",
    "webp",
    "jpg",
    "gif",
)

QUOTE_TOPICS = {
    "processing_time": (
        "business day",
        "hours",
        "within",
        "processed",
        "processing",
    ),
    "reappearance": ("reappear", "re-appear", "appear again", "new records", "future"),
    "authorized_agent": ("authorized agent", "authorised agent", "authorized representative"),
    "captcha": ("captcha", "recaptcha", "hcaptcha", "turnstile"),
    "email_verification": (
        "confirmation email",
        "confirm your email",
        "click the link",
        "verification code",
        "email a code",
    ),
    "identity_document": (
        "driver",
        "passport",
        "government-issued",
        "government issued",
        "photo id",
        "identity document",
        "redact",
    ),
    "request_limit": (
        "one listing",
        "each listing",
        "individually",
        "per email",
        "abuse",
        "limit",
    ),
    "eligibility": (
        "resident",
        "california",
        "eligible",
        "nationwide",
        "united states",
        "if you live",
    ),
    "phone_sms": ("text message", "sms", "one-time", "mobile"),
    "login": ("sign in", "log in", "create an account", "login"),
}


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False
        self.forms: list[dict] = []
        self.current_form: dict | None = None
        self.links: list[dict] = []
        self.home_link = False

    def handle_starttag(self, tag, attrs) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
        if tag == "form":
            self.current_form = {
                "action": attr.get("action", ""),
                "method": (attr.get("method") or "get").lower(),
                "fields": [],
            }
            self.forms.append(self.current_form)
        if tag in {"input", "textarea", "select"} and self.current_form is not None:
            field_type = (attr.get("type") or ("select" if tag == "select" else "text")).lower()
            if field_type in {"hidden", "submit", "button", "image"}:
                return
            self.current_form["fields"].append(
                {
                    "tag": tag,
                    "type": field_type,
                    "name": attr.get("name", ""),
                    "id": attr.get("id", ""),
                    "required": "required" in attr or attr.get("aria-required", "").lower() == "true",
                    "placeholder": attr.get("placeholder", "")[:160],
                    "aria_label": attr.get("aria-label", "")[:160],
                }
            )
        if tag == "a":
            href = attr.get("href", "")
            text_hint = attr.get("aria-label", "") or attr.get("title", "")
            self.links.append({"href": href[:500], "hint": text_hint[:120]})
            if href in {"/", "#"} or href.rstrip("/").endswith("://") or text_hint.lower() == "home":
                self.home_link = True
            parsed = urlparse(href)
            if parsed.path in {"", "/"} and not parsed.query and href.startswith("http"):
                self.home_link = True

    def handle_endtag(self, tag) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "title":
            self.in_title = False
        if tag == "form":
            self.current_form = None

    def handle_data(self, data) -> None:
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        text = data.strip()
        if text:
            self.parts.append(text)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_done(path: Path) -> set[str]:
    done = set()
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
            if item.get("url"):
                done.add(item["url"])
    return done


def decode_body(raw: bytes, encoding: str | None) -> str:
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    for codec in (encoding or "", "utf-8", "latin-1"):
        if not codec:
            continue
        try:
            return raw.decode(codec, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_url(url: str) -> dict:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.8,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.8",
            "Accept-Encoding": "gzip",
        },
        method="GET",
    )
    context = ssl.create_default_context()
    redirects: list[str] = []
    # urllib follows redirects and exposes the final URL via geturl().
    started = time.time()
    try:
        with urlopen(request, timeout=TIMEOUT, context=context) as response:
            status = getattr(response, "status", None) or response.getcode()
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            charset = response.headers.get_content_charset()
            raw = response.read(MAX_BYTES)
            header_blob = " ".join(
                f"{key}:{value}" for key, value in response.headers.items()
            )
    except HTTPError as exc:
        status = exc.code
        final_url = exc.geturl() or url
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        charset = exc.headers.get_content_charset() if exc.headers else None
        try:
            raw = exc.read(MAX_BYTES) if exc.fp else b""
        except Exception:
            raw = b""
        header_blob = ""
        if exc.headers:
            header_blob = " ".join(f"{key}:{value}" for key, value in exc.headers.items())
        error = None
    except URLError as exc:
        return _error_record(url, f"url_error:{exc.reason}", started)
    except TimeoutError:
        return _error_record(url, "timeout", started)
    except Exception as exc:  # noqa: BLE001 — record and continue
        return _error_record(url, f"{type(exc).__name__}:{exc}", started)
    else:
        error = None

    if final_url and final_url != url:
        redirects.append(final_url)

    text = decode_body(raw, charset)
    lowered = text.lower()
    headers_l = header_blob.lower()
    challenge = False
    challenge_vendor = None
    if (
        "cf-mitigated" in headers_l
        or "performing security verification" in lowered
        or ("just a moment" in lowered and "cloudflare" in lowered)
        or ("attention required" in lowered and "cloudflare" in lowered)
        or "enable javascript and cookies" in lowered and "cloudflare" in lowered
    ):
        challenge = True
        challenge_vendor = "cloudflare"

    content_type_l = content_type.lower()
    non_html = "pdf" in content_type_l or text.lstrip().startswith("%PDF")
    parser = PageParser()
    title = ""
    visible = ""
    forms: list[dict] = []
    links: list[dict] = []
    home_link = False
    if not non_html:
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            pass
        title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()[:300]
        visible = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
        forms = parser.forms[:8]
        for form in forms:
            form["fields"] = form["fields"][:40]
        links = parser.links[:80]
        home_link = parser.home_link

    emails = []
    for match in EMAIL_RE.findall(visible):
        mail = match.strip(".").lower()
        if any(part in mail for part in SKIP_EMAIL_PARTS):
            continue
        if mail not in emails:
            emails.append(mail)
        if len(emails) >= 12:
            break

    raw_l = text.lower()
    captcha_markers = []
    for marker in (
        "g-recaptcha",
        "recaptcha",
        "hcaptcha",
        "h-captcha",
        "cf-turnstile",
        "turnstile",
        "data-sitekey",
        "funcaptcha",
        "arkoselabs",
        "captcha",
    ):
        if marker in raw_l and marker not in captcha_markers:
            captcha_markers.append(marker)

    platform_markers = []
    for marker in ("onetrust", "saymine", "trustarc", "transcend", "ketch", "osano", "cookiebot"):
        if marker in raw_l:
            platform_markers.append(marker)

    quotes = extract_quotes(visible)
    privacy_links = []
    for link in links:
        href = link["href"]
        blob = (href + " " + link["hint"]).lower()
        if "privacy" in blob and href and not href.startswith(("#", "javascript:")):
            privacy_links.append(urljoin(final_url or url, href)[:500])
        if len(privacy_links) >= 6:
            break

    homepage = None
    if home_link and final_url:
        parsed = urlparse(final_url)
        if parsed.scheme and parsed.netloc:
            homepage = f"{parsed.scheme}://{parsed.netloc}/"

    field_names = []
    for form in forms:
        for field in form["fields"]:
            label = field.get("name") or field.get("id") or field.get("aria_label") or field.get("placeholder")
            if label and label not in field_names:
                field_names.append(label)

    return {
        "url": url,
        "fetched_at": utc_now(),
        "elapsed_ms": int((time.time() - started) * 1000),
        "request_user_agent": USER_AGENT,
        "http_status": status,
        "error": error,
        "final_url": final_url,
        "redirects": redirects,
        "content_type": content_type.split(";")[0][:120],
        "bytes_read": len(raw),
        "challenge": challenge,
        "challenge_vendor": challenge_vendor,
        "non_html": non_html,
        "title": title,
        "text_excerpt": smart_excerpt(visible),
        "text_length": len(visible),
        "emails": emails,
        "forms": forms,
        "form_field_names": field_names[:40],
        "captcha_markers": captcha_markers,
        "platform_markers": platform_markers,
        "privacy_policy_links": privacy_links,
        "official_homepage_candidate": homepage,
        "quotes": quotes,
        "batch_id": None,
    }


def _error_record(url: str, error: str, started: float) -> dict:
    return {
        "url": url,
        "fetched_at": utc_now(),
        "elapsed_ms": int((time.time() - started) * 1000),
        "request_user_agent": USER_AGENT,
        "http_status": None,
        "error": error[:300],
        "final_url": None,
        "redirects": [],
        "content_type": "",
        "bytes_read": 0,
        "challenge": False,
        "challenge_vendor": None,
        "non_html": False,
        "title": "",
        "text_excerpt": "",
        "text_length": 0,
        "emails": [],
        "forms": [],
        "form_field_names": [],
        "captcha_markers": [],
        "platform_markers": [],
        "privacy_policy_links": [],
        "official_homepage_candidate": None,
        "quotes": [],
        "batch_id": None,
    }


def smart_excerpt(visible: str, limit: int = 14000) -> str:
    """Keep the opening plus windows around request language so later sections survive."""
    if len(visible) <= limit:
        return visible
    chunks = [visible[:4500]]
    lowered = visible.lower()
    needles = (
        "opt-out",
        "opt out",
        "delete",
        "do not sell",
        "do-not-sell",
        "telephone",
        "call us",
        "email",
        "by mail",
        "captcha",
        "authorized agent",
        "business day",
        "driver",
        "privacy request",
        "suppression",
    )
    for needle in needles:
        start = 0
        found = 0
        while found < 2:
            index = lowered.find(needle, start)
            if index < 0:
                break
            chunks.append(visible[max(0, index - 160) : index + 460])
            start = index + len(needle)
            found += 1
    return "\n".join(chunks)[:limit]


def extract_quotes(visible: str) -> list[dict]:
    if not visible:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", visible)
    selected: list[dict] = []
    used = set()
    for topic, needles in QUOTE_TOPICS.items():
        count = 0
        for sentence in sentences:
            lowered = sentence.lower()
            if not any(needle in lowered for needle in needles):
                continue
            if topic == "processing_time" and not re.search(
                r"\b(\d+|one|two|three|five|ten)\b", lowered
            ):
                continue
            snippet = sentence.strip()[:500]
            key = (topic, snippet)
            if key in used:
                continue
            used.add(key)
            selected.append({"topic": topic, "text": snippet})
            count += 1
            if count >= 2:
                break
        if len(selected) >= 24:
            break
    return selected


def unique_catalog_urls() -> list[str]:
    rows = assign_ids(load_catalog())
    seen = set()
    ordered = []
    for row in rows:
        if row["url"] not in seen:
            seen.add(row["url"])
            ordered.append(row["url"])
    return ordered


def batched(urls: list[str], size: int) -> list[list[str]]:
    return [urls[index : index + size] for index in range(0, len(urls), size)]


def write_checkpoint(batch_name: str, payload: dict) -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = CHECKPOINT_DIR / f"{batch_name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch catalog URL evidence.")
    parser.add_argument("--batch", default="all", help="pilot, all, or a batch id like batch-001")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    urls = unique_catalog_urls()
    if args.batch == "pilot":
        selected = [url for url in PILOT_URLS if url in set(urls)]
        missing = [url for url in PILOT_URLS if url not in set(urls)]
        if missing:
            raise SystemExit(f"Pilot URLs missing from catalog: {missing}")
        batches = [("pilot", selected)]
    elif args.batch == "all":
        batches = [
            (f"batch-{index + 1:03d}", group)
            for index, group in enumerate(batched(urls, args.batch_size))
        ]
    else:
        # Resume one named batch from the deterministic split.
        groups = {
            f"batch-{index + 1:03d}": group
            for index, group in enumerate(batched(urls, args.batch_size))
        }
        if args.batch == "pilot":
            groups = {"pilot": PILOT_URLS}
        if args.batch not in groups and args.batch != "pilot":
            raise SystemExit(f"Unknown batch {args.batch}. Known: pilot, all, {', '.join(groups)}")
        batches = [(args.batch, groups.get(args.batch, PILOT_URLS))]

    if args.limit:
        limited = []
        left = args.limit
        for name, group in batches:
            take = group[:left]
            if take:
                limited.append((name, take))
            left -= len(take)
            if left <= 0:
                break
        batches = limited

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    done = set() if args.refetch else load_done(EVIDENCE_PATH)
    lock = threading.Lock()
    host_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
    host_next: dict[str, float] = defaultdict(float)

    def run_one(url: str, batch_name: str) -> dict:
        host = urlparse(url).netloc.lower()
        with host_locks[host]:
            wait = host_next[host] - time.time()
            if wait > 0:
                time.sleep(wait)
            record = fetch_url(url)
            # One retry for rate limits and transient network errors. Never retry a 403.
            if record["http_status"] in {429, 503} or (
                record["error"] and record["http_status"] is None
            ):
                time.sleep(4)
                retried = fetch_url(url)
                retried["retried"] = True
                record = retried
            host_next[host] = time.time() + HOST_DELAY
        record["batch_id"] = batch_name
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with lock:
            with EVIDENCE_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record

    for batch_name, group in batches:
        pending = [url for url in group if url not in done]
        started = utc_now()
        print(f"{batch_name}: {len(pending)} to fetch, {len(group) - len(pending)} cached", flush=True)
        results = []
        if pending:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(run_one, url, batch_name): url for url in pending}
                for future in as_completed(futures):
                    record = future.result()
                    results.append(record)
                    done.add(record["url"])
                    status = record["http_status"]
                    quality = "challenge" if record["challenge"] else record["error"] or status
                    print(
                        f"  {quality} {record['text_length']:5} {record['url'][:90]}",
                        flush=True,
                    )
        counts: dict[str, int] = defaultdict(int)
        for record in results:
            if record["challenge"]:
                counts["challenge"] += 1
            elif record["error"]:
                counts["error"] += 1
            elif record["http_status"] == 200:
                counts["http_200"] += 1
            else:
                counts[f"http_{record['http_status']}"] += 1
        write_checkpoint(
            batch_name,
            {
                "batch": batch_name,
                "started_at": started,
                "finished_at": utc_now(),
                "urls_in_batch": len(group),
                "fetched_this_run": len(results),
                "skipped_cached": len(group) - len(pending),
                "counts": counts,
                "requests_submitted": False,
            },
        )
        print(f"{batch_name} checkpoint written {dict(counts)}", flush=True)


if __name__ == "__main__":
    main()
