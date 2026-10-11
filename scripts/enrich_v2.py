#!/usr/bin/env python3
"""Apply registry matches and page fills to the v2 specs (data/v2/brokers/*.yaml).

Only fills values that are null/empty in a spec; never overwrites a reviewed value.
Each change gets a provenance entry (source registry or page_text) with its URL and date.

- jurisdiction.registries / drop_participant from data/v1/registry-matches.json
- legal_bases ccpa_delete + ccpa_opt_out (US-CA) when CPPA-registered
- removal email channel (template by basis) when a privacy email is known and no email channel exists
- email channel template_id: ccpa_delete when registered, else voluntary_optout
- timing.processing_days_documented from page fills (extracted only)
"""

from __future__ import annotations

import json
from datetime import date

from catalog import ROOT
from v2_common import dump_yaml, load_yaml, spec_paths

REG = ROOT / "data" / "v1" / "registry-matches.json"
FILLS = ROOT / "data" / "v1" / "field-fills.json"
EXPORT = ROOT / "data" / "v2" / "frame-export.json"


def main() -> int:
    reg = json.loads(REG.read_text())["matches"] if REG.exists() else {}
    fills = json.loads(FILLS.read_text())["entries"] if FILLS.exists() else {}
    exp = {b["id"]: b for b in json.loads(EXPORT.read_text())["brokers"]} if EXPORT.exists() else {}
    changed = 0
    for path in spec_paths():
        text = path.read_text(encoding="utf-8")
        header = "\n".join(l for l in text.splitlines() if l.startswith("#"))
        spec = load_yaml(path)
        before = json.dumps(spec, sort_keys=True, default=str)
        sid = spec["id"]
        prov = spec.setdefault("provenance", {}) or {}
        spec["provenance"] = prov
        j = spec["jurisdiction"]
        rg = reg.get(sid)
        if rg and not any(r["registry"] == "ca_cppa" for r in j["registries"]):
            j["registries"].append({"registry": "ca_cppa", "registration_id": None, "registered_name": rg["legal_name"],
                                    "checked_at": rg["checked_at"], "source_url": rg["source_url"]})
            prov["/jurisdiction/registries"] = {"checked_at": rg["checked_at"], "source": "registry", "evidence_url": rg["source_url"]}
        if rg and j.get("drop_participant") is None:
            j["drop_participant"] = True
            prov["/jurisdiction/drop_participant"] = {"checked_at": rg["checked_at"], "source": "registry", "evidence_url": rg["source_url"],
                                                      "note": "Listed in the CPPA data broker registry (domain match)."}
        if rg:
            have = {b["basis"] for b in j["legal_bases"]}
            for basis, days in (("ccpa_delete", 45), ("ccpa_opt_out", 15)):
                if basis not in have:
                    j["legal_bases"].append({"basis": basis, "eligible_residency": ["US-CA"], "statutory_response_days": days,
                                             "source_url": rg["source_url"]})
        # email channel
        chans = spec["removal"]["channels"]
        email = (exp.get(sid) or {}).get("removal", {}).get("privacy_email")
        econf = (exp.get(sid) or {}).get("removal", {}).get("privacy_email_confidence")
        has_email = any(c["channel"] == "email" for c in chans)
        if email and not has_email and econf in ("v1", "registry", "extracted"):
            rank = max([c["rank"] for c in chans] or [0]) + 1
            chans.append({"channel": "email", "rank": rank, "email": email,
                          "template_id": "ccpa_delete" if rg else "voluntary_optout",
                          "requires": {"captcha": "none", "captcha_provider": None, "email_verification": None, "login": False,
                                       "profile_url": False, "phone_verification": None, "id_document": None},
                          "steps": [{"order": 1, "type": "send_email", "target": "ccpa_delete" if rg else "voluntary_optout"}]})
            src = (exp[sid].get("provenance") or {}).get("privacy_email") or {}
            prov[f"/removal/channels/{len(chans) - 1}/email"] = {
                "checked_at": src.get("checked_at", date.today().isoformat()),
                "source": "registry" if econf == "registry" else "page_text",
                "evidence_url": src.get("source_url") or exp[sid].get("privacy_policy_url") or exp[sid]["opt_out_url"]}
        for c in chans:
            if c["channel"] == "email" and rg and c.get("template_id") in (None, "voluntary_optout"):
                c["template_id"] = "ccpa_delete"
                for st in c.get("steps", []):
                    if st["type"] == "send_email":
                        st["target"] = "ccpa_delete"
        # processing days
        f = fills.get(sid, {}).get("processing_time")
        if f and f["confidence"] == "extracted" and spec["timing"].get("processing_days_documented") is None and f["value"].get("days_upper_bound"):
            spec["timing"]["processing_days_documented"] = f["value"]["days_upper_bound"]
            prov["/timing/processing_days_documented"] = {"checked_at": f["checked_at"], "source": "page_text", "evidence_url": f["source_url"],
                                                          "note": f["value"]["text"][:200]}
        if json.dumps(spec, sort_keys=True, default=str) != before:
            spec["lifecycle"]["updated_at"] = date.today().isoformat()
            dump_yaml(spec, path, header)
            changed += 1
    print(json.dumps({"specs_changed": changed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
