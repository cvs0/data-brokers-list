#!/usr/bin/env python3
"""Apply headless-browser observations and the freshness recheck.

Does not submit forms. Does not set proxy_required true. Unknown stays unknown.
Catalog columns and stable ids are left unchanged.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

from build_dataset import (
    DATASET_VERSION,
    SCHEMA_VERSION,
    load_overrides,
    load_url_overrides,
    merge,
    progress_report,
    render_progress_md,
    research_record,
    utc_now,
)
from catalog import ROOT, assign_ids, load_catalog

DATASET_PATH = ROOT / "data" / "v1" / "broker-opt-outs.enriched.json"
BROWSER_LOG = ROOT / "research" / "evidence" / "browser-pass.jsonl"
FRESHNESS_LOG = ROOT / "research" / "evidence" / "freshness.jsonl"
SHORTLIST_PATH = ROOT / "data" / "v1" / "automation-shortlist.json"
PORTALS_PATH = ROOT / "data" / "v1" / "shared-portals.json"
PARENTS_PATH = ROOT / "data" / "v1" / "parent-companies.json"

WORKFLOW_KEYS = (
    "submission_methods",
    "steps",
    "required_fields",
    "optional_fields",
    "fields_observation",
    "profile_url_or_record_id_required",
    "account_login_required",
    "email_verification_required",
    "email_verification_mechanism",
    "phone_sms_verification_required",
    "captcha_present",
    "captcha_type",
    "captcha_provider",
    "captcha_stage",
    "captcha_requirement",
)

PAYWALL_RE = re.compile(
    r"\b(unlock the report|subscription required|must subscribe|pay to view|purchase a (report|membership))\b",
    re.I,
)
FREE_RE = re.compile(r"\b(free people search|search for free|free search|no charge to search)\b", re.I)
ELIGIBILITY_RE = re.compile(
    r"\b(opt out|opt-out|omitted from|omit your|do not call|personal information|"
    r"privacy concern|advertising mail|advertising materials|pipeda|anti-spam|"
    r"struck from|removed from the (?:list|register)|right to access|right to delete)\b",
    re.I,
)
REQUEST_PHRASE = re.compile(
    r"\b(do not sell my|delete my personal|opt out|opt-out|remove my|suppression|privacy request|"
    r"data subject|request deletion|request access|your privacy choices|right to access|right to delete|"
    r"right to correct|verification email)\b",
    re.I,
)
HOST_GEO = (
    ("elections.bc.ca", "CA-BC", ("british columbia",)),
    ("electionsquebec.qc.ca", "CA-QC", ("québec", "quebec")),
    ("elections.on.ca", "CA-ON", ("ontario",)),
    ("registertovoteon.ca", "CA-ON", ("ontario",)),
    ("elections.ab.ca", "CA-AB", ("alberta",)),
    ("elections.sk.ca", "CA-SK", ("saskatchewan",)),
    ("electionsmanitoba.ca", "CA-MB", ("manitoba",)),
    ("elections.ns.ca", "CA-NS", ("nova scotia",)),
    ("electionsnb.ca", "CA-NB", ("new brunswick",)),
    ("electionspei.ca", "CA-PE", ("prince edward island",)),
    ("elections.gov.nl.ca", "CA-NL", ("newfoundland",)),
    ("elections.yukon.ca", "CA-YT", ("yukon",)),
    ("electionsnwt.ca", "CA-NT", ("northwest territories",)),
    ("elections.nu.ca", "CA-NU", ("nunavut",)),
    ("priv.gc.ca", "CA", ("canada", "pipeda")),
    ("lnnte-dncl.gc.ca", "CA", ("canada", "do not call")),
    ("crtc.gc.ca", "CA", ("canada", "anti-spam", "casl")),
    ("fightspam.gc.ca", "CA", ("canada", "anti-spam", "casl")),
    ("oipc.bc.ca", "CA-BC", ("british columbia",)),
    ("oipc.ab.ca", "CA-AB", ("alberta",)),
    ("oipc.sk.ca", "CA-SK", ("saskatchewan",)),
    ("ombudsman.mb.ca", "CA-MB", ("manitoba",)),
    ("ipc.on.ca", "CA-ON", ("ontario",)),
    ("cai.gouv.qc.ca", "CA-QC", ("québec", "quebec")),
    ("canadapost-postescanada.ca", "CA", ("canada",)),
    ("thecma.ca", "CA", ("canada",)),
    ("canada411.ca", "CA", ("canada",)),
)


def load_jsonl(path):
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    rows = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        item, end = decoder.raw_decode(text, index)
        rows.append(item)
        index = end
    return rows


def to_evidence(item: dict) -> dict:
    return {
        "url": item["url"],
        "final_url": item.get("final_url") or item["url"],
        "http_status": item.get("http_status"),
        "title": item.get("title") or "",
        "text_excerpt": item.get("text_excerpt") or "",
        "text_length": item.get("text_length") or len(item.get("text_excerpt") or ""),
        "forms": item.get("forms") or [],
        "captcha_markers": item.get("captcha_markers") or [],
        "emails": item.get("emails") or [],
        "redirects": [],
        "fetched_at": item.get("started_at"),
        "challenge": item.get("outcome") == "challenge",
        "error": item.get("error"),
        "non_html": False,
        "request_user_agent": "Playwright headless Chromium",
        "platform_markers": [],
        "privacy_policy_links": [],
        "official_homepage_candidate": None,
        "form_field_names": [
            field.get("name") or field.get("id") or field.get("placeholder")
            for form in item.get("forms") or []
            for field in form.get("fields") or []
            if field.get("name") or field.get("id") or field.get("placeholder")
        ],
    }


def browser_note(item: dict) -> str:
    clicks = item.get("safe_clicks") or []
    parts = [
        f"Headless browser outcome {item.get('outcome')} "
        f"(HTTP {item.get('http_status')}). No form was submitted and no CAPTCHA was solved."
    ]
    if item.get("wizard_hints"):
        parts.append("Visible wizard text: " + "; ".join(item["wizard_hints"]) + ".")
    else:
        match = re.search(r"step\s+\d+[^\n]{0,80}", item.get("text_excerpt") or "", re.I)
        if match:
            parts.append("Rendered text includes: " + " ".join(match.group(0).split()) + ".")
    if item.get("captcha_visible"):
        parts.append("CAPTCHA markers in the rendered page: " + ", ".join(item.get("captcha_markers") or []) + ".")
    elif item.get("outcome") == "readable":
        parts.append("No CAPTCHA marker was visible on the inspected screen.")
    for click in clicks:
        if click.get("result") == "clicked":
            parts.append(
                f"Clicked non-submit control {click.get('text')!r}. Landed on {click.get('to_url')}."
            )
        elif click.get("reason") == "submit_button":
            parts.append(
                f"A control labeled {click.get('text')!r} is a submit button. It was not clicked, "
                "so any later CAPTCHA or wizard step was not observed."
            )
        elif click.get("result") == "not_clicked":
            parts.append(f"Did not click {click.get('text')!r} ({click.get('reason')}).")
    if item.get("post_click_captcha"):
        parts.append("After a non-submit click, CAPTCHA markers were: " + ", ".join(item["post_click_captcha"]) + ".")
    final_url = item.get("final_url")
    if final_url and final_url.rstrip("/") != item["url"].rstrip("/"):
        parts.append(f"Browser final URL: {final_url}")
    return " ".join(parts)


def inspection_object(item: dict) -> dict:
    outcome = item.get("outcome") or "error"
    if outcome not in {"readable", "challenge", "http_error", "timeout", "error"}:
        outcome = "error"
    hints = item.get("wizard_hints") or []
    return {
        "inspected_at": item.get("started_at"),
        "outcome": outcome,
        "final_url": item.get("final_url"),
        "http_status": item.get("http_status"),
        "captcha_visible": item.get("captcha_visible"),
        "wizard_hint": "; ".join(hints) if hints else None,
        "notes": browser_note(item)[:1200],
    }


def append_step(record: dict, action: str, observed: bool, source: str, evidence: str) -> None:
    steps = record["request_workflow"]["steps"]
    if any(step.get("action") == action for step in steps):
        return
    if steps and steps[0].get("action") == "unknown" and steps[0].get("source") == "not_inspected":
        steps.clear()
    steps.append(
        {
            "order": len(steps) + 1,
            "action": action,
            "observed": observed,
            "source": source,
            "evidence": evidence[:500],
        }
    )


def downgrade_unattended(record: dict) -> None:
    if record["automation"]["classification"] != "automation_candidate":
        return
    record["automation"]["classification"] = "assisted_workflow"
    record["automation"]["classification_evidence"] = (
        record["automation"]["classification_evidence"]
        + " This pass does not mark the flow unattended: it was not submitted, so a later CAPTCHA or confirmation screen is still possible."
    )


def stamp_access(record: dict, item: dict, previous: dict) -> None:
    direct = record["automation"]["direct_access"]
    previous_quality = previous["content_quality"]
    previous_proxy = previous["proxy_required"]
    if item.get("outcome") == "readable" and (item.get("http_status") or 0) < 400:
        direct["method"] = "headless_browser"
        direct["observed_http_status"] = item.get("http_status")
        direct["final_url"] = item.get("final_url")
        direct["content_quality"] = "readable"
        direct["user_agent"] = "Playwright headless Chromium"
        if previous_quality == "readable" and previous_proxy is False:
            direct["proxy_required"] = False
            direct["proxy_evidence"] = (
                "An earlier direct GET returned readable text, and this headless browser also rendered the page. "
                "Proxy access was not tested."
            )
        else:
            direct["proxy_required"] = None
            direct["proxy_evidence"] = (
                "A headless browser rendered readable text after the research GET was blocked or incomplete. "
                "Proxy access was not tested. This does not show that a residential proxy is required, "
                "and it does not show that a plain HTTP client can read the page."
            )
        direct["notes"] = (
            "Headless browser inspection on 2026-10-01. No form was submitted, no CAPTCHA was solved, "
            "and no account was created."
        )
        record["verification"]["inspection_channel"] = "rendered_reader"
        return
    # Still blocked or errored. Do not infer a proxy.
    direct["proxy_required"] = None if previous_proxy is not False else False
    note = (
        f" Headless browser retry on 2026-10-01 outcome was {item.get('outcome')} "
        f"(HTTP {item.get('http_status')}). Proxy access was not tested, and this retry is not evidence "
        "that a residential proxy is required."
    )
    if note.strip() not in (direct.get("notes") or ""):
        direct["notes"] = ((direct.get("notes") or "").rstrip() + note)[:800]
    if previous_quality != "readable":
        direct["proxy_evidence"] = (
            "Proxy access was not tested. A 403, 429, timeout, challenge page, or browser interstitial "
            "does not show that a residential proxy is required."
        )


def copy_workflow_if_better(record: dict, computed: dict) -> None:
    current = record["request_workflow"]["fields_observation"]
    incoming = computed["request_workflow"]["fields_observation"]
    if incoming != "form_parsed" or current == "form_parsed":
        if computed["request_workflow"]["captcha_present"] is True and current != "not_inspected":
            if record["request_workflow"]["captcha_present"] is not True:
                for key in (
                    "captcha_present",
                    "captcha_type",
                    "captcha_provider",
                    "captcha_stage",
                    "captcha_requirement",
                ):
                    record["request_workflow"][key] = computed["request_workflow"][key]
                if "captcha" not in record["automation"]["observed_blockers"]:
                    record["automation"]["observed_blockers"].append("captcha")
        return
    for key in WORKFLOW_KEYS:
        record["request_workflow"][key] = computed["request_workflow"][key]
    record["verification"]["verification_level"] = "workflow_partially_inspected"
    if computed["verification"]["research_status"] in {"verified", "partial"}:
        # A blocked page that now shows a request path can move up. Do not demote verified.
        if record["verification"]["research_status"] in {"blocked", "partial", "unresearched"}:
            record["verification"]["research_status"] = computed["verification"]["research_status"]
    if computed["verification"]["workflow_kind"] != "unknown":
        if record["verification"]["workflow_kind"] in {"unknown", "privacy_policy_only"}:
            record["verification"]["workflow_kind"] = computed["verification"]["workflow_kind"]
    if record["verification"]["research_status"] == "blocked" or not record["automation"].get("classification_evidence"):
        record["automation"]["classification"] = computed["automation"]["classification"]
        record["automation"]["classification_evidence"] = computed["automation"]["classification_evidence"]
        record["automation"]["observed_blockers"] = list(computed["automation"]["observed_blockers"])
    else:
        record["automation"]["classification"] = computed["automation"]["classification"]
        for blocker in computed["automation"]["observed_blockers"]:
            if blocker not in record["automation"]["observed_blockers"]:
                record["automation"]["observed_blockers"].append(blocker)
    for step in computed["automation"]["human_steps"]:
        if step not in record["automation"]["human_steps"]:
            record["automation"]["human_steps"].append(step)
    for step in computed["automation"]["automatable_steps"]:
        if step not in record["automation"]["automatable_steps"]:
            record["automation"]["automatable_steps"].append(step)
    if computed["identity"]["supported_request_types"] != ["unknown"]:
        if record["identity"]["supported_request_types"] == ["unknown"]:
            record["identity"]["supported_request_types"] = computed["identity"]["supported_request_types"]
    downgrade_unattended(record)


def augment_readable(record: dict, computed: dict, item: dict) -> None:
    copy_workflow_if_better(record, computed)
    note = browser_note(item)
    existing = record["verification"].get("evidence_notes") or ""
    if "Headless browser outcome" not in existing:
        record["verification"]["evidence_notes"] = (existing + " " + note).strip()[:2000]
    if item.get("url") not in record["verification"]["evidence_urls"]:
        record["verification"]["evidence_urls"].append(item["url"])
    clicks = item.get("safe_clicks") or []
    if any(click.get("reason") == "submit_button" for click in clicks):
        label = next(click.get("text") for click in clicks if click.get("reason") == "submit_button")
        append_step(
            record,
            "A later control is a submit button. It was not clicked.",
            False,
            "browser_render",
            f"The rendered page showed {label!r}. No personal data was entered, so post-submit screens were not observed.",
        )
    record["verification"]["browser_inspection"] = inspection_object(item)
    if record["verification"]["research_status"] == "verified" and not record["verification"]["evidence_urls"]:
        record["verification"]["evidence_urls"] = [item["url"]]
    downgrade_unattended(record)


def replace_blocked(record: dict, computed: dict, item: dict, url_overrides: dict, id_overrides: dict) -> None:
    grouping = {
        "sister_brands": record["identity"]["sister_brands"],
        "shared_portal_group_id": record["identity"]["shared_portal_group_id"],
        "workflow_family_id": record["identity"]["workflow_family_id"],
        "parent_company": record["identity"]["parent_company"],
    }
    for section in ("identity", "discovery", "request_workflow", "automation", "verification"):
        record[section] = computed[section]
    record["identity"]["sister_brands"] = grouping["sister_brands"]
    record["identity"]["shared_portal_group_id"] = grouping["shared_portal_group_id"]
    record["identity"]["workflow_family_id"] = grouping["workflow_family_id"]
    if grouping["parent_company"]:
        record["identity"]["parent_company"] = grouping["parent_company"]
    merged = merge(record, url_overrides.get(record["catalog"]["url"]) or {})
    merged = merge(merged, id_overrides.get(record["id"]) or {})
    record.clear()
    record.update(merged)
    record["verification"]["browser_inspection"] = inspection_object(item)
    note = browser_note(item)
    record["verification"]["evidence_notes"] = (
        (record["verification"].get("evidence_notes") or "") + " " + note
    ).strip()[:2000]
    if computed["request_workflow"]["fields_observation"] == "form_parsed":
        copy_workflow_if_better(record, computed)
    downgrade_unattended(record)


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [part.strip() for part in parts if part and len(part.strip()) > 40]


def calm_blocked_upgrade(record: dict, item: dict, previous_status: str) -> None:
    """Do not call a page verified from a weak phrase match or a nav menu."""
    if previous_status != "blocked":
        return
    if record["verification"]["research_status"] != "verified":
        return
    text = item.get("text_excerpt") or ""
    if REQUEST_PHRASE.search(text):
        return
    record["verification"]["research_status"] = "partial"
    record["verification"]["confidence"] = "low"
    record["identity"]["supported_request_types"] = ["unknown"]
    if record["verification"]["workflow_kind"] in {"dsar_portal", "public_listing_form", "privacy_policy_only"}:
        record["verification"]["workflow_kind"] = "privacy_policy_only" if "privacy" in text.lower() else "unknown"


def add_eligibility(record: dict, text: str, url: str) -> list[str]:
    found = []
    lowered_url = url.lower()
    blob = text.lower()
    official_host = any(host in lowered_url for host, _code, _needles in HOST_GEO)
    for host, code, needles in HOST_GEO:
        if host in lowered_url and any(needle in blob for needle in needles):
            coverage = record["identity"]["geographic_coverage"]
            if coverage == ["unknown"]:
                record["identity"]["geographic_coverage"] = [code]
            elif code not in coverage:
                record["identity"]["geographic_coverage"] = [item for item in coverage if item != "unknown"] + [code]
    for sentence in sentences(text):
        if not ELIGIBILITY_RE.search(sentence):
            continue
        low = sentence.lower()
        if not official_host and not any(
            token in low
            for token in (
                "canada",
                "canadian",
                "pipeda",
                "casl",
                "anti-spam",
                "province",
                "québec",
                "quebec",
                "ontario",
                "alberta",
                "british columbia",
                "manitoba",
                "saskatchewan",
            )
        ):
            continue
        if len(sentence) < 80 or sentence.count(" ") < 10:
            continue
        lowered = sentence.lower()
        if ">" in sentence[:30] or "click here" in lowered or "cookie" in lowered or "web analytics" in lowered:
            continue
        if not re.search(r"\b(you|your|resident|consumer|individual|person)\b", lowered):
            continue
        quote = sentence[:400]
        if quote not in found:
            found.append(quote)
        if len(found) >= 3:
            break
    if not found:
        return []
    current = record["identity"]["documented_request_eligibility"]
    if current == ["unknown"]:
        record["identity"]["documented_request_eligibility"] = found
    else:
        for quote in found:
            if quote not in current and len(current) < 4:
                current.append(quote)
    return found


def apply_search(record: dict, item: dict) -> None:
    if item.get("outcome") != "readable":
        record["discovery"]["search_observation"] = (
            f"Homepage browser outcome was {item.get('outcome')} (HTTP {item.get('http_status')}). "
            "A search query was not sent. Whether a listing is visible without payment remains unknown."
        )
        record.setdefault("verification", {})
        return
    search = item.get("search") or {}
    text = item.get("text_excerpt") or ""
    inputs = [value for value in (search.get("inputs") or []) if value != "unknown"]
    if inputs:
        current = record["discovery"]["supported_search_inputs"]
        if current == ["unknown"]:
            record["discovery"]["supported_search_inputs"] = inputs
        else:
            for value in inputs:
                if value not in current:
                    current.append(value)
    captcha = bool(item.get("captcha_visible"))
    login_copy = bool(re.search(r"\b(sign in to|log in to|create an account to|login to search)\b", text, re.I))
    paywall = bool(PAYWALL_RE.search(text))
    free_copy = FREE_RE.search(text)
    if inputs and not login_copy:
        record["discovery"]["public_profiles_searchable"] = True
        record["discovery"]["search_requires_login"] = False
    elif login_copy and not inputs:
        record["discovery"]["search_requires_login"] = True
    if captcha and inputs:
        record["discovery"]["search_requires_captcha"] = True
    elif inputs and item.get("captcha_visible") is False:
        record["discovery"]["search_requires_captcha"] = False
    if paywall:
        record["discovery"]["search_requires_payment"] = True
    elif free_copy:
        record["discovery"]["search_requires_payment"] = False
    quality = "page_not_useful"
    if inputs and paywall:
        quality = "paywall_copy_on_landing_results_not_opened"
    elif inputs and free_copy:
        quality = "free_search_copy_results_not_opened"
    elif inputs:
        quality = "search_form_public_results_not_opened"
    elif login_copy:
        quality = "login_copy_without_a_parsed_search_form"
    else:
        quality = "no_search_form_parsed_results_not_opened"
    labels = ", ".join((search.get("input_labels") or [])[:6])
    record["discovery"]["search_observation"] = (
        f"Public page {item.get('final_url') or item['url']} loaded in a headless browser. "
        f"Search-input kinds parsed: {inputs or ['none']}. "
        f"CAPTCHA marker on that page: {captcha}. "
        f"Exposure-check quality: {quality}. "
        "No name, phone, or email was typed and a search query was not sent, so a result-page paywall was not tested. "
        + (f"Field labels: {labels}." if labels else "")
    )[:900]
    if record["discovery"]["exposure_check"] == "unknown" and free_copy and inputs and not paywall and not login_copy:
        # The page offers a free search. A listing was not opened, so exposure stays unknown
        # unless the page itself is the search form for a people-search site and says the search is free.
        # Keep unknown. The observation records the quality.
        pass


def index_observations(items: list[dict]) -> dict:
    workflow = {}
    search = {}
    canadian = {}
    extras = []
    for item in items:
        mode = item.get("mode")
        if mode in {"shortlist", "blocked"}:
            previous = workflow.get(item["url"])
            rank = {"readable": 3, "http_error": 2, "challenge": 1}.get(item.get("outcome"), 0)
            previous_rank = 0
            if previous:
                previous_rank = {"readable": 3, "http_error": 2, "challenge": 1}.get(previous.get("outcome"), 0)
                if previous.get("mode") == "shortlist" and item.get("mode") != "shortlist" and rank == previous_rank:
                    continue
            if previous is None or rank > previous_rank or (
                rank == previous_rank and item.get("mode") == "shortlist"
            ):
                workflow[item["url"]] = item
        elif mode == "search":
            for entry_id in item.get("entry_ids") or []:
                search[entry_id] = item
        elif mode == "canadian":
            if item.get("entry_ids"):
                canadian[item["url"]] = item
            else:
                extras.append(item)
    return {"workflow": workflow, "search": search, "canadian": canadian, "extras": extras}


def norm_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    host = host.removeprefix("www.")
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return f"{host}{path}"


def freshness_flags(catalog_rows: list[dict]) -> dict:
    log = {item["url"]: item for item in load_jsonl(FRESHNESS_LOG)}
    counts = Counter()
    flagged = []
    seen = set()
    for row in catalog_rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        members = [other for other in catalog_rows if other["url"] == row["url"]]
        item = log.get(row["url"])
        if not item:
            counts["not_checked"] += 1
            continue
        status = item.get("http_status")
        error = item.get("error") or ""
        final_url = item.get("final_url") or ""
        quality = item.get("content_quality")
        if error and status is None:
            kind = "unresolved_or_network_error"
        elif status in {404, 410}:
            kind = "dead"
        elif quality == "challenge" or status in {401, 403, 429, 503}:
            kind = "blocked"
        elif final_url and norm_url(final_url) != norm_url(row["url"]):
            same_host = (urlparse(final_url).hostname or "").lower().removeprefix("www.") == (
                urlparse(row["url"]).hostname or ""
            ).lower().removeprefix("www.")
            kind = "moved_same_site" if same_host else "moved_other_host"
        elif str(row["http_status"]).isdigit() and status and int(row["http_status"]) != status:
            kind = "status_changed"
        else:
            kind = "unchanged"
        counts[kind] += 1
        if kind != "unchanged":
            flagged.append(
                {
                    "entry_ids": [member["id"] for member in members],
                    "names": [member["name"] for member in members],
                    "url": row["url"],
                    "drift": kind,
                    "catalog_http_status": row["http_status"],
                    "observed_http_status": status,
                    "final_url": final_url or None,
                    "error": error[:240] or None,
                }
            )
    return {"drift_counts": dict(sorted(counts.items())), "flagged": flagged}


def playbook_for(record: dict) -> dict:
    workflow = record["request_workflow"]
    verification = record["verification"]
    return {
        "entry_id": record["id"],
        "name": record["catalog"]["name"],
        "request_url": record["catalog"]["url"],
        "for_authorized_tests_only": True,
        "requests_submitted_in_this_research": False,
        "research_status": verification["research_status"],
        "verification_level": verification["verification_level"],
        "workflow_kind": verification["workflow_kind"],
        "acknowledgement": verification.get("submission_acknowledgement")
        or "Unknown. The inspected page did not describe an acknowledgement, and this research did not receive one.",
        "broker_confirmation": verification.get("broker_confirmation")
        or (
            "Unknown. broker_confirmation is null because no request was submitted. "
            "A future authorized test would need a message or status the broker itself sends after the request, "
            "which this page did not demonstrate."
        ),
        "independent_removal_check": verification.get("independent_removal_verification")
        or (
            "Unknown. The inspected page did not describe a public listing to re-open. "
            "Do not treat the absence of a new email as proof of removal."
        ),
        "human_gates": workflow and record["automation"]["observed_blockers"],
        "do_not_automate": [
            "Do not submit the form, create an account, upload an identity document, trigger SMS, or solve a CAPTCHA as part of research.",
            "A parsed field list is not a selector and not an API.",
        ],
        "notes": (verification.get("evidence_notes") or "")[:700],
    }


def build_workflow_index(records: list[dict], portals: dict) -> dict:
    by_id = {record["id"]: record for record in records}
    groups = []
    for group in portals.get("shared_request_urls") or []:
        exceptions = []
        for member_id in group["member_ids"]:
            record = by_id.get(member_id)
            if not record:
                continue
            for sister in record["identity"]["sister_brands"]:
                if sister["relationship"] in {"verified_parent_exception", "verified_sister"}:
                    exceptions.append(
                        {
                            "from_entry_id": member_id,
                            "entry_id": sister["entry_id"],
                            "name": sister["name"],
                            "relationship": sister["relationship"],
                            "evidence_basis": sister["evidence_basis"],
                        }
                    )
        coverage = None
        evidence = group.get("evidence")
        for member_id in group["member_ids"]:
            record = by_id[member_id]
            if record["identity"]["single_request_covers_multiple_brands"] is True:
                coverage = True
                evidence = record["identity"]["single_request_coverage_evidence"] or evidence
                break
        groups.append(
            {
                "workflow_id": group["group_id"],
                "kind": "shared_request_url",
                "separate_submissions": False,
                "single_request_covers_members": coverage,
                "representative_entry_id": group["member_ids"][0],
                "member_ids": group["member_ids"],
                "request_url": group["request_url"],
                "brand_exceptions": exceptions,
                "notes": evidence,
            }
        )
    for family in portals.get("workflow_families") or []:
        groups.append(
            {
                "workflow_id": family["group_id"],
                "kind": "shared_form_signature",
                "separate_submissions": True,
                "single_request_covers_members": None,
                "representative_entry_id": family["member_ids"][0],
                "member_ids": family["member_ids"],
                "path": family.get("path"),
                "brand_exceptions": [
                    {
                        "name": "Government arrest record",
                        "relationship": "out_of_scope",
                        "note": "A removal on these commercial repost sites does not erase the government arrest record.",
                    }
                ]
                if "arrests" in family["member_ids"][0]
                else [],
                "notes": family.get("evidence"),
            }
        )
    return {
        "purpose": (
            "Product work should implement one adapter per workflow_id. "
            "shared_request_url is one entry point. It does not by itself mean one submission removes every brand. "
            "shared_form_signature is one form shape with a separate submission per URL. "
            "Catalog-name families in parent-companies.json stay unverified until an official page says so."
        ),
        "workflows": groups,
    }


def write_findings(before: dict, report: dict, freshness: dict, canadian_summary: dict, search_summary: dict, browser_summary: dict) -> str:
    status = report["research_status"]
    return "\n".join(
        [
            "# Findings",
            "",
            "This pass kept the **1122** catalog rows and their stable ids. It added a headless-browser read of the automation shortlist and of catalog URLs whose research GET was blocked, a freshness recheck of every catalog URL, search-page observations, Canadian official-page notes, and verification playbooks. No privacy request was submitted. No account was created. No identity document was uploaded. No SMS was sent. No CAPTCHA was solved.",
            "",
            "`opt-outs.csv` columns are unchanged. Its `http_status` values remain the 2026-10-01 catalog check. Newer HTTP results are in `data/v1/freshness-report.json`.",
            "",
            "## Research status before and after",
            "",
            "| Status | Before | After |",
            "| --- | ---: | ---: |",
            f"| verified | {before['verified']} | {status.get('verified', 0)} |",
            f"| partial | {before['partial']} | {status.get('partial', 0)} |",
            f"| blocked | {before['blocked']} | {status.get('blocked', 0)} |",
            f"| not_applicable | {before['not_applicable']} | {status.get('not_applicable', 0)} |",
            "",
            f"Verification level after this pass: {report['verification_level']}.",
            f"Browser workflow outcomes: {browser_summary}.",
            "",
            "A blocked URL that stayed blocked was opened again in a headless browser. If that browser still showed a challenge, HTTP 403, or timeout, the record stays blocked. `proxy_required` was not set to true. A browser that rendered the page is also not proof that a residential proxy is unnecessary for a plain HTTP client.",
            "",
            "## Shortlist",
            "",
            canadian_summary.get("shortlist_prose")
            or "Shortlist browser notes are on each candidate and in data/v1/verification-playbooks.json.",
            "",
            "## Freshness",
            "",
            f"Direct GET recheck on {freshness.get('checked_at', '2026-10-01')}. Drift counts: `{json.dumps(freshness['drift_counts'])}`.",
            "",
            "Moved means the final URL's host or path differs after ignoring a leading www and a trailing slash. Blocked means the recheck saw a challenge or HTTP 401/403/429/503. That is an access observation, not a dead page and not a proxy finding. Unresolved hostnames are network errors, not HTTP 404s. The catalog CSV was not rewritten.",
            "",
            "## Search signals",
            "",
            f"Homepage observations: {json.dumps(search_summary)}. No people-search query was submitted. `exposure_check` stays unknown when a result page was not opened. `search_observation` records whether the landing page showed a search form, a CAPTCHA marker, login copy, or paywall copy.",
            "",
            "## Canada",
            "",
            canadian_summary["prose"],
            "",
            "Eligibility sentences were copied only from pages this pass loaded. Provincial sites that did not load are listed as not loaded. They were not added to `opt-outs.csv`.",
            "",
            "## Unique workflows",
            "",
            "Shared request URLs and parsed-form families are indexed in `data/v1/workflow-groups.json`. One shared URL is one entry point. PeopleConnect's page says the suppression tool does not apply to Classmates.com or to user data; that exception is preserved. Arrests.org form families remove a commercial repost, not the government record. Catalog parenthetical brand hints remain unverified in `parent-companies.json`.",
            "",
            "## Origin mirror",
            "",
            canadian_summary.get("origin_note", "Origin sync is recorded in the pull request."),
            "",
            "## Still unresolved",
            "",
            "- Headless-browser challenges and HTTP 403/429 responses. A residential proxy was not tested.",
            "- Screens behind submit buttons, email links, accounts, SMS, and CAPTCHA.",
            "- Whether a free search form leads to a full listing without payment. Result pages were not opened.",
            "- Provincial voter and commissioner pages that did not load in this environment.",
            "- Sister brands that share only a catalog-name hint.",
            "- End-to-end acknowledgement, broker confirmation, and removal. None were tested.",
            "",
        ]
    )


def apply_observations(records: list[dict]) -> dict:
    items = load_jsonl(BROWSER_LOG)
    grouped = index_observations(items)
    url_overrides = load_url_overrides()
    id_overrides = load_overrides()
    rows = {row["id"]: row for row in assign_ids(load_catalog())}
    before = Counter(record["verification"]["research_status"] for record in records)
    browser_outcomes = Counter()
    upgraded = []
    for record in records:
        item = grouped["workflow"].get(record["catalog"]["url"])
        if not item:
            continue
        browser_outcomes[item.get("outcome") or "error"] += 1
        previous_status = record["verification"]["research_status"]
        previous_access = dict(record["automation"]["direct_access"])
        if item.get("outcome") == "readable" and (item.get("http_status") or 0) < 400:
            computed = research_record(rows[record["id"]], to_evidence(item))
            if previous_status == "blocked":
                replace_blocked(record, computed, item, url_overrides, id_overrides)
                if record["verification"]["research_status"] != "blocked":
                    upgraded.append(record["id"])
            else:
                augment_readable(record, computed, item)
            stamp_access(record, item, previous_access)
            calm_blocked_upgrade(record, item, previous_status)
        else:
            record["verification"]["browser_inspection"] = inspection_object(item)
            existing = record["verification"].get("evidence_notes") or ""
            addition = browser_note(item)
            if "Headless browser outcome" not in existing:
                record["verification"]["evidence_notes"] = (existing + " " + addition).strip()[:2000]
            stamp_access(record, item, previous_access)
            if previous_status == "blocked":
                record["verification"]["research_status"] = "blocked"
                if record["verification"]["verification_level"] == "end_to_end_tested":
                    record["verification"]["verification_level"] = "source_documented"

    search_summary = Counter()
    for record in records:
        item = grouped["search"].get(record["id"])
        if not item:
            continue
        apply_search(record, item)
        observation = record["discovery"].get("search_observation") or ""
        if "Exposure-check quality: " in observation:
            quality = observation.split("Exposure-check quality: ", 1)[1].split(".", 1)[0]
            search_summary[quality] += 1
        else:
            search_summary["not_readable"] += 1

    canadian_quotes = []
    for record in records:
        item = grouped["canadian"].get(record["catalog"]["url"])
        if not item:
            continue
        record["verification"]["browser_inspection"] = inspection_object(item)
        quotes = []
        bilingual = None
        if item.get("outcome") == "readable":
            quotes = add_eligibility(record, item.get("text_excerpt") or "", item["url"])
            follow = item.get("bilingual_follow") or {}
            if follow.get("outcome") == "readable":
                bilingual = follow.get("final_url") or follow.get("url")
                french_quotes = []
                for sentence in sentences(follow.get("text_excerpt") or ""):
                    if ELIGIBILITY_RE.search(sentence):
                        french_quotes.append(sentence[:300])
                    if len(french_quotes) >= 2:
                        break
                if french_quotes:
                    note = " French-language page " + bilingual + " loaded. Quote: " + french_quotes[0]
                    record["verification"]["evidence_notes"] = (
                        (record["verification"].get("evidence_notes") or "") + note
                    ).strip()[:2000]
        canadian_quotes.append(
            {
                "entry_id": record["id"],
                "name": record["catalog"]["name"],
                "url": item["url"],
                "outcome": item.get("outcome"),
                "http_status": item.get("http_status"),
                "eligibility_quotes": quotes,
                "bilingual_url": (item.get("bilingual_follow") or {}).get("final_url")
                or (item.get("bilingual_follow") or {}).get("url"),
                "bilingual_outcome": (item.get("bilingual_follow") or {}).get("outcome"),
                "in_catalog": True,
            }
        )
    extras = []
    for item in grouped["extras"]:
        quotes = []
        if item.get("outcome") == "readable":
            for sentence in sentences(item.get("text_excerpt") or ""):
                if len(sentence) < 80 or sentence.count(" ") < 10:
                    continue
                lowered = sentence.lower()
                if "cookie" in lowered or "web analytics" in lowered:
                    continue
                if not re.search(r"\b(you|your|resident|consumer|individual|person)\b", lowered):
                    continue
                if ELIGIBILITY_RE.search(sentence):
                    quotes.append(sentence[:400])
                if len(quotes) >= 3:
                    break
        extras.append(
            {
                "entry_id": None,
                "url": item["url"],
                "outcome": item.get("outcome"),
                "http_status": item.get("http_status"),
                "title": item.get("title"),
                "eligibility_quotes": quotes,
                "bilingual_url": (item.get("bilingual_follow") or {}).get("final_url"),
                "bilingual_outcome": (item.get("bilingual_follow") or {}).get("outcome"),
                "in_catalog": False,
                "final_url": item.get("final_url"),
            }
        )
    loaded = [item["url"] for item in extras if item["outcome"] == "readable" and item["eligibility_quotes"]]
    failed = [item["url"] for item in extras if item["outcome"] != "readable"]
    prose = (
        f"Catalog Canadian rows inspected in the browser: {len(canadian_quotes)}. "
        f"Additional official URLs that returned eligibility language: {len(loaded)}. "
        f"Additional official URLs that did not load or had no eligibility sentence: {len(failed)}. "
        "Quotes are stored in data/v1/canadian-coverage.json and were not inferred from the province name alone."
    )
    return {
        "before": dict(before),
        "browser_outcomes": dict(browser_outcomes),
        "upgraded_ids": upgraded,
        "search_summary": dict(search_summary),
        "canadian_rows": canadian_quotes,
        "canadian_extras": extras,
        "canadian_prose": prose,
    }


def main() -> None:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    records = dataset["records"]
    summary = apply_observations(records)
    portals = json.loads(PORTALS_PATH.read_text(encoding="utf-8"))
    parents = json.loads(PARENTS_PATH.read_text(encoding="utf-8"))
    # Attach brand exceptions onto the PeopleConnect portal from preserved sister links.
    for group in portals.get("shared_request_urls") or []:
        if group["group_id"].startswith("portal-suppression-peopleconnect"):
            group["brand_exceptions"] = [
                {
                    "name": "Classmates.com",
                    "entry_id": "classmates-com",
                    "exception": "The suppression page says the tool does not apply to Classmates.com or to user data.",
                }
            ]
    families = portals.get("workflow_families") or []
    shared = portals.get("shared_request_urls") or []
    report = progress_report(records, shared, families)
    dataset["schema_version"] = SCHEMA_VERSION
    dataset["dataset_version"] = DATASET_VERSION
    dataset["freshness_check_date"] = "2026-10-01"
    dataset["browser_pass_at"] = utc_now()
    dataset["research_generated_at"] = utc_now()
    dataset["requests_submitted"] = False
    dataset["methodology"] = (
        dataset.get("methodology", "")
        + " A later pass opened shortlist URLs, blocked URLs, people-search homepages, and Canadian official pages in headless Chromium. "
        "It did not submit forms, solve CAPTCHAs, create accounts, upload identity documents, or send SMS. "
        "A browser challenge was recorded as an access failure. proxy_required was not set to true. "
        "Catalog CSV columns were not changed."
    )
    rows = assign_ids(load_catalog())
    freshness = freshness_flags(rows)
    freshness_report = {
        "check_date": "2026-10-01",
        "checked_at": utc_now(),
        "method": (
            "Direct HTTP GET, no proxy, no form submission. www and trailing-slash differences are ignored. "
            "A blocked result is not a dead page and is not evidence that a residential proxy is required. "
            "opt-outs.csv http_status was not rewritten."
        ),
        "catalog_rows": len(rows),
        "unique_urls": len({row["url"] for row in rows}),
        **freshness,
    }
    workflow_index = build_workflow_index(records, portals)
    shortlist = json.loads(SHORTLIST_PATH.read_text(encoding="utf-8"))
    by_id = {record["id"]: record for record in records}
    for candidate in shortlist["candidates"]:
        record = by_id[candidate["entry_id"]]
        inspection = record["verification"].get("browser_inspection") or {}
        candidate["research_status"] = record["verification"]["research_status"]
        candidate["verification_level"] = record["verification"]["verification_level"]
        candidate["automation_classification"] = record["automation"]["classification"]
        candidate["blockers"] = list(record["automation"]["observed_blockers"])
        candidate["browser_note"] = inspection.get("notes") or "This shortlist URL was not in the browser log."
        if inspection.get("notes") and inspection["notes"][:180] not in candidate["why"]:
            candidate["why"] = (candidate["why"].rstrip() + " Browser pass: " + inspection["notes"])[:1400]
    playbooks = []
    seen_urls = set()
    for candidate in shortlist["candidates"]:
        record = by_id[candidate["entry_id"]]
        playbooks.append(playbook_for(record))
        seen_urls.add(record["catalog"]["url"])
    for record in records:
        if record["id"] in summary["upgraded_ids"] and record["catalog"]["url"] not in seen_urls:
            if record["verification"]["research_status"] == "verified":
                playbooks.append(playbook_for(record))
                seen_urls.add(record["catalog"]["url"])
    canadian = {
        "checked_at": utc_now(),
        "rule": "Eligibility text is a sentence from a page this pass loaded. Missing pages stay missing. Extra official URLs were not inserted into opt-outs.csv.",
        "catalog_rows": summary["canadian_rows"],
        "official_pages_not_in_catalog": summary["canadian_extras"],
    }
    (ROOT / "data" / "v1" / "freshness-report.json").write_text(
        json.dumps(freshness_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (ROOT / "data" / "v1" / "workflow-groups.json").write_text(
        json.dumps(workflow_index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (ROOT / "data" / "v1" / "canadian-coverage.json").write_text(
        json.dumps(canadian, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (ROOT / "data" / "v1" / "verification-playbooks.json").write_text(
        json.dumps(
            {
                "for_authorized_tests_only": True,
                "requests_submitted": False,
                "playbooks": playbooks,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    SHORTLIST_PATH.write_text(json.dumps(shortlist, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    PORTALS_PATH.write_text(json.dumps(portals, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (ROOT / "data" / "v1" / "research-progress.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (ROOT / "docs" / "research-progress.md").write_text(render_progress_md(report), encoding="utf-8")
    readable_names = []
    blocked_names = []
    for candidate in shortlist["candidates"]:
        outcome = (by_id[candidate["entry_id"]]["verification"].get("browser_inspection") or {}).get("outcome")
        if outcome == "readable":
            readable_names.append(candidate["name"])
        else:
            blocked_names.append(f"{candidate['name']} ({outcome or 'not_run'})")
    shortlist_prose = (
        "The 30 shortlist URLs were opened in headless Chromium. Submit buttons were not clicked, so a CAPTCHA that appears only after submit was not observed. "
        f"Readable: {', '.join(readable_names) or 'none'}. "
        f"Not readable: {', '.join(blocked_names) or 'none'}. "
        "Per-workflow notes are on each shortlist candidate and in data/v1/verification-playbooks.json."
    )
    moved = [item for item in freshness["flagged"] if item["drift"].startswith("moved") or item["drift"] in {"dead", "unresolved_or_network_error", "status_changed"}]
    freshness_lines = [
        "# Freshness check",
        "",
        "Recheck date: 2026-10-01. Method: direct HTTP GET from the research environment. No forms were submitted.",
        "`opt-outs.csv` was not rewritten. A blocked response is not treated as a dead page and is not evidence that a residential proxy is required.",
        "",
        "## Counts",
        "",
        "| Drift | Unique URLs |",
        "| --- | ---: |",
    ]
    for key, value in freshness["drift_counts"].items():
        freshness_lines.append(f"| `{key}` | {value} |")
    freshness_lines.extend(["", "## Moved, dead, unresolved, or status changed", ""])
    for item in moved:
        freshness_lines.append(
            f"- `{item['drift']}` {item['url']} catalog {item['catalog_http_status']} observed {item['observed_http_status']} final {item['final_url'] or item['error']}"
        )
    freshness_lines.append("")
    (ROOT / "docs" / "freshness.md").write_text("\n".join(freshness_lines), encoding="utf-8")
    (ROOT / "docs" / "workflow-groups.md").write_text(
        "\n".join(
            [
                "# Workflow groups",
                "",
                "Use `data/v1/workflow-groups.json` to pick one implementation target.",
                "",
                "A `shared_request_url` group is one published entry point. Sharing that URL does not, by itself, mean one submission removes every brand. `single_request_covers_members` is true only when an official page says so.",
                "",
                "PeopleConnect's suppression page says the tool does not apply to Classmates.com or to user data. That exception stays on the portal even though those brands share the suppression URL in the catalog.",
                "",
                "A `shared_form_signature` group is one form shape. Each member URL is still its own submission. The arrests.org families remove a commercial repost of an arrest record. They do not erase the government record.",
                "",
                "Parent companies in `data/v1/parent-companies.json` are verified only for PeopleConnect, and only for the suppression URL the page describes. Other brand families are `catalog_leads_unverified` because the relationship comes from the catalog name, not from an official page.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    findings = write_findings(
        summary["before"],
        report,
        freshness_report,
        {
            "prose": summary["canadian_prose"],
            "shortlist_prose": shortlist_prose,
            "origin_note": "Origin sync status is in the pull request. This file is updated if a sync tool returns a result.",
        },
        summary["search_summary"],
        summary["browser_outcomes"],
    )
    (ROOT / "docs" / "findings.md").write_text(findings, encoding="utf-8")
    DATASET_PATH.write_text(json.dumps(dataset, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (ROOT / "research" / "evidence" / "browser-pass-summary.json").write_text(
        json.dumps(
            {
                "before": summary["before"],
                "browser_outcomes": summary["browser_outcomes"],
                "upgraded_ids": summary["upgraded_ids"],
                "search_summary": summary["search_summary"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "before": summary["before"],
        "after": report["research_status"],
        "browser_outcomes": summary["browser_outcomes"],
        "upgraded": len(summary["upgraded_ids"]),
        "search": summary["search_summary"],
        "freshness": freshness["drift_counts"],
        "playbooks": len(playbooks),
    }, indent=2))


if __name__ == "__main__":
    main()
