#!/usr/bin/env python3
"""Move dead catalog rows into data/archived.json and update every derived file.

Input: research/evidence/liveness-report.json (scripts/classify_liveness.py).
Only verdicts in ARCHIVE_VERDICTS are archived, and only when every liveness pass
agreed (the classifier enforces two or more spaced passes). Pass --ids to limit,
--dry-run to print the plan.

Updates together, so validators keep passing:
  opt-outs.csv, scripts/catalog.py EXPECTED_CATALOG_ROWS, data/v1/catalog-ids.csv,
  data/v1/*.json (records, member lists, counts), data/v2 specs + priority files,
  overlays (field-fills, registry-matches), brokers/*.md, README/SOURCES counts.
Nothing is deleted from history: each archived row keeps its full catalog row,
its v1 id, the reason, the date and the liveness evidence.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
from collections import Counter
from datetime import date

from catalog import CATALOG_COLUMNS, CATALOG_PATH, ROOT, assign_ids, load_catalog

REPORT = ROOT / "research" / "evidence" / "liveness-report.json"
ARCHIVE = ROOT / "data" / "archived.json"
ARCHIVE_VERDICTS = {"seized", "parked", "dns_dead", "gone", "unreachable"}
V1 = ROOT / "data" / "v1"
V2 = ROOT / "data" / "v2"
CATEGORY_FILES = {
    "people-search": "people-search.md", "background-checks": "background-checks.md", "marketing": "marketing.md",
    "credit-financial": "credit-financial.md", "public-records": "public-records.md", "other": "other.md",
}
REASON_TEXT = {
    "seized": "Domain transferred by court order (seizure banner on every check).",
    "parked": "Domain shows a registrar parking or for-sale page on every check.",
    "dns_dead": "Domain has no DNS record on every check.",
    "gone": "Opt-out page and homepage both return 404/410 on every check.",
    "unreachable": "Connection failed for the opt-out page and homepage on every check.",
}


def scrub(node, dead: set[str]):
    """Remove dead ids from any JSON structure: list items, dict keys, member/entry dicts."""
    if isinstance(node, list):
        out = []
        for item in node:
            if isinstance(item, str) and item in dead:
                continue
            if isinstance(item, dict) and (item.get("id") in dead or item.get("entry_id") in dead):
                continue
            out.append(scrub(item, dead))
        return out
    if isinstance(node, dict):
        return {k: scrub(v, dead) for k, v in node.items() if k not in dead}
    return node


def write_json(path, data, indent=2):
    path.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")


def render_md(rows: list[dict]) -> None:
    for cat, fname in CATEGORY_FILES.items():
        path = ROOT / "brokers" / fname
        text = path.read_text(encoding="utf-8")
        head, _, _ = text.partition("| Broker | Opt-out / privacy URL | Notes |")
        sub = sorted((r for r in rows if r["category"] == cat), key=lambda r: r["name"].lower())
        head = re.sub(r"^\d+ entries\.", f"{len(sub)} entries.", head, flags=re.M)
        lines = ["| Broker | Opt-out / privacy URL | Notes |", "| --- | --- | --- |"]
        for r in sub:
            esc = lambda s: s.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {esc(r['name'])} | {r['url']} | {esc(r['notes'])} |")
        path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="comma-separated subset of archive-worthy ids")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    report = json.loads(REPORT.read_text())["entries"]
    plan = {rid: e for rid, e in report.items() if e["verdict"] in ARCHIVE_VERDICTS}
    if args.ids:
        want = set(args.ids.split(","))
        plan = {k: v for k, v in plan.items() if k in want}
    print(json.dumps({"archive": len(plan), "by_reason": Counter(e["verdict"] for e in plan.values())}, indent=1))
    if args.dry_run or not plan:
        for k, v in sorted(plan.items()):
            print(f"  {v['verdict']:12} {k}")
        return 0
    dead = set(plan)
    rows = assign_ids(load_catalog())
    keep = [r for r in rows if r["id"] not in dead]
    gone = [r for r in rows if r["id"] in dead]
    today = date.today().isoformat()

    # archive file
    archive = json.loads(ARCHIVE.read_text()) if ARCHIVE.exists() else {
        "description": "Catalog rows removed because the broker is gone. Kept for history; never re-add without a fresh check.",
        "entries": []}
    for r in gone:
        e = plan[r["id"]]
        archive["entries"].append({
            "id": r["id"], "name": r["name"], "domain": r["domain"], "url": r["url"], "category": r["category"],
            "reason": e["verdict"], "reason_text": REASON_TEXT[e["verdict"]], "merged_into": None, "archived_at": today,
            "evidence": {"passes": e["passes"], "detail": e["detail"]},
            "catalog_row": {k: r[k] for k in CATALOG_COLUMNS},
        })
    archive["entries"].sort(key=lambda a: (a["archived_at"], a["id"]))
    archive["count"] = len(archive["entries"])
    write_json(ARCHIVE, archive)

    # catalog CSV (keep original order and quoting style)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CATALOG_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in keep:
        w.writerow({k: r[k] for k in CATALOG_COLUMNS})
    CATALOG_PATH.write_text(buf.getvalue(), encoding="utf-8")
    cat_py = ROOT / "scripts" / "catalog.py"
    cat_py.write_text(re.sub(r"EXPECTED_CATALOG_ROWS = \d+", f"EXPECTED_CATALOG_ROWS = {len(keep)}", cat_py.read_text()))

    # pinned ids
    ids_path = V1 / "catalog-ids.csv"
    lines = ids_path.read_text(encoding="utf-8").splitlines()
    ids_path.write_text("\n".join([lines[0]] + [l for l in lines[1:] if l.split(",", 1)[0] not in dead]) + "\n", encoding="utf-8")

    # v1 json files
    ds_path = V1 / "broker-opt-outs.enriched.json"
    ds = json.loads(ds_path.read_text())
    ds["records"] = [r for r in ds["records"] if r["id"] not in dead]
    ds["catalog_row_count"] = len(ds["records"])
    write_json(ds_path, ds)
    for name in ("shared-portals.json", "parent-companies.json", "workflow-groups.json", "automation-shortlist.json",
                 "verification-playbooks.json", "canadian-coverage.json", "freshness-report.json"):
        p = V1 / name
        if p.exists():
            data = scrub(json.loads(p.read_text()), dead)
            if name == "shared-portals.json":
                for key in ("shared_request_urls", "workflow_families"):
                    data[key] = [g for g in data.get(key, []) if len(g.get("member_ids", [])) >= 2]
            if name == "freshness-report.json":
                data["flagged"] = [f for f in data.get("flagged", []) if f.get("entry_ids")]
                data["catalog_rows"] = len(keep)
            write_json(p, data)
    for name in ("field-fills.json", "registry-matches.json"):
        p = V1 / name
        if p.exists():
            data = json.loads(p.read_text())
            key = "entries" if "entries" in data else "matches"
            data[key] = {k: v for k, v in data[key].items() if k not in dead}
            if key == "matches":
                data["matched_catalog_rows"] = len(data[key])
                data["review"] = [x for x in data.get("review", []) if x.get("candidate_id") not in dead]
            write_json(p, data, indent=1)
    # research-progress counts
    rp = V1 / "research-progress.json"
    prog = json.loads(rp.read_text())
    recs = ds["records"]
    prog["catalog_entries"] = len(recs)
    prog["unique_request_urls"] = len({r["catalog"]["url"] for r in recs})
    prog["research_status"] = dict(sorted(Counter(r["verification"]["research_status"] for r in recs).items()))
    prog["verification_level"] = dict(sorted(Counter(r["verification"]["verification_level"] for r in recs).items()))
    prog["automation_classification"] = dict(sorted(Counter(r["automation"]["classification"] for r in recs).items()))
    prog["entry_type"] = dict(sorted(Counter(r["identity"]["entry_type"] for r in recs).items()))
    prog["workflow_kind"] = dict(sorted(Counter(r["verification"].get("workflow_kind") for r in recs).items()))
    write_json(rp, prog)

    # v2
    for rid in dead:
        p = V2 / "brokers" / f"{rid}.yaml"
        if p.exists():
            p.unlink()
    for name in ("priority-impact.csv", "priority.csv"):
        p = V2 / name
        if p.exists():
            ls = p.read_text(encoding="utf-8").splitlines()
            idx = 1 if name == "priority.csv" else 0
            p.write_text("\n".join([ls[0]] + [l for l in ls[1:] if l.split(",")[idx] not in dead]) + "\n", encoding="utf-8")

    # docs
    render_md(keep)
    n_old, n_new = len(rows), len(keep)
    counts = Counter(r["category"] for r in keep)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme = readme.replace(f"A catalog of **{n_old}**", f"A catalog of **{n_new}**")
    readme = readme.replace(f"| **Total** | [opt-outs.csv](opt-outs.csv) | **{n_old}** |", f"| **Total** | [opt-outs.csv](opt-outs.csv) | **{n_new}** |")
    for cat, fname in CATEGORY_FILES.items():
        readme = re.sub(rf"(\[brokers/{re.escape(fname)}\]\(brokers/{re.escape(fname)}\) \| )\d+ \|", rf"\g<1>{counts[cat]} |", readme)
    (ROOT / "README.md").write_text(readme, encoding="utf-8")
    src = ROOT / "SOURCES.md"
    src.write_text(src.read_text(encoding="utf-8").replace(f"It has **{n_old}** data rows.", f"It has **{n_new}** data rows. {len(archive['entries'])} dead rows were moved to `data/archived.json` (see `docs/liveness.md`)."))
    print(json.dumps({"before": n_old, "after": n_new, "archived_total": archive["count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
