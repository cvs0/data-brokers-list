"""Shared helpers for the v2 automation layer (data/v2/brokers/*.yaml).

v1 (data/v1/broker-opt-outs.enriched.json) stays the evidence layer.
v2 is the machine-executable layer and joins v1 by id.
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

import csv
import json
import math
import re

from catalog import ROOT

V2_DIR = ROOT / "data" / "v2"
BROKERS_DIR = V2_DIR / "brokers"
SCHEMA_PATH = ROOT / "data" / "schema" / "broker-automation.schema.json"
V1_DATASET = ROOT / "data" / "v1" / "broker-opt-outs.enriched.json"
IDS_PATH = ROOT / "data" / "v1" / "catalog-ids.csv"
EXAMPLES_DIR = ROOT / "docs" / "examples"

CSV_COLUMNS = [
    "id", "family_id", "covers_ids", "lifecycle_status", "priority_score", "priority_tier",
    "operator_country", "cppa_registration_id", "drop_participant", "publishes_canadians", "legal_bases",
    "search_method", "search_url_template", "search_inputs", "parse_strategy", "listing_id_regex",
    "match_fields_available", "primary_channel", "primary_channel_url", "fallback_channel", "privacy_email",
    "captcha", "email_verification", "profile_url_required", "login_required", "id_document", "removal_fields",
    "removes_scope", "suppression_not_deletion", "authorized_agent_accepted", "authorized_agent_requirements",
    "processing_days_documented", "processing_days_observed_p50", "recheck_after_days",
    "relist_check_interval_days", "relist_rate_observed", "min_interval_s", "max_per_day",
    "tos_automation_prohibited", "tos_policy_decision", "bot_wall_observed", "geo_block_observed",
    "adapter_id", "last_e2e_at", "last_e2e_result", "last_verified_at",
]


def load_yaml(path: Path):
    import yaml  # imported lazily so v1 scripts keep working without PyYAML

    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def dump_yaml(data, path: Path, header: str = "") -> None:
    import yaml

    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120, default_flow_style=False)
    path.write_text((header + "\n" if header else "") + text, encoding="utf-8")


def catalog_ids() -> set[str]:
    with IDS_PATH.open(newline="", encoding="utf-8") as handle:
        return {row["id"] for row in csv.DictReader(handle)}


def v1_records() -> dict[str, dict]:
    data = json.loads(V1_DATASET.read_text(encoding="utf-8"))
    return {record["id"]: record for record in data["records"]}


def spec_paths() -> list[Path]:
    return sorted(BROKERS_DIR.glob("*.yaml"))


# --- canonical field mapping -------------------------------------------------

_FIELD_RULES: list[tuple[str, str]] = [
    (r"(^|[^a-z])(first[_ -]?name|fname|given)", "first_name"),
    (r"(^|[^a-z])(last[_ -]?name|lname|surname|family[_ -]?name)", "last_name"),
    (r"(^|[^a-z])(middle)", "middle_name"),
    (r"(full[_ -]?name|your[_ -]?name|^name\b|\bname name\b)", "full_name"),
    (r"(e-?mail)", "email"),
    (r"(phone|tel\b|mobile)", "phone"),
    (r"(zip|postal)", "postal_code"),
    (r"(\bcity\b|town)", "city"),
    (r"(\bstate\b|province|region)", "region"),
    (r"(address1|street|\baddress\b)", "street_address"),
    (r"(\bdob\b|birth[_ -]?date|date[_ -]?of[_ -]?birth)", "birth_date"),
    (r"(birth[_ -]?year|year[_ -]?of[_ -]?birth)", "birth_year"),
    (r"(\bage\b)", "age"),
    (r"(profile|listing|record)[_ -]?(url|link)|\burl\b", "profile_url"),
    (r"(record[_ -]?id|report[_ -]?id)", "record_id"),
    (r"(signature)", "signature"),
]


def canonical_field(raw: str) -> str | None:
    """Map a v1 raw required_fields string (name + label + type) to a canonical field."""
    text = raw.lower()
    if re.search(r"(checkbox|captcha|recaptcha|submit|^subject\s|message|comment|request[_ -]?type|declare)", text):
        return None
    if re.search(r"first", text) and re.search(r"last", text) and not re.search(r"(first|last)[_ -]?name\S* (first|last)[_ -]?name", text):
        return "full_name"
    for pattern, name in _FIELD_RULES:
        if re.search(pattern, text):
            return name
    return None


def form_name(raw: str) -> str | None:
    token = raw.split(" ", 1)[0].strip()
    return token or None


# --- processing time ---------------------------------------------------------

_UNIT_DAYS = {"hour": 1 / 24, "day": 1, "business day": 1.4, "week": 7, "month": 30}
_NUM = r"(\d+(?:\.\d+)?)"
_PROC_RE = re.compile(
    rf"(?:{_NUM}\s*(?:-|–|to)\s*)?{_NUM}\s*(business day|working day|hour|day|week|month)s?",
    re.IGNORECASE,
)
_WORD_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10,
              "fourteen": 14, "fifteen": 15, "thirty": 30, "forty-five": 45}


def processing_days(text: str | None) -> int | None:
    """Upper bound in whole days from a sentence like 'processed in 24-48 hours'. None if unparsable."""
    if not text:
        return None
    t = text.lower()
    for word, num in _WORD_NUMS.items():
        t = re.sub(rf"\b{word}\b", str(num), t)
    t = t.replace("working day", "business day")
    best = None
    for match in _PROC_RE.finditer(t):
        upper = float(match.group(2))
        unit = match.group(3).lower()
        days = upper * _UNIT_DAYS[unit]
        best = days if best is None else max(best, days)
    if best is None:
        return None
    return max(1, math.ceil(best))
