#!/usr/bin/env python3
"""Build one importable artifact for apps that run exposure scans and removals.

Inputs (all in this repo):
  opt-outs.csv                         the catalog (source of truth for which brokers are active)
  data/v1/broker-opt-outs.enriched.json evidence layer
  data/v1/field-fills.json             per-field fills from public pages (scripts/rescrape_fields.py)
  data/v1/registry-matches.json        CPPA registry matches (scripts/fetch_registries.py)
  data/v2/brokers/*.yaml               machine specs (top brokers)
  data/v2/priority.csv                 priority scores
  research/evidence/liveness-report.json latest liveness verdicts
  data/archived.json                   removed entries, with reason
  templates/*.md                       request templates

Outputs:
  data/v2/frame-export.json    {meta, template_by_residency, templates, brokers[], archived[]}
  data/v2/frame-export.ndjson  one broker per line (same objects), for streaming imports
  data/v2/templates.json       the request templates as data

Precedence for every field: v2 spec > v1 evidence > CPPA registry > field fill.
Every field that came from somewhere other than v1 carries a provenance entry.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone

from catalog import ROOT, assign_ids, load_catalog
from v2_common import V2_DIR, load_yaml, processing_days, spec_paths

EXPORT_VERSION = "1.0.0"
OUT_JSON = V2_DIR / "frame-export.json"
OUT_NDJSON = V2_DIR / "frame-export.ndjson"
OUT_TEMPLATES = V2_DIR / "templates.json"
TEMPLATE_DIR = ROOT / "templates"
FLOW = {"web_form": "form", "official_portal": "portal", "email": "email", "postal_mail": "letter",
        "phone": "phone", "drop": "drop", "official_api": "api", "other": "manual"}
FLOW_ORDER = ["portal", "form", "email", "letter", "phone", "drop", "api"]
# Which template a person should use, by where they live. Apps pick the most specific match.
TEMPLATE_BY_RESIDENCY = {
    "US-CA": ["ccpa_delete", "ccpa_opt_out"],
    "US-VA": ["state_privacy_delete"], "US-CO": ["state_privacy_delete"], "US-CT": ["state_privacy_delete"],
    "US-UT": ["state_privacy_delete"], "US-TX": ["state_privacy_delete"], "US-OR": ["state_privacy_delete"],
    "US-MT": ["state_privacy_delete"], "US-IA": ["state_privacy_delete"], "US-DE": ["state_privacy_delete"],
    "US-NH": ["state_privacy_delete"], "US-NJ": ["state_privacy_delete"], "US-TN": ["state_privacy_delete"],
    "US-MN": ["state_privacy_delete"], "US-MD": ["state_privacy_delete"], "US-IN": ["state_privacy_delete"],
    "US-KY": ["state_privacy_delete"], "US-RI": ["state_privacy_delete"], "US-NE": ["state_privacy_delete"],
    "CA-QC": ["quebec_cessation", "quebec_cessation_en"],
    "CA": ["pipeda_withdraw"],
    "EU": ["eu_gdpr_erasure"],
    "GB": ["uk_gdpr_erasure"],
    "ANY": ["voluntary_optout"],
}
RECHECK_DEFAULT = 14
RELIST_DEFAULT = 90


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def parse_template(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise SystemExit(f"{path}: missing front matter")
    import yaml
    meta = yaml.safe_load(m.group(1))
    body = m.group(2).strip() + "\n"
    used = sorted(set(re.findall(r"\{\{(\w+)\}\}", body + meta.get("subject", ""))))
    if sorted(meta.get("placeholders", [])) != sorted(set(used) - set()) and set(used) - set(meta.get("placeholders", [])):
        raise SystemExit(f"{path}: body uses {sorted(set(used) - set(meta['placeholders']))} not declared in placeholders")
    return {**meta, "body": body}


def norm_url(v):
    """Registry entries are typed by hand: add a scheme, drop mailto: and junk."""
    v = (v or "").split(";")[0].split(",")[0].strip()
    if not v or v.lower().startswith("mailto:") or "@" in v.split("/")[0] or " " in v:
        return None
    if not re.match(r"^https?://", v, re.I):
        if not re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", v, re.I):
            return None
        v = "https://" + v
    return v


def nonempty(v):
    return v not in (None, "", [], {}, "unknown", ["unknown"])


def main() -> int:
    rows = assign_ids(load_catalog())
    v1 = {r["id"]: r for r in load_json(ROOT / "data" / "v1" / "broker-opt-outs.enriched.json", {"records": []})["records"]}
    fills = load_json(ROOT / "data" / "v1" / "field-fills.json", {"entries": {}})["entries"]
    registry = load_json(ROOT / "data" / "v1" / "registry-matches.json", {"matches": {}})["matches"]
    live = load_json(ROOT / "research" / "evidence" / "liveness-report.json", {"entries": {}})["entries"]
    archived = load_json(ROOT / "data" / "archived.json", {"entries": []})["entries"]
    specs = {}
    for p in spec_paths():
        s = load_yaml(p)
        specs[s["id"]] = s
    covered_by = {cid: s["id"] for s in specs.values() for cid in (s.get("covers_ids") or [])}
    prio = {}
    pp = V2_DIR / "priority.csv"
    if pp.exists():
        for r in csv.DictReader(pp.open(encoding="utf-8")):
            prio[r["id"]] = {"rank": int(r["rank"]), "score": int(r["score"]), "tier": r["tier"]}
    portals = load_json(ROOT / "data" / "v1" / "shared-portals.json", {"shared_request_urls": []})
    portal_of = {}
    for g in portals.get("shared_request_urls", []):
        for m in g["member_ids"]:
            portal_of[m] = g["group_id"]
    parents = load_json(ROOT / "data" / "v1" / "parent-companies.json", {"verified": []})
    parent_of = {}
    for g in parents.get("verified", []):
        for m in g["member_ids"]:
            parent_of[m] = g["parent_company"]

    templates = {}
    for path in sorted(TEMPLATE_DIR.glob("*.md")):
        if path.name == "README.md":
            continue
        t = parse_template(path)
        if t["id"] != path.stem:
            raise SystemExit(f"{path}: id must equal file name")
        templates[t["id"]] = t
    for ids in TEMPLATE_BY_RESIDENCY.values():
        for tid in ids:
            if tid not in templates:
                raise SystemExit(f"template {tid} referenced but missing")

    brokers = []
    for row in rows:
        rid = row["id"]
        rec = v1.get(rid, {})
        disc, wf, ident, auto, ver = (rec.get(k, {}) for k in ("discovery", "request_workflow", "identity", "automation", "verification"))
        f = fills.get(rid, {})
        rg = registry.get(rid)
        spec = specs.get(rid)
        prov = {}

        def fill(field):
            x = f.get(field)
            if x:
                prov[field] = {"source": "page_fill", "source_url": x["source_url"], "checked_at": x["checked_at"], "confidence": x["confidence"]}
                return x["value"]
            return None

        # contact email
        email, email_conf = disc.get("privacy_contact_email"), "v1" if disc.get("privacy_contact_email") else None
        rg_email = None
        if rg:
            for cand in [rg.get("contact_email") or "", (rg.get("privacy_rights_url") or "").removeprefix("mailto:")]:
                m = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)*\.[a-z]{2,}", cand.replace(" [at] ", "@"), re.I)
                if m:
                    rg_email = m.group(0).lower()
                    break
        if not email and rg_email:
            email, email_conf = rg_email, "registry"
            prov["privacy_email"] = {"source": "ca_cppa_registry", "source_url": rg["source_url"], "checked_at": rg["checked_at"], "confidence": "official"}
        if not email:
            email = fill("privacy_contact_email")
            if email:
                prov["privacy_email"] = prov.pop("privacy_contact_email")
                email_conf = prov["privacy_email"]["confidence"]
        # methods
        methods = [m for m in (wf.get("submission_methods") or []) if m != "unknown"] or (fill("submission_methods") or [])
        if spec:
            methods = sorted({c["channel"] for c in spec["removal"]["channels"]} | set(methods))
        if email and "email" not in methods:
            methods = sorted(set(methods) | {"email"})
        flows = sorted({FLOW.get(m, m) for m in methods}, key=lambda x: FLOW_ORDER.index(x) if x in FLOW_ORDER else 99)
        if spec and spec["removal"]["channels"]:
            primary = FLOW.get(sorted(spec["removal"]["channels"], key=lambda c: c["rank"])[0]["channel"])
        else:
            primary = flows[0] if flows else "manual"
        # required info
        req = None
        if spec:
            req = sorted({fl["canonical"] for c in spec["removal"]["channels"] for fl in (c.get("fields") or []) if fl.get("required")}) or None
        if not req:
            req = fill("required_info")
        # verification
        verification = []
        evm = wf.get("email_verification_mechanism")
        if evm in ("link", "code"):
            verification.append("email_" + evm)
        if wf.get("identity_document_required"):
            verification.append("id_document")
        if wf.get("phone_sms_verification_required"):
            verification.append("phone_sms")
        if wf.get("captcha_requirement") in ("always_required", "conditionally_observed") or wf.get("captcha_present"):
            verification.append("captcha")
        if wf.get("account_login_required"):
            verification.append("account_login")
        if wf.get("profile_url_or_record_id_required"):
            verification.append("profile_url")
        if not verification:
            verification = fill("verification") or []
        # processing time
        days = processing_days(wf.get("documented_processing_time"))
        days_src = "documented" if days else None
        if spec and spec["timing"].get("processing_days_documented"):
            days, days_src = spec["timing"]["processing_days_documented"], "documented"
        if not days:
            pt = fill("processing_time")
            if pt and pt.get("days_upper_bound"):
                days, days_src = pt["days_upper_bound"], "page_fill"
        if days and days > 365:
            days, days_src = None, None
        reported_median = rg.get("delete_days_median") if rg else None
        # jurisdiction
        coverage = [g for g in (ident.get("geographic_coverage") or []) if g != "unknown"]
        if not coverage:
            coverage = fill("jurisdiction") or []
        statutory = []
        if rg:
            statutory += ["ccpa_delete", "ccpa_opt_out"]
        if spec:
            statutory += [b["basis"] for b in spec["jurisdiction"].get("legal_bases", []) if b["basis"] != "voluntary"]
        if any(c in coverage for c in ("CA", "CA-QC", "CA-BC", "CA-AB")):
            statutory.append("pipeda")
            if "CA-QC" in coverage:
                statutory.append("quebec_law25")
        if "EU" in coverage:
            statutory.append("eu_gdpr")
        if "GB" in coverage:
            statutory.append("uk_gdpr")
        statutory = sorted(set(statutory))
        # search
        search = {"method": None, "url_template": None, "search_page": None, "inputs": [], "public_profiles": disc.get("public_profiles_searchable"),
                  "requires_login": disc.get("search_requires_login"), "requires_payment": disc.get("search_requires_payment"), "spec_ready": False}
        if spec:
            ss = spec["search"]
            search.update(method=ss.get("method"), url_template=ss.get("url_template"), inputs=ss.get("inputs") or [],
                          spec_ready=bool(ss.get("url_template") and (ss.get("results") or {}).get("item_selector")))
        if not search["inputs"] and disc.get("supported_search_inputs"):
            search["inputs"] = disc["supported_search_inputs"]
        sp = fill("search_page")
        if sp:
            search["search_page"] = sp["url"]
            search["inputs"] = search["inputs"] or sp["inputs"]
        blockers = auto.get("observed_blockers") or []
        lv = live.get(rid, {})
        request_url = norm_url(disc.get("request_url")) or norm_url(fill("request_url")) or row["url"]
        policy_url = norm_url(disc.get("privacy_policy_url")) or norm_url(fill("privacy_policy_url"))
        if rg and norm_url(rg.get("privacy_rights_url")) and not policy_url:
            policy_url = norm_url(rg["privacy_rights_url"])
            prov["privacy_policy_url"] = {"source": "ca_cppa_registry", "source_url": rg["source_url"], "checked_at": rg["checked_at"], "confidence": "official"}
        lifecycle = spec["lifecycle"]["status"] if spec else None
        broker = {
            "id": rid,
            "name": row["name"],
            "category": row["category"],
            "domain": row["domain"],
            "opt_out_url": row["url"],
            "request_url": request_url,
            "privacy_policy_url": policy_url,
            "notes": row["notes"] or None,
            "sources": [s for s in row["sources"].split(";") if s],
            "last_http_status": int(row["http_status"]) if row["http_status"].isdigit() else None,
            "liveness": {"verdict": lv.get("verdict"), "checked_at": (lv.get("passes") or [{}])[-1].get("checked_at")},
            "family": {
                "parent_company": parent_of.get(rid),
                "shared_portal_group_id": portal_of.get(rid),
                "covered_by": covered_by.get(rid),
                "covers": (spec or {}).get("covers_ids") or [],
            },
            "priority": prio.get(rid),
            "removal": {
                "primary_flow": primary,
                "flows": flows,
                "privacy_email": email,
                "privacy_email_confidence": email_conf,
                "required_info": req or [],
                "verification": sorted(set(verification)),
                "id_document": "id_document" in verification,
                "processing_days": days,
                "processing_days_source": days_src,
                "registry_reported_median_days": reported_median,
                "recheck_after_days": (spec or {}).get("timing", {}).get("recheck_after_days") or (days + 7 if days else RECHECK_DEFAULT),
                "relist_check_interval_days": (spec or {}).get("timing", {}).get("relist_check_interval_days") or RELIST_DEFAULT,
                "suppression_not_deletion": (spec or {}).get("removal", {}).get("suppression_not_deletion"),
            },
            "search": search,
            "jurisdiction": {
                "coverage": coverage,
                "statutory_bases": statutory,
                "registries": ([{"registry": "ca_cppa", "legal_name": rg["legal_name"], "dba": rg.get("dba"), "year": rg["registry_year"],
                                 "fcra_regulated": rg.get("fcra_regulated"), "collects_precise_geolocation": rg.get("collects_precise_geolocation")}] if rg else []),
                "drop_participant": True if rg else None,
            },
            "automation": {
                "v1_class": auto.get("classification"),
                "research_status": ver.get("research_status"),
                "spec_status": lifecycle,
                "blockers": blockers,
                "bot_wall": "bot_wall" in blockers or lv.get("verdict") == "blocked",
                "tos_policy": (spec or {}).get("tos", {}).get("policy_decision"),
                "adapter_ready": lifecycle in ("e2e_tested", "live"),
            },
            "provenance": prov,
        }
        brokers.append(broker)

    counts = {
        "active": len(brokers),
        "archived": len(archived),
        "by_category": dict(sorted(Counter(b["category"] for b in brokers).items())),
        "with_privacy_email": sum(1 for b in brokers if b["removal"]["privacy_email"]),
        "with_processing_days": sum(1 for b in brokers if b["removal"]["processing_days"]),
        "cppa_registered": sum(1 for b in brokers if b["jurisdiction"]["registries"]),
        "with_spec": sum(1 for b in brokers if b["automation"]["spec_status"]),
        "primary_flow": dict(sorted(Counter(b["removal"]["primary_flow"] for b in brokers).items())),
    }
    body = json.dumps(brokers, sort_keys=True, ensure_ascii=False)
    meta = {
        "export_version": EXPORT_VERSION,
        "repo": "cvs0/data-brokers-list",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "counts": counts,
        "precedence": "v2 spec > v1 evidence > CPPA registry > page fill",
    }
    out = {"meta": meta, "template_by_residency": TEMPLATE_BY_RESIDENCY,
           "templates": {k: {kk: vv for kk, vv in v.items() if kk != "body"} for k, v in templates.items()},
           "brokers": brokers, "archived": archived}
    OUT_JSON.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_NDJSON.write_text("".join(json.dumps(b, ensure_ascii=False, sort_keys=True) + "\n" for b in brokers), encoding="utf-8")
    OUT_TEMPLATES.write_text(json.dumps({"templates": templates, "template_by_residency": TEMPLATE_BY_RESIDENCY}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(counts, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
