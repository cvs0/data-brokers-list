#!/usr/bin/env python3
"""Match catalog rows to the California CPPA data broker registry.

Downloads the CPPA registry CSV (public government data) into
research/registries/, matches each registrant to catalog rows by registrable
domain (website and privacy-rights URL), and writes data/v1/registry-matches.json.

A domain match is the only thing that sets `registered`. Name-only similarity
is written to the review list, never applied.

Fields kept per match: legal name, DBA, website, contact email, privacy-rights
URL, the registrant's self-reported delete-request statistics (mean/median days),
and the sensitive-data flags. No personal data: registrants are companies.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
from datetime import date
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests

from catalog import ROOT, assign_ids, load_catalog

REG_URL = "https://cppa.ca.gov/data_broker_registry/registry.csv"
REG_PAGE = "https://cppa.ca.gov/data_broker_registry/"
OUT = ROOT / "data" / "v1" / "registry-matches.json"
RAW_DIR = ROOT / "research" / "registries"
UA = "Mozilla/5.0 (X11; Linux x86_64) data-brokers-list-research/1.0 (+https://github.com/cvs0/data-brokers-list)"
SHARED_HOSTS = {"onetrust.com", "trustarc.com", "ketch.com", "transcend.io", "osano.com", "truyo.com",
                "securiti.ai", "privacyrights.info", "google.com", "docs.google.com", "forms.gle", "wirewheel.io",
                "datagrail.io", "mineos.ai", "zendesk.com", "jotform.com", "typeform.com", "hubspot.com"}


def reg(host: str | None) -> str:
    parts = (host or "").lower().strip().removeprefix("www.").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def host_from(value: str) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if "://" not in v:
        v = "https://" + v
    try:
        return urlparse(v).hostname
    except ValueError:
        return None


def num(v: str):
    v = (v or "").replace(",", "").strip()
    try:
        f = float(v)
        return int(f) if f.is_integer() else round(f, 1)
    except ValueError:
        return None


def yes(v: str):
    v = (v or "").strip().lower()
    return True if v.startswith("yes") else False if v.startswith("no") else None


def norm_name(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|lp|l\.l\.c|plc|limited|dba|d/b/a)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true", help="use the newest file in research/registries/")
    args = ap.parse_args()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if args.offline:
        path = sorted(RAW_DIR.glob("cppa-*.csv"))[-1]
        text = path.read_text(encoding="utf-8-sig")
    else:
        r = requests.get(REG_URL, headers={"User-Agent": UA}, timeout=60)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig", errors="replace")
        path = RAW_DIR / f"cppa-{date.today().isoformat()}.csv"
        path.write_text(text, encoding="utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    header, data = rows[0], [r for r in rows[1:] if any(c.strip() for c in r)]
    col = {h.strip().rstrip(":"): i for i, h in enumerate(header)}

    def get(r, startswith):
        for h, i in col.items():
            if h.startswith(startswith):
                return r[i] if i < len(r) else ""
        return ""

    catalog = assign_ids(load_catalog())
    by_dom: dict[str, list[str]] = {}
    for row in catalog:
        url_dom = reg(host_from(row["url"]))
        for d in {url_dom if url_dom not in SHARED_HOSTS else "", reg(row["domain"])}:
            if d:
                by_dom.setdefault(d, []).append(row["id"])
    matches: dict[str, dict] = {}
    review = []
    for r in data:
        name = get(r, "Data broker name").strip()
        dba = get(r, "Doing Business As").strip()
        site = get(r, "Data broker primary website").strip()
        rights = get(r, "Data broker's primary website that contains details").strip()
        site_doms = {reg(host_from(x)) for x in re.split(r"[;,\s]+", site) if x}
        rights_doms = {reg(host_from(x)) for x in re.split(r"[;,\s]+", rights) if x} - SHARED_HOSTS
        doms = (site_doms | rights_doms) - {"", None}
        entry = {
            "registry": "ca_cppa",
            "registry_year": 2026,
            "legal_name": name,
            "dba": dba or None,
            "website": site or None,
            "contact_email": get(r, "Data broker primary contact email").strip().lower() or None,
            "privacy_rights_url": rights or None,
            "country": get(r, "Data broker country").strip() or None,
            "delete_requests_received": num(get(r, "Requests to delete - Total requests received")),
            "delete_days_mean": num(get(r, "Requests to delete - The number of days to respond substantively to a request to delete in 2024 - Mean")),
            "delete_days_median": num(get(r, "Requests to delete - The number of days to respond substantively to a request to delete in 2024 - Median")),
            "collects_precise_geolocation": yes(get(r, "Data broker collects consumers' precise geolocation")),
            "collects_minors": yes(get(r, "Data broker collects personal information of minors")),
            "fcra_regulated": yes(get(r, "The data broker or any of its subsidiaries is regulated by the federal Fair Credit")),
            "source_url": REG_PAGE,
            "checked_at": date.today().isoformat(),
            "match": "domain",
        }
        hit = False
        for d in doms:
            for cid in by_dom.get(d, []):
                prev = matches.get(cid)
                if prev is None or (prev.get("dba") is None and dba):
                    matches[cid] = entry
                hit = True
        if not hit:
            nn = norm_name(dba or name)
            best = max(((SequenceMatcher(None, nn, norm_name(c["name"])).ratio(), c["id"]) for c in catalog), default=(0, None))
            if best[0] >= 0.85:
                review.append({"registrant": name, "dba": dba or None, "website": site, "candidate_id": best[1], "similarity": round(best[0], 3)})
    out = {
        "registry": "ca_cppa",
        "source_url": REG_PAGE,
        "raw_file": str(path.relative_to(ROOT)),
        "registrants": len(data),
        "matched_catalog_rows": len(matches),
        "note": "registered=true only on a domain match. Name-only candidates are in review and are NOT applied.",
        "matches": dict(sorted(matches.items())),
        "review": sorted(review, key=lambda x: -x["similarity"]),
    }
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("registrants", "matched_catalog_rows")} | {"review": len(review)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
