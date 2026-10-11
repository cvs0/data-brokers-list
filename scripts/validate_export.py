#!/usr/bin/env python3
"""Validate data/v2/frame-export.json against its schema and against the catalog.

Checks: JSON Schema; one broker per opt-outs.csv row (same ids, same count);
ids and domains unique; no archived id is still active; archived ids are gone
from catalog-ids.csv; every template referenced exists; covered_by points at an
active broker; meta.counts match the arrays.
"""

from __future__ import annotations

import json
import sys

from catalog import ROOT, assign_ids, load_catalog

EXPORT = ROOT / "data" / "v2" / "frame-export.json"
SCHEMA = ROOT / "data" / "schema" / "frame-export.schema.json"


def main() -> int:
    import jsonschema
    doc = json.loads(EXPORT.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in jsonschema.Draft202012Validator(schema).iter_errors(doc)]
    rows = assign_ids(load_catalog())
    ids = [r["id"] for r in rows]
    b_ids = [b["id"] for b in doc["brokers"]]
    if sorted(ids) != sorted(b_ids):
        errors.append(f"export ids differ from catalog: missing {sorted(set(ids) - set(b_ids))[:5]} extra {sorted(set(b_ids) - set(ids))[:5]}")
    if len(set(b_ids)) != len(b_ids):
        errors.append("duplicate broker ids")
    doms = [b["domain"] for b in doc["brokers"]]
    if len(set(doms)) != len(doms):
        errors.append("duplicate broker domains (apps key on domain)")
    arch = {a["id"] for a in doc["archived"]}
    if arch & set(b_ids):
        errors.append(f"archived ids still active: {sorted(arch & set(b_ids))}")
    pinned = (ROOT / "data" / "v1" / "catalog-ids.csv").read_text(encoding="utf-8")
    for a in arch:
        if f"\n{a}," in pinned:
            errors.append(f"archived id {a} still in catalog-ids.csv")
    for res, tids in doc["template_by_residency"].items():
        for t in tids:
            if t not in doc["templates"]:
                errors.append(f"template_by_residency[{res}] -> missing template {t}")
    active = set(b_ids)
    for b in doc["brokers"]:
        cb = b["family"]["covered_by"]
        if cb and cb not in active:
            errors.append(f"{b['id']}: covered_by {cb} is not active")
    c = doc["meta"]["counts"]
    if c["active"] != len(b_ids) or c["archived"] != len(doc["archived"]):
        errors.append("meta.counts do not match arrays")
    print(json.dumps({"brokers": len(b_ids), "archived": len(arch), "errors": len(errors)}, indent=2))
    if errors:
        print("\n".join(errors[:60]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
