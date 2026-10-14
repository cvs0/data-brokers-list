#!/usr/bin/env python3
"""Validate the v2 automation layer: data/v2/brokers/*.yaml and docs/examples/*.yaml.

Checks each file against data/schema/broker-automation.schema.json, then the
cross-file rules that JSON Schema cannot express. Exit code 1 on any error.
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
import json
import re
import sys
from datetime import date, timedelta

from v2_common import BROKERS_DIR, EXAMPLES_DIR, SCHEMA_PATH, catalog_ids, load_yaml, spec_paths

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
SAFE_DECISIONS = {"email_or_letter_only", "user_assisted_only"}
E2E_MAX_AGE_DAYS = 90


def check_rules(spec: dict, ids: set[str], is_example: bool, today: date) -> list[str]:
    errors: list[str] = []
    sid = spec.get("id", "?")
    # (a) id joins the v1 catalog
    if sid not in ids:
        errors.append(f"{sid}: id is not in data/v1/catalog-ids.csv")
    # (b) covered brands exist and are evidenced
    covers = spec.get("covers_ids") or []
    for other in covers:
        if other not in ids:
            errors.append(f"{sid}: covers_ids entry {other} is not in the catalog")
        if other == sid:
            errors.append(f"{sid}: covers_ids must not include the spec's own id")
    if covers and not spec.get("coverage_evidence_url"):
        errors.append(f"{sid}: covers_ids needs coverage_evidence_url")
    family = spec.get("family_id")
    if family and family not in ids:
        errors.append(f"{sid}: family_id {family} is not in the catalog")
    # (c) ToS prohibition forces a non-automated decision
    tos = spec.get("tos", {})
    if tos.get("automation_prohibited") is True and tos.get("policy_decision") not in SAFE_DECISIONS:
        errors.append(f"{sid}: tos.automation_prohibited is true, so policy_decision must be one of {sorted(SAFE_DECISIONS)}")
    # (d) tested/live needs a recent passing end-to-end test
    status = spec.get("lifecycle", {}).get("status")
    adapter = spec.get("adapter") or {}
    if status in {"e2e_tested", "live"}:
        if adapter.get("last_e2e_result") != "pass" or not adapter.get("last_e2e_at"):
            errors.append(f"{sid}: status {status} needs adapter.last_e2e_result=pass and last_e2e_at")
        else:
            when = date.fromisoformat(str(adapter["last_e2e_at"]))
            if today - when > timedelta(days=E2E_MAX_AGE_DAYS):
                errors.append(f"{sid}: last end-to-end pass is older than {E2E_MAX_AGE_DAYS} days")
    # (e) inferred values block promotion past adapter_dev
    inferred = [k for k, v in (spec.get("provenance") or {}).items() if v.get("source") == "inferred"]
    if inferred and status in {"e2e_tested", "live"}:
        errors.append(f"{sid}: status {status} is not allowed while provenance has inferred values: {inferred}")
    # (f) url_template placeholders must be declared inputs
    search = spec.get("search", {})
    template = search.get("url_template") or ""
    missing = set(PLACEHOLDER.findall(template)) - set(search.get("inputs") or [])
    if missing:
        errors.append(f"{sid}: url_template uses {sorted(missing)} which are not in search.inputs")
    # (g) channel ranks are unique
    ranks = [c.get("rank") for c in spec.get("removal", {}).get("channels", [])]
    if len(ranks) != len(set(ranks)):
        errors.append(f"{sid}: removal.channels ranks must be unique")
    # provenance pointers must resolve
    for pointer in (spec.get("provenance") or {}):
        if not _resolves(spec, pointer):
            errors.append(f"{sid}: provenance key {pointer} does not point at a field in this file")
    return errors


def _resolves(doc, pointer: str) -> bool:
    node = doc
    for part in pointer.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                return False
            node = node[int(part)]
        elif isinstance(node, dict):
            if part not in node:
                return False
            node = node[part]
        else:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", help="Override today's date (YYYY-MM-DD) for the end-to-end age check.")
    args = parser.parse_args()
    today = date.fromisoformat(args.today) if args.today else date.today()

    try:
        import jsonschema
    except ImportError:
        print("jsonschema and pyyaml are required: pip install -r requirements.txt", file=sys.stderr)
        return 1
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    ids = catalog_ids()

    paths = [(p, False) for p in spec_paths()] + [(p, True) for p in sorted(EXAMPLES_DIR.glob("*.yaml"))]
    errors: list[str] = []
    seen: dict[str, str] = {}
    for path, is_example in paths:
        spec = load_yaml(path)
        rel = path.relative_to(path.parents[2]) if not is_example else path.name
        for err in validator.iter_errors(spec):
            loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
            errors.append(f"{rel}: {loc}: {err.message}")
        if not isinstance(spec, dict):
            continue
        if not is_example:
            if path.stem != spec.get("id"):
                errors.append(f"{rel}: file name must equal id ({spec.get('id')})")
            if spec.get("id") in seen:
                errors.append(f"{rel}: duplicate id also in {seen[spec['id']]}")
            seen[spec.get("id")] = str(rel)
        errors.extend(check_rules(spec, ids, is_example, today))

    # a brand may be covered by at most one spec, and must not also have its own spec
    covered: dict[str, str] = {}
    for path in spec_paths():
        spec = load_yaml(path)
        for other in spec.get("covers_ids") or []:
            if other in covered:
                errors.append(f"{other}: covered by both {covered[other]} and {spec['id']}")
            covered[other] = spec["id"]
            if other in seen:
                errors.append(f"{other}: has its own spec but is also covered by {spec['id']}")

    print(json.dumps({"specs": len(seen), "examples": sum(1 for _, e in paths if e), "errors": len(errors)}, indent=2))
    if errors:
        print("\n".join(errors[:100]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
