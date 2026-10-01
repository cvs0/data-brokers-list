#!/usr/bin/env python3
"""Recheck catalog opt-out URLs for HTTP status and redirect drift.

Does not submit forms. A 403 or 429 is an access observation, not proof that
a residential proxy is required and not proof that the page is dead.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from catalog import ROOT, assign_ids, load_catalog

OUT_LOG = ROOT / "research" / "evidence" / "freshness.jsonl"
OUT_REPORT = ROOT / "data" / "v1" / "freshness-report.json"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 14
MAX_BYTES = 80_000
WORKERS = 8
HOST_DELAY = 0.45

CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies",
    "sorry, you have been blocked",
    "access denied",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme.lower()}://{host}{path}{query}"


def same_site(left: str, right: str) -> bool:
    a = urlparse(left).hostname or ""
    b = urlparse(right).hostname or ""
    a = a.lower().removeprefix("www.")
    b = b.lower().removeprefix("www.")
    return a == b or a.endswith("." + b) or b.endswith("." + a)


class HostGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        with self._lock:
            now = time.monotonic()
            ready = self._next.get(host, now)
            delay = max(0.0, ready - now)
            self._next[host] = max(now, ready) + HOST_DELAY
        if delay:
            time.sleep(delay)


def fetch(url: str, gate: HostGate) -> dict:
    gate.wait(url)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    context = ssl.create_default_context()
    started = time.monotonic()
    try:
        with urlopen(request, timeout=TIMEOUT, context=context) as response:
            status = response.status
            final_url = response.geturl()
            raw = response.read(MAX_BYTES)
            content_type = (response.headers.get("Content-Type") or "").lower()
    except HTTPError as exc:
        status = exc.code
        final_url = exc.geturl() or url
        try:
            raw = exc.read(MAX_BYTES)
        except Exception:
            raw = b""
        content_type = (exc.headers.get("Content-Type") or "").lower() if exc.headers else ""
        error = None
    except Exception as exc:  # URLError, timeout, SSL
        return {
            "url": url,
            "checked_at": utc_now(),
            "http_status": None,
            "final_url": None,
            "content_type": None,
            "text_sample": "",
            "content_quality": "error",
            "error": type(exc).__name__ + ": " + str(exc)[:240],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    else:
        error = None
    text = raw.decode("utf-8", errors="replace")
    lowered = text.lower()
    if "html" not in content_type and not lowered.lstrip().startswith("<!") and "<html" not in lowered[:500]:
        quality = "non_html"
    elif status in {404, 410}:
        quality = "error"
    elif status in {401, 403, 429, 503} or any(marker in lowered for marker in CHALLENGE_MARKERS):
        quality = "challenge" if any(marker in lowered for marker in CHALLENGE_MARKERS) or status in {403, 429, 503} else "error"
    elif len(text) < 400:
        quality = "empty_shell"
    else:
        quality = "readable"
    return {
        "url": url,
        "checked_at": utc_now(),
        "http_status": status,
        "final_url": final_url,
        "content_type": content_type[:120],
        "text_sample": " ".join(text.split())[:280],
        "content_quality": quality,
        "error": error,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def classify(row: dict, catalog_status: str) -> str:
    status = row.get("http_status")
    final_url = row.get("final_url") or ""
    quality = row.get("content_quality")
    if row.get("error") or status is None:
        return "error"
    if status in {404, 410}:
        return "dead"
    if quality == "challenge" or status in {401, 403, 429, 503}:
        return "blocked"
    original = row["url"]
    if final_url and norm_url(final_url) != norm_url(original):
        return "redirect_same_site" if same_site(original, final_url) else "redirect_other_host"
    catalog_code = catalog_status.strip()
    if catalog_code.isdigit() and int(catalog_code) != status:
        return "status_changed"
    return "unchanged"


def main() -> None:
    rows = assign_ids(load_catalog())
    by_url: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_url[row["url"]].append(row)
    urls = list(by_url)
    gate = HostGate()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch, url, gate): url for url in urls}
        done = 0
        for future in as_completed(futures):
            item = future.result()
            results.append(item)
            done += 1
            if done % 50 == 0 or done == len(urls):
                print(f"checked {done}/{len(urls)}", flush=True)
    results.sort(key=lambda item: item["url"])
    OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with OUT_LOG.open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    flags = []
    counts: Counter[str] = Counter()
    for item in results:
        members = by_url[item["url"]]
        catalog_status = members[0]["http_status"]
        drift = classify(item, catalog_status)
        counts[drift] += 1
        if drift == "unchanged":
            continue
        flags.append(
            {
                "url": item["url"],
                "drift": drift,
                "catalog_http_status": catalog_status,
                "observed_http_status": item.get("http_status"),
                "final_url": item.get("final_url"),
                "content_quality": item.get("content_quality"),
                "error": item.get("error"),
                "entry_ids": [member["id"] for member in members],
                "names": [member["name"] for member in members],
            }
        )
    report = {
        "check_date": "2026-10-01",
        "checked_at": utc_now(),
        "method": (
            "Direct HTTP GET from the research environment, 8 workers, 14s timeout, "
            "per-host delay, no proxy, no form submission. Catalog http_status in opt-outs.csv "
            "was not rewritten. A blocked result is an access observation, not evidence that a "
            "residential proxy is required."
        ),
        "catalog_rows": len(rows),
        "unique_urls": len(urls),
        "drift_counts": dict(sorted(counts.items())),
        "flagged": flags,
    }
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["drift_counts"], indent=2))


if __name__ == "__main__":
    main()
