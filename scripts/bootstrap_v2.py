#!/usr/bin/env python3
"""Create starter v2 specs (data/v2/brokers/<id>.yaml) from v1 evidence.

Only facts already in v1 are copied, each with a provenance entry. Everything
v1 does not establish stays null. Generated specs start as `draft` (v1 blocked)
or `researching`. Existing files are left alone unless --force is passed, so
hand edits are never overwritten.

  python scripts/bootstrap_v2.py                 # every id in data/v2/priority.csv
  python scripts/bootstrap_v2.py --ids spokeo    # selected ids
"""

from __future__ import annotations

import argparse
import csv
from datetime import date

from v2_common import BROKERS_DIR, V2_DIR, canonical_field, dump_yaml, form_name, processing_days, v1_records

PRIORITY = V2_DIR / "priority.csv"
HEADER = ("# Bootstrapped from data/v1 by scripts/bootstrap_v2.py on {today}.\n"
          "# Review before promoting lifecycle.status. null means not established, never false.")
SEARCH_INPUTS = {"name": ["full_name"], "location": ["city", "region"], "phone": ["phone"], "email": ["email"],
                 "profile_url": ["profile_url"]}
CAPTCHA = {"always_required": "always", "conditionally_observed": "conditional"}


def _date(ts: str | None) -> str:
    return (ts or str(date.today()))[:10]


def _id_doc(value):
    return {True: "always", False: "never"}.get(value)


def build_spec(rec: dict, prio: dict, today: str, peers: dict) -> dict:
    ident, disc, rw, auto, ver = rec["identity"], rec["discovery"], rec["request_workflow"], rec["automation"], rec["verification"]
    checked = _date(ver.get("researched_at"))
    evidence = (ver.get("evidence_urls") or [None])[0] or disc.get("request_url") or rec["catalog"]["url"]
    prov: dict[str, dict] = {}

    def cite(pointer: str, source: str = "page_text", note: str | None = None, url: str | None = None):
        entry = {"checked_at": checked, "source": source, "evidence_url": url or evidence}
        if note:
            entry["note"] = note
        prov[pointer] = entry

    methods = [m for m in rw["submission_methods"] if m != "unknown"]
    request_url = disc.get("request_url") or rec["catalog"]["url"]
    email = disc.get("privacy_contact_email")
    blockers = auto["observed_blockers"]
    coverage = set(ident["geographic_coverage"]) | set(ident["documented_request_eligibility"])
    ccpa = "US-CA" in coverage

    requires = {
        "captcha": CAPTCHA.get(rw["captcha_requirement"]),
        "captcha_provider": None,
        "email_verification": (rw["email_verification_mechanism"] if rw["email_verification_mechanism"] in {"link", "code"}
                               else ("none" if rw["email_verification_required"] is False else None)),
        "login": rw["account_login_required"],
        "profile_url": rw["profile_url_or_record_id_required"],
        "phone_verification": rw["phone_sms_verification_required"],
        "id_document": _id_doc(rw["identity_document_required"]),
    }

    fields, seen = [], set()
    for raw in rw["required_fields"]:
        canon = canonical_field(raw)
        if canon and canon not in seen:
            seen.add(canon)
            fields.append({"canonical": canon, "form_name": form_name(raw), "selector": None, "required": True, "transform": None})

    channels = []
    rank = 1
    if rec["id"] == "california-drop":
        channels.append({"channel": "drop", "rank": rank, "url": request_url, "requires": requires, "steps": [
            {"order": 1, "type": "goto", "target": request_url},
            {"order": 2, "type": "human_task", "human_task_kind": "login", "target": request_url},
        ]})
        cite("/removal/channels/0", note="Consumer files DROP personally (California Identity Gateway sign-in).")
        rank += 1
    elif "web_form" in methods or not methods:
        steps = [{"order": 1, "type": "goto", "target": request_url}]
        for f in fields:
            steps.append({"order": len(steps) + 1, "type": "fill", "target": None, "value_from": f["canonical"]})
        steps.append({"order": len(steps) + 1, "type": "screenshot"})
        if requires["captcha"] in {"always", "conditional"}:
            steps.append({"order": len(steps) + 1, "type": "human_task", "human_task_kind": "captcha"})
        if requires["login"]:
            steps.insert(1, {"order": 0, "type": "human_task", "human_task_kind": "login"})
        steps.append({"order": len(steps) + 1, "type": "submit", "target": None})
        steps.append({"order": len(steps) + 1, "type": "screenshot"})
        if requires["email_verification"] == "link":
            steps += [{"order": 0, "type": "await_email", "timeout_s": 86400}, {"order": 0, "type": "open_email_link"},
                      {"order": 0, "type": "screenshot"}]
        for i, step in enumerate(steps, 1):
            step["order"] = i
        channels.append({"channel": "web_form", "rank": rank, "url": request_url, "requires": requires,
                         "fields": fields, "steps": steps})
        if methods:
            cite(f"/removal/channels/{len(channels) - 1}/requires")
        else:
            cite(f"/removal/channels/{len(channels) - 1}", source="inferred",
                 note="v1 submission method is unknown; the published request URL is used as a starting point.")
        if fields:
            cite(f"/removal/channels/{len(channels) - 1}/fields", note="Mapped from v1 required_fields by scripts/v2_common.py")
        rank += 1
    if email:
        template = "ccpa_delete" if ccpa else "voluntary_optout"
        channels.append({"channel": "email", "rank": rank, "email": email, "template_id": template,
                         "requires": {"captcha": "none", "captcha_provider": None, "email_verification": None, "login": False,
                                      "profile_url": rw["profile_url_or_record_id_required"], "phone_verification": None,
                                      "id_document": _id_doc(rw["identity_document_required"])},
                         "steps": [{"order": 1, "type": "send_email", "target": template}]})
        cite(f"/removal/channels/{len(channels) - 1}/email")
        rank += 1
    if "postal_mail" in methods:
        channels.append({"channel": "postal_mail", "rank": rank, "postal_address": None,
                         "template_id": "ccpa_delete" if ccpa else "voluntary_optout",
                         "requires": {"captcha": "none", "captcha_provider": None, "email_verification": "none", "login": False,
                                      "profile_url": None, "phone_verification": False,
                                      "id_document": _id_doc(rw["identity_document_required"])},
                         "steps": [{"order": 1, "type": "send_letter", "target": "ccpa_delete" if ccpa else "voluntary_optout"}]})
        cite(f"/removal/channels/{len(channels) - 1}/channel", note="Postal address still to be copied from the broker's page.")
        rank += 1
    if "phone" in methods:
        channels.append({"channel": "phone", "rank": rank, "requires": {
            "captcha": "none", "captcha_provider": None, "email_verification": None, "login": None,
            "profile_url": None, "phone_verification": None, "id_document": None},
            "steps": [{"order": 1, "type": "human_task", "human_task_kind": "phone_call"}]})
        cite(f"/removal/channels/{len(channels) - 1}/channel")

    # search
    inputs: list[str] = []
    for raw in disc["supported_search_inputs"]:
        for canon in SEARCH_INPUTS.get(raw, []):
            if canon not in inputs:
                inputs.append(canon)
    if disc["exposure_check"] == "broker_confirmation_only":
        method = "broker_confirmation_only"
    elif disc["public_profiles_searchable"] or disc["exposure_check"] in {"public", "both"}:
        method = "browser" if ("bot_wall" in blockers or "captcha" in blockers) else "http_get"
    else:
        method = "none"
    search = {"method": method, "url_template": None, "inputs": inputs, "input_transforms": {}, "requires_js": None,
              "results": {"strategy": None, "item_selector": None, "fields": {}, "listing_url_selector": None,
                          "listing_id_regex": None, "no_results_marker": None, "next_page_selector": None, "max_pages": 1},
              "match_fields_available": []}
    search["results"].pop("strategy")
    if method != "none":
        cite("/search/method", source="inferred", note="From v1 public_profiles_searchable / exposure_check; template not yet discovered.")

    legal = []
    if ccpa:
        legal.append({"basis": "ccpa_delete", "eligible_residency": ["US-CA"], "statutory_response_days": 45,
                      "source_url": disc.get("privacy_policy_url") or request_url})
        legal.append({"basis": "ccpa_opt_out", "eligible_residency": ["US-CA"], "statutory_response_days": None,
                      "source_url": disc.get("privacy_policy_url") or request_url})
    if "CA" in coverage:
        legal.append({"basis": "pipeda", "eligible_residency": ["CA"], "statutory_response_days": 30,
                      "source_url": disc.get("privacy_policy_url") or request_url})
    if "CA-QC" in coverage:
        legal.append({"basis": "quebec_law25", "eligible_residency": ["CA-QC"], "statutory_response_days": 30,
                      "source_url": disc.get("privacy_policy_url") or request_url})
    if rec["id"] != "california-drop":
        legal.append({"basis": "voluntary", "eligible_residency": ["ANY"], "statutory_response_days": None, "source_url": request_url})
    if legal:
        cite("/jurisdiction/legal_bases", note="Derived from v1 geographic_coverage and documented_request_eligibility.")

    days = processing_days(rw["documented_processing_time"])
    if days is not None:
        cite("/timing/processing_days_documented", note=rw["documented_processing_time"][:300])

    covers = []
    if ident["single_request_covers_multiple_brands"]:
        covers = [s["entry_id"] for s in ident["sister_brands"]
                  if s["relationship"] == "same_request_url" and s["entry_id"] in peers.get(rec["id"], set())]
    agent_text = rw["authorized_agent_requirements"]
    wall = "challenge_page" if "bot_wall" in blockers else ("hard_block" if "inaccessible_page" in blockers else None)
    if wall:
        cite("/egress/bot_wall_observed", source="observed_browser")

    scope = None
    if "public_listing_suppression" in ident["supported_request_types"] and rw["profile_url_or_record_id_required"]:
        scope = "listing"
        cite("/removal/removes_scope", note="v1: listing suppression with a profile URL or record id required.")

    spec = {
        "schema_version": "2.0.0",
        "id": rec["id"],
        "family_id": None,
        "covers_ids": covers,
        "coverage_evidence_url": request_url if covers else None,
        "lifecycle": {"status": "draft" if ver["research_status"] == "blocked" else "researching",
                      "status_reason": f"Bootstrapped from v1 (research_status {ver['research_status']}, automation {auto['classification']}).",
                      "updated_at": today},
        "priority": {"score": int(prio["score"]), "tier": prio["tier"], "rationale": prio["rationale"]},
        "jurisdiction": {"operator_country": None, "registries": [], "drop_participant": None, "publishes_canadians": None,
                         "legal_bases": legal},
        "search": search,
        "removal": {"channels": channels, "removes_scope": scope or "unknown", "suppression_not_deletion": None},
        "authorized_agent": {"accepted": None, "requirements": [], "agent_channel": None,
                             "source_url": disc.get("privacy_policy_url") if agent_text else None,
                             "notes": agent_text[:500] if agent_text else None},
        "identity_verification": {"fields_requested": [f["canonical"] for f in fields],
                                  "id_document": _id_doc(rw["identity_document_required"]),
                                  "id_redaction_allowed": None,
                                  "phone_verification": rw["phone_sms_verification_required"]},
        "timing": {"processing_days_documented": days, "processing_days_observed_p50": None,
                   "processing_days_observed_p90": None, "observed_sample_size": 0,
                   "recheck_after_days": (days + 2) if days is not None else 14,
                   "relist_check_interval_days": 30, "relist_rate_observed": None},
        "rate_policy": {"min_interval_s": 4, "max_per_day": 2000, "concurrency": 1,
                        "backoff_on_status": [403, 429, 503], "backoff_hours": 6, "honor_retry_after": True},
        "tos": {"automation_prohibited": None, "clause_quote": None, "url": None, "checked_at": None, "policy_decision": None},
        "egress": {"geo_block_observed": [], "bot_wall_observed": wall, "proxy_policy": "never_residential"},
        "adapter": {"id": None, "module_path": None, "last_e2e_at": None, "last_e2e_result": None, "e2e_evidence_url": None},
        "provenance": prov,
    }
    if agent_text:
        cite("/authorized_agent/notes")
    if covers:
        cite("/covers_ids", source="official_doc", note=(ident["single_request_coverage_evidence"] or "")[:300])
    return spec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--force", action="store_true", help="Overwrite existing spec files.")
    args = parser.parse_args()
    today = str(date.today())
    records = v1_records()
    with PRIORITY.open(newline="", encoding="utf-8") as handle:
        prio = {row["id"]: row for row in csv.DictReader(handle)}
    ids = args.ids or list(prio)
    import json
    from v2_common import ROOT
    parents = json.loads((ROOT / "data" / "v1" / "parent-companies.json").read_text(encoding="utf-8"))
    peers: dict[str, set[str]] = {}
    for group in parents.get("verified", []):
        for member in group["member_ids"]:
            peers[member] = set(group["member_ids"]) - {member}
    BROKERS_DIR.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for bid in ids:
        if bid not in records:
            raise SystemExit(f"{bid} is not in the v1 dataset")
        path = BROKERS_DIR / f"{bid}.yaml"
        if path.exists() and not args.force:
            skipped += 1
            continue
        default = {"score": 0, "tier": "later", "rationale": None}
        spec = build_spec(records[bid], prio.get(bid, default), today, peers)
        dump_yaml(spec, path, HEADER.format(today=today))
        written += 1
    print({"written": written, "skipped_existing": skipped})


if __name__ == "__main__":
    main()
