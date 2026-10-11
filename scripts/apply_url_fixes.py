#!/usr/bin/env python3
"""Apply a reviewed URL-fix file (research/manual/url-fixes-*.json) to the catalog.

Keeps each row's stable id: opt-outs.csv, data/v1/catalog-ids.csv and the v1
record's catalog block are updated together, and the note says what changed.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

from catalog import CATALOG_COLUMNS, CATALOG_PATH, ROOT, assign_ids, load_catalog


def main(path: str) -> int:
    doc = json.loads(Path(path).read_text())
    fixes = {f["id"]: f for f in doc["fixes"]}
    rows = assign_ids(load_catalog())
    by_id = {r["id"]: r for r in rows}
    domains = {r["domain"] for r in rows}
    missing = set(fixes) - set(by_id)
    if missing:
        raise SystemExit(f"unknown ids: {sorted(missing)}")
    changed = {}
    for rid, f in fixes.items():
        r = by_id[rid]
        new_url = f.get("url") or ("https://" + r["url"][7:] if f.get("https") and r["url"].startswith("http://") else r["url"])
        new_dom = f.get("domain") or r["domain"]
        if new_dom != r["domain"] and new_dom in domains:
            raise SystemExit(f"{rid}: domain {new_dom} already used by another row")
        if new_url == r["url"] and new_dom == r["domain"]:
            continue
        note = f"Rechecked {doc['checked_at']}: {f['why']}"
        changed[rid] = (r["url"], new_url, r["domain"], new_dom)
        r["url"], r["domain"], r["http_status"] = new_url, new_dom, f.get("http_status", r["http_status"])
        r["notes"] = (r["notes"].rstrip() + " " + note).strip()
        r["sources"] = r["sources"] if "manual" in r["sources"].split(";") else (r["sources"] + ";manual").strip(";")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CATALOG_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r[k] for k in CATALOG_COLUMNS})
    CATALOG_PATH.write_text(buf.getvalue(), encoding="utf-8")
    ids_path = ROOT / "data" / "v1" / "catalog-ids.csv"
    lines = ids_path.read_text(encoding="utf-8").splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        rid = line.split(",", 1)[0]
        if rid in changed:
            r = by_id[rid]
            name = r["name"].replace('"', '""')
            line = f'{rid},"{name}",{r["category"]},{r["domain"]},"{r["url"]}"'
        out.append(line)
    ids_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    ds_path = ROOT / "data" / "v1" / "broker-opt-outs.enriched.json"
    ds = json.loads(ds_path.read_text())
    for rec in ds["records"]:
        if rec["id"] in changed:
            r = by_id[rec["id"]]
            rec["catalog"].update(url=r["url"], domain=r["domain"], notes=r["notes"], http_status=r["http_status"],
                                  sources=r["sources"].split(";"))
            if r["domain"] not in rec["identity"]["domains"]:
                rec["identity"]["domains"].append(r["domain"])
    ds_path.write_text(json.dumps(ds, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"changed": len(changed)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
