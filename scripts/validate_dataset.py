#!/usr/bin/env python3
"""Validate the enriched dataset against the catalog, schema, and evidence rules."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from catalog import ROOT, assign_ids, load_catalog

DATASET = ROOT / "data" / "v1" / "broker-opt-outs.enriched.json"
SCHEMA = ROOT / "data" / "schema" / "broker-opt-out-record.schema.json"
PORTALS = ROOT / "data" / "v1" / "shared-portals.json"
PARENTS = ROOT / "data" / "v1" / "parent-companies.json"
IDS = ROOT / "data" / "v1" / "catalog-ids.csv"
SHORTLIST = ROOT / "data" / "v1" / "automation-shortlist.json"

EXCLUSIVE_ARRAYS = {
    ("identity", "geographic_coverage"),
    ("identity", "documented_request_eligibility"),
    ("identity", "supported_request_types"),
    ("discovery", "supported_search_inputs"),
    ("request_workflow", "submission_methods"),
}


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_exclusive(errors: list[str], record_id: str, values: list) -> None:
    if not isinstance(values, list) or not values:
        fail(errors, f"{record_id}: expected a non-empty array, got {values!r}")
        return
    if "unknown" in values and len(values) > 1:
        fail(errors, f"{record_id}: unknown mixed with other values in {values}")


def main() -> int:
    errors: list[str] = []
    rows = assign_ids(load_catalog())
    dataset = load_json(DATASET)
    schema = load_json(SCHEMA)
    portals = load_json(PORTALS)
    parents = load_json(PARENTS)

    try:
        import jsonschema
    except ImportError:
        jsonschema = None
        fail(errors, "jsonschema is not installed; structural schema validation was skipped.")
    if jsonschema is not None:
        validator = jsonschema.Draft202012Validator(schema)
        for issue in validator.iter_errors(dataset):
            path = ".".join(str(part) for part in issue.absolute_path) or "<root>"
            fail(errors, f"schema {path}: {issue.message}")
            if len(errors) > 40:
                break

    records = dataset.get("records") or []
    if dataset.get("requests_submitted") is not False:
        fail(errors, "requests_submitted must be false.")
    if dataset.get("schema_version") != "1.0.0":
        fail(errors, "schema_version mismatch.")
    if len(records) != len(rows):
        fail(errors, f"record count {len(records)} != catalog {len(rows)}")

    by_id = {}
    for record in records:
        entry_id = record.get("id")
        if entry_id in by_id:
            fail(errors, f"duplicate id {entry_id}")
        by_id[entry_id] = record

    expected_ids = [row["id"] for row in rows]
    if [record["id"] for record in records] != expected_ids:
        fail(errors, "record order or IDs do not match the catalog derivation.")

    for row in rows:
        record = by_id.get(row["id"])
        if not record:
            fail(errors, f"missing record for {row['id']}")
            continue
        catalog = record["catalog"]
        for column in ("name", "category", "url", "notes", "domain", "http_status"):
            if catalog.get(column) != row[column]:
                fail(errors, f"{row['id']}: catalog.{column} does not match opt-outs.csv")
                break
        if catalog.get("sources") != row["sources_list"]:
            fail(errors, f"{row['id']}: sources were not preserved")
        if record["verification"]["verification_level"] == "end_to_end_tested":
            fail(errors, f"{row['id']}: end_to_end_tested is not allowed without an authorized submission.")
        if record["request_workflow"]["observed_turnaround"] is not None:
            fail(errors, f"{row['id']}: observed turnaround must stay null when no request was submitted.")
        if record["automation"]["direct_access"]["proxy_required"] is True:
            fail(errors, f"{row['id']}: proxy_required true was not demonstrated in this research pass.")
        for path in EXCLUSIVE_ARRAYS:
            value = record
            for part in path:
                value = value[part]
            check_exclusive(errors, f"{row['id']} {'.'.join(path)}", value)
        steps = record["request_workflow"]["steps"]
        orders = [step["order"] for step in steps]
        if orders != list(range(1, len(orders) + 1)):
            fail(errors, f"{row['id']}: workflow steps are not ordered from 1")
        if record["identity"]["single_request_covers_multiple_brands"] is True and not record["identity"][
            "single_request_coverage_evidence"
        ]:
            fail(errors, f"{row['id']}: multi-brand coverage is true without evidence")
        status = record["verification"]["research_status"]
        if status == "verified" and not record["verification"]["evidence_urls"]:
            fail(errors, f"{row['id']}: verified record has no evidence URL")
        if status == "unresearched" and record["verification"]["verification_level"] != "source_documented":
            fail(errors, f"{row['id']}: unresearched record should stay source_documented")
        for sister in record["identity"]["sister_brands"]:
            if sister["entry_id"] not in by_id:
                fail(errors, f"{row['id']}: sister {sister['entry_id']} does not exist")
            if sister["evidence_basis"] == "official_page" and sister["relationship"] == "catalog_name_hint":
                fail(errors, f"{row['id']}: catalog name hint marked as an official page")

    group_ids = set()
    for group in portals.get("shared_request_urls") or []:
        group_ids.add(group["group_id"])
        if len(group["member_ids"]) < 2:
            fail(errors, f"{group['group_id']}: shared URL group needs at least two members")
        urls = set()
        for member_id in group["member_ids"]:
            if member_id not in by_id:
                fail(errors, f"{group['group_id']}: missing member {member_id}")
                continue
            member = by_id[member_id]
            urls.add(member["catalog"]["url"])
            if member["identity"]["shared_portal_group_id"] != group["group_id"]:
                fail(errors, f"{member_id}: shared_portal_group_id does not match {group['group_id']}")
        if len(urls) != 1:
            fail(errors, f"{group['group_id']}: members do not share one URL")
    for group in portals.get("workflow_families") or []:
        group_ids.add(group["group_id"])
        for member_id in group["member_ids"]:
            if member_id not in by_id:
                fail(errors, f"{group['group_id']}: missing family member {member_id}")
                continue
            if by_id[member_id]["identity"]["workflow_family_id"] != group["group_id"]:
                fail(errors, f"{member_id}: workflow_family_id mismatch")
    for record in records:
        group_id = record["identity"]["shared_portal_group_id"]
        if group_id and group_id not in group_ids:
            fail(errors, f"{record['id']}: unknown shared_portal_group_id {group_id}")
        family_id = record["identity"]["workflow_family_id"]
        if family_id and family_id not in group_ids:
            fail(errors, f"{record['id']}: unknown workflow_family_id {family_id}")

    for parent in parents.get("verified") or []:
        for member_id in parent["member_ids"]:
            if member_id not in by_id:
                fail(errors, f"parent mapping missing {member_id}")
            elif by_id[member_id]["identity"]["parent_company"] != parent["parent_company"]:
                fail(errors, f"{member_id}: parent company mapping mismatch")
    for lead in parents.get("catalog_leads_unverified") or []:
        if lead.get("evidence_basis") != "catalog_name":
            fail(errors, f"catalog lead {lead.get('hint')} is not labeled as a catalog name lead")

    if IDS.exists():
        id_text = IDS.read_text(encoding="utf-8").splitlines()
        if not id_text or not id_text[0].startswith("id,"):
            fail(errors, "catalog-ids.csv is missing a header")
        elif len(id_text) != len(rows) + 1:
            fail(errors, "catalog-ids.csv row count does not match the catalog")

    if SHORTLIST.exists():
        shortlist = load_json(SHORTLIST)
        entries = shortlist.get("candidates") or []
        if not 20 <= len(entries) <= 30:
            fail(errors, f"shortlist has {len(entries)} candidates; expected 20 to 30")
        seen = set()
        for candidate in entries:
            entry_id = candidate.get("entry_id")
            if entry_id in seen:
                fail(errors, f"shortlist duplicate {entry_id}")
            seen.add(entry_id)
            if entry_id not in by_id:
                fail(errors, f"shortlist unknown id {entry_id}")
                continue
            if candidate.get("verification_level") == "end_to_end_tested":
                fail(errors, f"shortlist {entry_id} claims an end-to-end test")
            for key in ("rank", "why", "blockers", "implementation_effort", "automation_classification"):
                if key not in candidate:
                    fail(errors, f"shortlist {entry_id} missing {key}")

    status_counts = Counter(record["verification"]["research_status"] for record in records)
    print(json.dumps({"records": len(records), "research_status": dict(status_counts), "errors": len(errors)}, indent=2))
    if errors:
        print("\n".join(errors[:50]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
