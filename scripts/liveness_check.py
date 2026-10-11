#!/usr/bin/env python3
"""Polite liveness pass over every catalog domain and opt-out URL.

One pass = one observation per catalog row, appended to
research/evidence/liveness.jsonl. Classification (scripts/classify_liveness.py)
needs at least two passes spaced apart before it calls anything dead, so a
single flaky night never removes a broker.

Rules this script keeps:
- real browser-like User-Agent plus a contact string, no proxy, no cookies kept;
- per-host spacing (HOST_DELAY) and a small worker pool;
- a short timeout and two retries with backoff on network errors and 5xx;
- GET with a capped body (needed to spot parking pages); no forms submitted;
- a 403/429/challenge page is "blocked", which is a sign of life, never death.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import urllib3

from catalog import ROOT, assign_ids, load_catalog

urllib3.disable_warnings()

OUT_LOG = ROOT / "research" / "evidence" / "liveness.jsonl"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36 data-brokers-list-liveness/1.0 "
    "(+https://github.com/cvs0/data-brokers-list)"
)
TIMEOUT = (8, 15)
MAX_BYTES = 120_000
HOST_DELAY = 1.0
RETRIES = 2

# Phrases that show a registrar/marketplace parking or for-sale page.
PARKED_MARKERS = (
    "this domain is for sale",
    "domain is for sale",
    "buy this domain",
    "this domain may be for sale",
    "the domain name is for sale",
    "make an offer on this domain",
    "is available for purchase",
    "domain has expired",
    "this domain has expired",
    "domain name has expired",
    "parked free, courtesy of",
    "this domain is parked",
    "domain parking",
    "parkingcrew",
    "sedoparking",
    "bodis.com",
    "afternic",
    "hugedomains",
    "dan.com/buy-domain",
    "godaddy.com/domainsearch",
    "is registered at namecheap",
    "this web page is parked",
    "related searches",  # common on ad-parked lander pages, combined below
)
PARKED_HOSTS = (
    "sedo.com", "dan.com", "afternic.com", "hugedomains.com", "bodis.com",
    "parkingcrew.net", "godaddy.com", "namecheap.com", "domainmarket.com",
    "porkbun.com", "atom.com", "squadhelp.com", "buydomains.com", "undeveloped.com",
)
SEIZED_MARKERS = (
    "transferred by court order", "this domain has been seized", "has been seized by",
    "this website has been seized", "domain seized",
)
CHALLENGE_MARKERS = (
    "just a moment", "attention required", "cf-browser-verification",
    "verify you are human", "checking your browser", "enable javascript and cookies",
    "sorry, you have been blocked", "access denied", "px-captcha", "captcha-delivery",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def bare(host: str) -> str:
    return host.removeprefix("www.")


class HostGate:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            ready = self._next.get(host, now)
            self._next[host] = max(ready, now) + self.delay
        time.sleep(max(0.0, ready - now))


def dns_ok(domain: str) -> tuple[bool, str | None]:
    errors = []
    for name in (domain, "www." + domain):
        try:
            socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
            return True, None
        except socket.gaierror as exc:
            errors.append(f"{name}: {exc.strerror or exc}")
    return False, "; ".join(errors)


def parked_signal(text: str, final_url: str, title: str = "") -> str | None:
    """Return a parking/for-sale signal, or None.

    Real privacy pages often mention registrars or "domain parking" in passing,
    so a phrase only counts on a short page whose title is not a privacy page.
    """
    low = text.lower()
    visible = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text))
    short = len(re.sub(r"\s+", " ", visible)) < 6000
    privacy_title = re.search(r"privacy|opt[- ]?out|remov|policy|choices|request|rights", title or "", re.I)
    fh = bare(host_of(final_url))
    for ph in PARKED_HOSTS:
        if fh == ph or fh.endswith("." + ph):
            return f"redirected to marketplace {ph}"
    strong = [m for m in PARKED_MARKERS if m != "related searches" and m in low]
    if strong and short and not privacy_title:
        return strong[0]
    if "related searches" in low and short and not privacy_title:
        return "ad-parked lander (related searches)"
    return None


def fetch(session: requests.Session, gate: HostGate, url: str) -> dict:
    out: dict = {"url": url}
    last_exc = None
    for attempt in range(RETRIES + 1):
        gate.wait(host_of(url))
        try:
            with session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True, verify=True) as r:
                body = b""
                for chunk in r.iter_content(16_384):
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
                text = body.decode("utf-8", errors="replace")
                out.update(
                    status=r.status_code,
                    final_url=r.url,
                    redirects=len(r.history),
                    retry_after=r.headers.get("retry-after"),
                )
                title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
                out["title"] = re.sub(r"\s+", " ", title.group(1)).strip()[:160] if title else None
                low = text.lower()
                out["challenge"] = r.status_code in (401, 403, 429, 503) or any(m in low for m in CHALLENGE_MARKERS)
                out["parked"] = parked_signal(text, r.url, out["title"] or "")
                out["seized"] = next((m for m in SEIZED_MARKERS if m in low), None)
                if r.status_code >= 500 and attempt < RETRIES:
                    time.sleep(3 * (attempt + 1))
                    continue
                return out
        except requests.exceptions.SSLError as exc:
            # Retry once without verification so an expired cert is recorded, not mistaken for death.
            last_exc = exc
            out["tls_error"] = str(exc)[:200]
            try:
                gate.wait(host_of(url))
                r = session.get(url, timeout=TIMEOUT, allow_redirects=True, verify=False)
                out.update(status=r.status_code, final_url=r.url, redirects=len(r.history))
                out["parked"] = parked_signal(r.text[:MAX_BYTES], r.url)
                out["challenge"] = r.status_code in (401, 403, 429, 503)
                return out
            except requests.RequestException as exc2:
                last_exc = exc2
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < RETRIES:
            time.sleep(3 * (attempt + 1))
    out.update(status=None, error=type(last_exc).__name__ + ": " + str(last_exc)[:240])
    return out


def check_row(session, gate, row) -> dict:
    domain = bare((row["domain"] or host_of(row["url"])).lower())
    rec = {"id": row["id"], "name": row["name"], "domain": domain, "catalog_url": row["url"], "checked_at": utc_now()}
    ok, err = dns_ok(domain)
    rec["dns_ok"] = ok
    if not ok:
        # also try the URL host if it differs (e.g. a subdomain on another apex)
        uh = bare(host_of(row["url"]))
        if uh and uh != domain:
            ok2, _ = dns_ok(uh)
            rec["dns_ok_url_host"] = ok2
            if not ok2:
                rec["dns_error"] = err
                return rec
        else:
            rec["dns_error"] = err
            return rec
    rec["optout"] = fetch(session, gate, row["url"])
    root = f"https://{domain}/"
    if bare(host_of(row["url"])) != domain or urlparse(row["url"]).path not in ("", "/"):
        rec["root"] = fetch(session, gate, root)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--ids", help="comma-separated ids to check (default: all)")
    ap.add_argument("--pass-label", required=True, help="label stored on each record, e.g. 2026-10-11-a")
    args = ap.parse_args()

    rows = assign_ids(load_catalog())
    if args.ids:
        want = set(args.ids.split(","))
        rows = [r for r in rows if r["id"] in want]
    gate = HostGate(HOST_DELAY)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-CA,en;q=0.9"})
    adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    with OUT_LOG.open("a", encoding="utf-8") as log, ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(check_row, session, gate, r): r for r in rows}
        for fut in as_completed(futs):
            rec = fut.result()
            rec["pass"] = args.pass_label
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            done += 1
            if done % 100 == 0:
                print(f"{done}/{len(rows)}", flush=True)
    print(json.dumps({"checked": done, "log": str(OUT_LOG.relative_to(ROOT))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
