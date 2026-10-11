#!/usr/bin/env python3
"""Compile data/v2/brokers/*.yaml into data/v2/brokers.json and data/v2/brokers.csv.

Run validate_v2.py first. Output is sorted by id so diffs stay small.
"""

from __future__ import annotations

import csv
import json

from v2_common import BROKERS_DIR, CSV_COLUMNS, V2_DIR, load_yaml, spec_paths


def _pipe(values) -> str:
    return "|".join(str(v) for v in values or [])


def _b(value) -> str:
    return "" if value is None else str(value).lower()


def flatten(spec: dict) -> dict:
    channels = sorted(spec["removal"]["channels"], key=lambda c: c["rank"])
    first = channels[0]
    second = channels[1] if len(channels) > 1 else {}
    email = next((c.get("email") for c in channels if c["channel"] == "email" and c.get("email")), "")
    req = first.get("requires", {})
    juris = spec["jurisdiction"]
    cppa = next((r.get("registration_id") or "" for r in juris.get("registries", []) if r["registry"] == "ca_cppa"), "")
    search = spec["search"]
    results = search.get("results") or {}
    timing, rate, tos = spec["timing"], spec["rate_policy"], spec["tos"]
    egress, adapter = spec.get("egress") or {}, spec.get("adapter") or {}
    agent = spec["authorized_agent"]
    checked = [v["checked_at"] for v in (spec.get("provenance") or {}).values() if v.get("checked_at")]
    return {
        "id": spec["id"],
        "family_id": spec.get("family_id") or "",
        "covers_ids": _pipe(spec.get("covers_ids")),
        "lifecycle_status": spec["lifecycle"]["status"],
        "priority_score": spec["priority"]["score"],
        "priority_tier": spec["priority"]["tier"],
        "operator_country": juris.get("operator_country") or "",
        "cppa_registration_id": cppa,
        "drop_participant": _b(juris.get("drop_participant")),
        "publishes_canadians": _b(juris.get("publishes_canadians")),
        "legal_bases": _pipe(b["basis"] for b in juris.get("legal_bases", [])),
        "search_method": search["method"],
        "search_url_template": search.get("url_template") or "",
        "search_inputs": _pipe(search.get("inputs")),
        "parse_strategy": results.get("strategy") or "",
        "listing_id_regex": results.get("listing_id_regex") or "",
        "match_fields_available": _pipe(search.get("match_fields_available")),
        "primary_channel": first["channel"],
        "primary_channel_url": first.get("url") or "",
        "fallback_channel": second.get("channel", ""),
        "privacy_email": email or "",
        "captcha": req.get("captcha") or "",
        "email_verification": req.get("email_verification") or "",
        "profile_url_required": _b(req.get("profile_url")),
        "login_required": _b(req.get("login")),
        "id_document": (spec.get("identity_verification") or {}).get("id_document") or "",
        "removal_fields": _pipe(f["canonical"] for f in first.get("fields", [])),
        "removes_scope": spec["removal"].get("removes_scope") or "",
        "suppression_not_deletion": _b(spec["removal"].get("suppression_not_deletion")),
        "authorized_agent_accepted": _b(agent.get("accepted")),
        "authorized_agent_requirements": _pipe(agent.get("requirements")),
        "processing_days_documented": timing.get("processing_days_documented") if timing.get("processing_days_documented") is not None else "",
        "processing_days_observed_p50": timing.get("processing_days_observed_p50") if timing.get("processing_days_observed_p50") is not None else "",
        "recheck_after_days": timing["recheck_after_days"],
        "relist_check_interval_days": timing["relist_check_interval_days"],
        "relist_rate_observed": timing.get("relist_rate_observed") if timing.get("relist_rate_observed") is not None else "",
        "min_interval_s": rate["min_interval_s"],
        "max_per_day": rate["max_per_day"],
        "tos_automation_prohibited": _b(tos.get("automation_prohibited")),
        "tos_policy_decision": tos.get("policy_decision") or "",
        "bot_wall_observed": egress.get("bot_wall_observed") or "",
        "geo_block_observed": _pipe(egress.get("geo_block_observed")),
        "adapter_id": adapter.get("id") or "",
        "last_e2e_at": adapter.get("last_e2e_at") or "",
        "last_e2e_result": adapter.get("last_e2e_result") or "",
        "last_verified_at": max(map(str, checked)) if checked else "",
    }


def main() -> None:
    specs = [load_yaml(p) for p in spec_paths()]
    specs.sort(key=lambda s: s["id"])
    V2_DIR.mkdir(parents=True, exist_ok=True)
    (V2_DIR / "brokers.json").write_text(
        json.dumps({"schema_version": "2.0.0", "count": len(specs), "brokers": specs}, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    with (V2_DIR / "brokers.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for spec in specs:
            writer.writerow(flatten(spec))
    print(json.dumps({"brokers": len(specs), "out": ["data/v2/brokers.json", "data/v2/brokers.csv"]}))


if __name__ == "__main__":
    main()
