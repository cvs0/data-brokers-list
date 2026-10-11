#!/usr/bin/env python3
"""Combine liveness passes into one verdict per catalog row.

A row is only called dead when EVERY pass (at least two, the first and last at
least --min-gap-minutes apart) agrees. Anything else stays in the catalog.

Verdicts:
  alive            page answered (2xx/3xx)
  blocked          403/429/challenge page: the site exists, it just refuses bots
  url_drift        site alive, but the opt-out URL 404s or now lands on another host
  https_upgrade    http:// URL times out but the https site answers
  seized           "transferred by court order" / seizure banner on every pass
  parked           registrar / marketplace / for-sale page on every pass
  dns_dead         no DNS record for the domain on every pass
  gone             opt-out URL and homepage both 404/410 on every pass
  unreachable      connection failures on opt-out URL and homepage on every pass
  flaky            mixed results across passes (kept, recheck later)
Archive-worthy: seized, parked, dns_dead, gone, unreachable.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import urlparse

from catalog import ROOT

LOG = ROOT / "research" / "evidence" / "liveness.jsonl"
REPORT = ROOT / "research" / "evidence" / "liveness-report.json"
ARCHIVE_VERDICTS = {"seized", "parked", "dns_dead", "gone", "unreachable"}


def reg(host: str | None) -> str:
    parts = (host or "").lower().removeprefix("www.").split(".")
    return ".".join(parts[-2:])


def one(rec: dict) -> tuple[str, dict]:
    o = rec.get("optout") or {}
    rt = rec.get("root") or {}
    if not rec.get("dns_ok") and not rec.get("dns_ok_url_host"):
        return "dns_dead", {"error": rec.get("dns_error")}
    if o.get("seized") or rt.get("seized"):
        return "seized", {"title": o.get("title") or rt.get("title")}
    if o.get("parked") or rt.get("parked"):
        return "parked", {"signal": o.get("parked") or rt.get("parked"), "final_url": o.get("final_url") or rt.get("final_url")}
    os_, rs = o.get("status"), rt.get("status")
    if os_ is None and rs is None and rt:
        if rec["catalog_url"].startswith("http://"):
            return "unreachable", {"error": o.get("error")}
        return "unreachable", {"error": o.get("error"), "root_error": rt.get("error")}
    if os_ is None and not rt:
        return "unreachable", {"error": o.get("error")}
    if os_ is None and rs is not None:
        if rec["catalog_url"].startswith("http://") and rs < 400:
            return "https_upgrade", {"new_url": "https://" + rec["catalog_url"][7:]}
        return ("blocked" if rt.get("challenge") else "url_drift"), {"error": o.get("error"), "root_status": rs}
    if o.get("challenge"):
        return "blocked", {"status": os_}
    if os_ in (404, 410):
        if rs in (404, 410) or (rs is None and not rt):
            return "gone", {"status": os_, "root_status": rs}
        if rt.get("challenge"):
            return "blocked", {"status": os_}
        return "url_drift", {"status": os_, "root_status": rs, "root_final_url": rt.get("final_url")}
    if os_ >= 500:
        return "flaky", {"status": os_}
    fin = o.get("final_url") or ""
    if reg(urlparse(fin).hostname) != reg(urlparse(rec["catalog_url"]).hostname):
        return "url_drift", {"status": os_, "final_url": fin, "cross_host": True}
    return "alive", {"status": os_, "final_url": fin if fin.rstrip("/") != rec["catalog_url"].rstrip("/") else None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-gap-minutes", type=int, default=45)
    args = ap.parse_args()
    by_id: dict[str, list[dict]] = defaultdict(list)
    for line in LOG.read_text().splitlines():
        r = json.loads(line)
        by_id[r["id"]].append(r)
    out = {}
    counts: Counter = Counter()
    for rid, recs in sorted(by_id.items()):
        recs.sort(key=lambda r: r["checked_at"])
        verdicts = [one(r) for r in recs]
        kinds = [v for v, _ in verdicts]
        first = datetime.fromisoformat(recs[0]["checked_at"])
        last = datetime.fromisoformat(recs[-1]["checked_at"])
        spaced = len(recs) >= 2 and (last - first).total_seconds() >= args.min_gap_minutes * 60
        last_kind, detail = verdicts[-1]
        if last_kind in ARCHIVE_VERDICTS:
            same = all(k in ARCHIVE_VERDICTS for k in kinds) and len(set(kinds)) == 1
            if not (same and spaced):
                final = "flaky" if spaced else "pending_recheck"
            else:
                final = last_kind
        elif any(k in ARCHIVE_VERDICTS for k in kinds):
            final = last_kind if last_kind != "alive" else "alive"
            detail = {**detail, "earlier_failures": [k for k in kinds if k in ARCHIVE_VERDICTS]}
        else:
            final = last_kind
        counts[final] += 1
        out[rid] = {"name": recs[-1]["name"], "catalog_url": recs[-1]["catalog_url"], "verdict": final,
                    "passes": [{"pass": r.get("pass"), "checked_at": r["checked_at"], "verdict": k} for r, (k, _) in zip(recs, verdicts)],
                    "detail": detail}
    REPORT.write_text(json.dumps({"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                  "min_gap_minutes": args.min_gap_minutes, "archive_verdicts": sorted(ARCHIVE_VERDICTS),
                                  "counts": dict(counts.most_common()), "entries": out}, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(dict(counts.most_common()), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
