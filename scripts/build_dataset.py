#!/usr/bin/env python3
"""Build the versioned enriched dataset from catalog rows and fetched evidence.

Catalog notes stay under catalog_claims. Research fields are filled only from
fetched page text, parsed forms, or an explicit manual override. No request is
submitted by this script.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from catalog import ROOT, assign_ids, load_catalog, shared_url_groups

EVIDENCE_PATH = ROOT / "research" / "evidence" / "url-evidence.jsonl"
OVERRIDES_PATH = ROOT / "research" / "manual" / "overrides.json"
URL_OVERRIDES_PATH = ROOT / "research" / "manual" / "url-overrides.json"
OUT_DIR = ROOT / "data" / "v1"
SCHEMA_VERSION = "1.0.0"
DATASET_VERSION = "1.0.0"

REQUEST_PATTERNS = {
    "public_listing_suppression": [
        "remove your listing",
        "opt out your listing",
        "opt-out your listing",
        "suppress your",
        "suppression tool",
        "suppression request",
        "remove my info",
        "remove the listing",
        "url of your profile",
        "url of the profile",
    ],
    "deletion": [
        "right to delete",
        "right to erasure",
        "deletion request",
        "request deletion",
        "delete your personal",
        "delete my personal",
        "request that we delete",
    ],
    "sale_sharing_opt_out": [
        "do not sell",
        "do-not-sell",
        "opt out of the sale",
        "opt-out of the sale",
        "sale or sharing",
        "sell or share",
        "do not sell or share",
    ],
    "marketing_opt_out": [
        "direct marketing",
        "marketing opt-out",
        "opt out of marketing",
        "marketing communications",
        "interest-based advertising",
        "firm offers",
        "prescreen",
    ],
    "access": [
        "right to know",
        "right to access",
        "access request",
        "request access",
        "copy of your personal",
        "copy of the personal information",
    ],
    "correction": [
        "right to correct",
        "correction request",
        "correct inaccuracies",
        "request correction",
    ],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_evidence() -> dict[str, dict]:
    found: dict[str, dict] = {}
    if not EVIDENCE_PATH.exists():
        return found
    with EVIDENCE_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            # Later lines win so a refetch replaces an older observation.
            found[item["url"]] = item
    return found


def load_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {}
    return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))


def load_url_overrides() -> dict:
    if not URL_OVERRIDES_PATH.exists():
        return {}
    return json.loads(URL_OVERRIDES_PATH.read_text(encoding="utf-8"))


def merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = merge(merged[key], value) if key in merged else value
        return merged
    return override


def exclusive(values: list[str]) -> list[str]:
    ordered = []
    for value in values:
        if value not in ordered:
            ordered.append(value)
    if "unknown" in ordered and len(ordered) > 1:
        ordered = [value for value in ordered if value != "unknown"]
    return ordered or ["unknown"]


def sentences(text: str) -> list[str]:
    rough = re.split(r"(?<=[.!?])\s+|\s{2,}|\s\|\s", text)
    parts = []
    for part in rough:
        part = part.strip()
        if not part:
            continue
        if len(part) <= 420:
            parts.append(part)
            continue
        for start in range(0, len(part), 320):
            chunk = part[start : start + 420].strip()
            if chunk:
                parts.append(chunk)
    return parts


def first_sentence(text: str, needles: tuple[str, ...] | list[str], extra: str | None = None) -> str | None:
    for sentence in sentences(text):
        lowered = sentence.lower()
        if any(needle in lowered for needle in needles) and (extra is None or re.search(extra, lowered)):
            return sentence[:500]
    return None


def content_quality(evidence: dict | None) -> str:
    if not evidence:
        return "not_fetched"
    if evidence.get("challenge"):
        return "challenge"
    status = evidence.get("http_status")
    text = (evidence.get("text_excerpt") or "").lower()
    length = evidence.get("text_length") or 0
    # A short 403/429 body is an access observation, not a workflow page.
    if status in {401, 403, 429, 503} and (
        length < 1200 or "access denied" in text or "been blocked" in text or "just a moment" in text
    ):
        return "error"
    if evidence.get("non_html"):
        return "non_html"
    if evidence.get("error") and length < 400:
        return "error"
    if length < 400:
        return "empty_shell"
    return "readable"


REQUEST_FIELD_RE = re.compile(
    r"url|profile|e-?mail|name|phone|address|city|state|zip|opt|privacy|message|comment|request|record|birth|ssn",
    re.I,
)
IGNORE_FIELD_RE = re.compile(
    r"^(q|query|s|search|keywords|giraffe|what_doing|what_wrong|cx|btng)$",
    re.I,
)


SEARCH_ACTION_RE = re.compile(r"/(search|people|reverse)(/|$)", re.I)
SEARCH_FIELD_RE = re.compile(r"search|lookup", re.I)


def _field_blob(field: dict) -> str:
    return " ".join(
        part
        for part in (
            field.get("name"),
            field.get("id"),
            field.get("placeholder"),
            field.get("aria_label"),
            field.get("type"),
        )
        if part
    )


def request_forms(evidence: dict) -> list[dict]:
    """Keep forms that look like a request or login. Drop site search and feedback widgets."""
    selected = []
    for form in evidence.get("forms") or []:
        action = form.get("action") or ""
        action_path = urlparse(action).path if "://" in action else action
        fields = []
        for field in form.get("fields") or []:
            label = field.get("name") or field.get("id") or ""
            blob = _field_blob(field)
            if field.get("type") != "password" and (
                IGNORE_FIELD_RE.match(label) or SEARCH_FIELD_RE.search(label)
            ):
                continue
            if REQUEST_FIELD_RE.search(blob) or field.get("type") == "password":
                fields.append(field)
        if not fields:
            continue
        # A people/business/phone search box is not a privacy request, even when
        # the placeholder says "name" or "phone".
        if SEARCH_ACTION_RE.search(action_path) and not any(
            re.search(r"e-?mail|message|comment|request|opt|password", _field_blob(field), re.I)
            for field in fields
        ):
            continue
        copied = dict(form)
        copied["fields"] = fields
        selected.append(copied)
    return selected


def family_hints(name: str) -> list[str]:
    return [hint.strip() for hint in re.findall(r"\(([^)]+)\)", name) if hint.strip()]


def portal_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.strip("/") or "root"
    slug = re.sub(r"[^a-z0-9]+", "-", f"{host}-{path}".lower()).strip("-")
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    return "portal-" + slug[:70].strip("-") + "-" + digest


def preferred_email(emails: list[str]) -> str | None:
    if not emails:
        return None
    prefixes = (
        "privacy",
        "dpo",
        "dataprivacy",
        "consumer",
        "compliance",
        "optout",
        "opt-out",
        "removal",
        "legal",
        "support",
    )
    for prefix in prefixes:
        for email in emails:
            if email.startswith(prefix):
                return email
    return emails[0]


def request_types(text: str) -> tuple[list[str], list[str]]:
    lowered = text.lower()
    found = []
    quotes = []
    for name, needles in REQUEST_PATTERNS.items():
        hit = next((needle for needle in needles if needle in lowered), None)
        if not hit:
            continue
        found.append(name)
        sentence = first_sentence(text, (hit,))
        if sentence:
            quotes.append(f"{name}: {sentence}")
    if (
        re.search(r"profile url|url of your profile|url of the profile", lowered)
        and re.search(r"\b(opt|remov|suppress)\b", lowered)
        and "public_listing_suppression" not in found
    ):
        found.append("public_listing_suppression")
        sentence = first_sentence(text, ("profile url", "url of your profile", "url of the profile"))
        if sentence:
            quotes.append(f"public_listing_suppression: {sentence}")
    head = lowered[:500]
    if "open register" in lowered and "opt out" in lowered and "other" not in found:
        found.append("other")
        sentence = first_sentence(text, ("open register",))
        if sentence:
            quotes.append(f"other: {sentence}")
    if "national do not call" in head and "marketing_opt_out" not in found:
        found.append("marketing_opt_out")
        sentence = first_sentence(text, ("do not call", "telemarketing"))
        if sentence:
            quotes.append(f"marketing_opt_out: {sentence}")
    return exclusive(found), quotes


def captcha_info(evidence: dict, text: str) -> dict:
    markers = [marker for marker in (evidence.get("captcha_markers") or []) if marker not in {"captcha", "recaptcha"}]
    sentence = first_sentence(text, ("captcha", "recaptcha", "hcaptcha", "turnstile"))
    present = None
    requirement = "unknown"
    provider = None
    captcha_type = None
    if any(marker in markers for marker in ("g-recaptcha",)) or (sentence and "recaptcha" in sentence.lower()):
        present = True
        provider = "google-recaptcha"
        captcha_type = "recaptcha"
        requirement = "conditionally_observed"
    elif any(marker in markers for marker in ("hcaptcha", "h-captcha")):
        present = True
        provider = "hcaptcha"
        captcha_type = "hcaptcha"
        requirement = "conditionally_observed"
    elif any(marker in markers for marker in ("cf-turnstile", "turnstile")):
        present = True
        provider = "cloudflare-turnstile"
        captcha_type = "turnstile"
        requirement = "conditionally_observed"
    elif any(marker in markers for marker in ("funcaptcha", "arkoselabs")):
        present = True
        provider = "arkose"
        captcha_type = "funcaptcha"
        requirement = "conditionally_observed"
    elif sentence:
        present = True
        provider = "unspecified"
        captcha_type = "unspecified"
        requirement = "conditionally_observed"
    elif evidence.get("forms") and content_quality(evidence) == "readable":
        present = False
        requirement = "unknown"
    if sentence and re.search(r"\b(must|required|complete the captcha)\b", sentence.lower()):
        requirement = "always_required"
    stage = None
    if present and sentence:
        stage = "mentioned_on_inspected_page"
    return {
        "captcha_present": present,
        "captcha_type": captcha_type,
        "captcha_provider": provider,
        "captcha_stage": stage,
        "captcha_requirement": requirement,
        "captcha_sentence": sentence,
    }


def geography_and_eligibility(text: str) -> tuple[list[str], list[str]]:
    coverage = []
    eligibility = []
    checks = [
        ("US", ("throughout the united states", "nationwide", "any us state", "residents of any u.s", "residents of any us", "regardless of state of residence", "us consumer")),
        ("US-CA", ("california resident", "california residents")),
        ("CA", ("across canada", "canadian resident", "in canada")),
        ("GB", ("england, scotland or wales", "united kingdom", "if you live in the uk")),
        ("EU", ("european economic area", "in the european union")),
    ]
    lowered = text.lower()
    for code, needles in checks:
        if any(needle in lowered for needle in needles):
            coverage.append(code)
            sentence = first_sentence(text, needles)
            if sentence:
                eligibility.append(sentence[:400])
    if not coverage:
        coverage = ["unknown"]
    if not eligibility:
        eligibility = ["unknown"]
    return exclusive(coverage) if coverage != ["unknown"] else ["unknown"], eligibility[:4]


def submission_methods(evidence: dict, text: str) -> list[str]:
    methods = []
    lowered = text.lower()
    if request_forms(evidence):
        methods.append("web_form")
    host = (evidence.get("final_url") or evidence.get("url") or "").lower()
    if any(token in host for token in ("onetrust.com", "saymine.io", "trustarc.", "privacyportal")):
        methods.append("web_form")
    if first_sentence(text, ("email us", "email you", "contact us at", "e-mail us", "send an email", "by email")) or (
        preferred_email(evidence.get("emails") or []) and "privacy request" in lowered
    ):
        methods.append("email")
    if re.search(r"\b(call us|by telephone|by phone|telephone number|toll-free|call 1-|call us at)\b", lowered) and re.search(
        r"\d{3}[).\-\s]+\d{3}", text
    ):
        methods.append("phone")
    if "electoral registration office" in lowered or "register to vote service" in lowered:
        methods.append("other")
    if re.search(r"\b(submit online|online request form)\b", lowered):
        methods.append("web_form")
    if re.search(r"\b(mail your request|print\s*&\s*mail|by mail|submit your request by mail|mailing address)\b", lowered):
        methods.append("postal_mail")
    return exclusive(methods)


def field_lists(evidence: dict) -> tuple[list[str], list[str], str, bool | None]:
    forms = request_forms(evidence)
    required, optional = [], []
    profile = None
    if not forms:
        observation = "non_html" if evidence.get("non_html") else "no_form_in_html"
        return [], [], observation, None
    for form in forms:
        for field in form.get("fields") or []:
            label = " ".join(
                part
                for part in (
                    field.get("name"),
                    field.get("id"),
                    field.get("aria_label"),
                    field.get("placeholder"),
                    field.get("type"),
                )
                if part
            ).strip()
            if not label:
                label = field.get("tag") or "unnamed_field"
            target = required if field.get("required") else optional
            if label not in target and label not in required and label not in optional:
                target.append(label[:160])
            blob = label.lower()
            if re.search(r"\b(url|profile|record)\b", blob):
                profile = True
    if profile is None:
        profile = False
    return required[:30], optional[:30], "form_parsed", profile


def build_steps(evidence: dict, text: str, email_sentence: str | None, ack_sentence: str | None) -> list[dict]:
    steps = []
    order = 1
    for redirect in evidence.get("redirects") or []:
        steps.append(
            {
                "order": order,
                "action": "HTTP redirect observed while fetching the catalog URL.",
                "observed": True,
                "source": "http_redirect",
                "evidence": redirect,
            }
        )
        order += 1
    parsed_forms = request_forms(evidence)
    if parsed_forms:
        names = ", ".join(
            field.get("name") or field.get("id") or field.get("placeholder") or field.get("type") or "field"
            for form in parsed_forms
            for field in form.get("fields") or []
        )[:300]
        steps.append(
            {
                "order": order,
                "action": "A form is present in the fetched HTML. It was not submitted.",
                "observed": True,
                "source": "page_form",
                "evidence": f"Observed input names or labels: {names}",
            }
        )
        order += 1
    elif (evidence.get("text_length") or 0) >= 180:
        steps.append(
            {
                "order": order,
                "action": "Fetched page text was inspected. No submittable form fields were parsed from the HTML.",
                "observed": True,
                "source": "page_instructions",
                "evidence": (evidence.get("title") or "Untitled page")[:240],
            }
        )
        order += 1
    if email_sentence:
        steps.append(
            {
                "order": order,
                "action": "Page instructions describe an email confirmation step. The mailbox was not contacted.",
                "observed": False,
                "source": "page_instructions",
                "evidence": email_sentence[:500],
            }
        )
        order += 1
    if ack_sentence:
        steps.append(
            {
                "order": order,
                "action": "Page instructions describe an acknowledgement. None was received in this research.",
                "observed": False,
                "source": "page_instructions",
                "evidence": ack_sentence[:500],
            }
        )
    if not steps:
        return [
            {
                "order": 1,
                "action": "unknown",
                "observed": False,
                "source": "not_inspected",
                "evidence": "Request steps were not inspected.",
            }
        ]
    return steps


def classify_entry(url: str, text: str, title: str, types: list[str]) -> tuple[str, str | None]:
    blob = f"{title} {text[:1500]}".lower()
    host = urlparse(url).netloc.lower()
    if "transferred by court order" in blob or "this domain has been transferred" in blob:
        return "other", "Fetched page says the domain was transferred by court order."
    if "people search" in blob and (
        "public_listing_suppression" in types or "opt out" in blob or "opt-out" in blob or "removal" in blob
    ):
        return "people_search_site", "Fetched page describes a people-search opt-out or listing."
    cra_hosts = (
        "equifax.",
        "experian.",
        "transunion.",
        "innovis.com",
        "chexsystems.com",
        "optoutprescreen.com",
        "lexisnexis.com",
        "nctue.com",
        "sagestreamllc.com",
        "clarityservices.com",
        "microbilt.com",
    )
    if any(token in host for token in cra_hosts) and any(
        token in blob for token in ("opt", "credit", "privacy", "fcra", "prescreen")
    ):
        return "consumer_reporting_agency", "Official consumer-reporting host and the fetched page discusses a consumer request."
    if "we are a consumer reporting agency" in blob or "is a consumer reporting agency" in blob:
        return "consumer_reporting_agency", "Fetched page describes the operator as a consumer reporting agency."
    if "we are a data broker" in blob or "registered data broker" in blob:
        return "data_broker", "Fetched page describes the operator as a data broker."
    official = {
        "donotcall.gov": ("regulator_guidance", "Official National Do Not Call Registry host."),
        "lnnte-dncl.gc.ca": ("regulator_guidance", "Official Canadian National Do Not Call List host."),
        "privacy.ca.gov": ("regulator_guidance", "Official California Privacy Protection Agency DROP page."),
        "gov.uk": ("public_records_source", "Official GOV.UK electoral-register guidance."),
        "youradchoices.ca": ("marketing_choice_service", "Digital Advertising Alliance of Canada choice tool."),
        "youronlinechoices.com": ("marketing_choice_service", "European Interactive Digital Advertising Alliance choice tool."),
        "youradchoices.com": ("marketing_choice_service", "Digital Advertising Alliance control page."),
        "aboutads.info": ("marketing_choice_service", "DAA aboutads.info choice page."),
        "dmachoice.org": ("marketing_choice_service", "ANA DMAchoice mail-preference service."),
        "adsrvr.org": ("marketing_choice_service", "The Trade Desk data-access / advertising-choice page."),
        "thenai.org": ("marketing_choice_service", "Network Advertising Initiative opt-out instructions."),
    }
    for needle, (entry_type, evidence) in official.items():
        if needle in host and content_supports_official(blob, needle):
            return entry_type, evidence
    return "unknown", None


def content_supports_official(blob: str, needle: str) -> bool:
    if needle == "gov.uk":
        return "electoral" in blob or "open register" in blob
    if needle == "privacy.ca.gov":
        return "drop" in blob or "data broker" in blob
    if needle == "adsrvr.org":
        return "trade desk" in blob or "opt-out" in blob or "personal information" in blob
    return True


def workflow_kind(text: str, entry_type: str, methods: list[str], types: list[str], evidence: dict) -> str:
    lowered = text.lower()
    if "transferred by court order" in lowered or "this domain has been transferred" in lowered:
        return "defunct_or_unavailable"
    if entry_type == "marketing_choice_service":
        if "cookie" in lowered or "browser" in lowered or "interest-based" in lowered:
            return "cookie_or_device_choice"
        return "government_or_industry_choice"
    if entry_type == "regulator_guidance":
        return "government_or_industry_choice"
    if entry_type == "public_records_source" and "opt out" in lowered:
        return "government_or_industry_choice"
    if "public_listing_suppression" in types:
        return "public_listing_form"
    host = (evidence.get("final_url") or evidence.get("url") or "").lower()
    if any(token in host for token in ("onetrust.com", "saymine.io", "trustarc.", "transcend.io", "privacyportal")):
        return "dsar_portal"
    if "web_form" in methods and any(
        phrase in lowered for phrase in ("do not sell", "privacy request", "data subject")
    ):
        return "dsar_portal"
    if "privacy policy" in lowered or "privacy notice" in lowered:
        return "privacy_policy_only"
    return "unknown"


def direct_access(evidence: dict | None, quality: str) -> dict:
    if not evidence:
        return {
            "method": "not_fetched",
            "observed_http_status": None,
            "final_url": None,
            "content_quality": "not_fetched",
            "proxy_required": None,
            "proxy_evidence": "No fetch was attempted for this URL in the recorded evidence log.",
            "user_agent": None,
            "notes": "Unresearched URL.",
        }
    status = evidence.get("http_status")
    if quality == "readable":
        proxy_required = False
        proxy_evidence = (
            "A direct HTTP GET from the research environment returned readable page text without a proxy."
        )
    else:
        proxy_required = None
        proxy_evidence = (
            "Proxy access was not tested. A 403, 429, timeout, or bot-wall response does not show that a residential proxy is required."
        )
    notes = "Single automated GET. No form was submitted."
    if quality == "challenge":
        notes = "The response was an anti-bot interstitial. The workflow page behind it was not inspected."
    elif status in {403, 429}:
        notes = f"HTTP {status} is an access observation, not proof that the URL is dead."
    return {
        "method": "http_get",
        "observed_http_status": status,
        "final_url": evidence.get("final_url"),
        "content_quality": quality,
        "proxy_required": proxy_required,
        "proxy_evidence": proxy_evidence,
        "user_agent": evidence.get("request_user_agent"),
        "notes": notes,
    }


def research_record(row: dict, evidence: dict | None) -> dict:
    notes = row["notes"]
    claims = {
        "notes": notes,
        "http_status_recorded": row["http_status"],
        "sources": row["sources_list"],
        "listed_in_cppa_registry_per_catalog": "cppa" in row["sources_list"],
        "family_hints_from_name": family_hints(row["name"]),
        "note_mentions_captcha": "captcha" in notes.lower(),
        "note_mentions_email": "email" in notes.lower(),
        "note_mentions_account": "account" in notes.lower(),
        "note_mentions_blocked_check": "403" in notes or "429" in notes,
    }
    quality = content_quality(evidence)
    text = (evidence or {}).get("text_excerpt") or ""
    title = (evidence or {}).get("title") or ""
    types, type_quotes = request_types(text) if quality == "readable" else (["unknown"], [])
    entry_type, entry_evidence = (
        classify_entry(row["url"], text, title, types) if quality == "readable" else ("unknown", None)
    )
    methods = submission_methods(evidence, text) if evidence and quality == "readable" else ["unknown"]
    kind = (
        workflow_kind(text, entry_type, methods, types, evidence)
        if evidence and quality == "readable"
        else "unknown"
    )
    if kind == "defunct_or_unavailable":
        research_status = "not_applicable"
    elif quality == "not_fetched":
        research_status = "unresearched"
    elif quality in {"challenge", "error"} or (
        evidence and evidence.get("http_status") in {401, 403, 429, 503} and quality != "readable"
    ):
        research_status = "blocked"
    elif "unknown" not in types and "unknown" not in methods and (
        kind in {"public_listing_form", "dsar_portal", "government_or_industry_choice", "cookie_or_device_choice"}
        or "web_form" in methods
        or (evidence and request_forms(evidence))
    ):
        research_status = "verified"
    else:
        research_status = "partial"

    if quality in {"not_fetched", "challenge", "error"} or research_status == "blocked":
        level = "source_documented"
    elif evidence and request_forms(evidence):
        level = "workflow_partially_inspected"
    elif quality in {"readable", "empty_shell", "non_html"}:
        level = "page_inspected"
    else:
        level = "source_documented"

    required, optional, fields_observation, profile_required = (
        field_lists(evidence) if evidence and quality == "readable" else ([], [], "not_inspected", None)
    )
    if quality == "non_html":
        fields_observation = "non_html"
    email_sentence = first_sentence(
        text,
        ("confirmation email", "confirm your email", "click the link", "verification code", "click this link"),
    ) if quality == "readable" else None
    email_required = None
    email_mechanism = "unknown"
    if email_sentence:
        email_required = True
        lowered = email_sentence.lower()
        has_link = "link" in lowered
        has_code = "code" in lowered
        if has_link and has_code:
            email_mechanism = "both"
        elif has_link:
            email_mechanism = "link"
        elif has_code:
            email_mechanism = "code"
    elif research_status == "not_applicable":
        email_mechanism = "not_applicable"

    phone_sentence = first_sentence(text, ("text message", "sms code", "verification text")) if quality == "readable" else None
    phone_required = True if phone_sentence and re.search(r"\b(code|verif)", phone_sentence.lower()) else None

    id_sentence = first_sentence(
        text,
        (
            "driver's license",
            "driver’s license",
            "drivers license",
            "passport",
            "government-issued",
            "government issued",
            "photo id",
            "identity document",
            "supporting documentation",
            "police report",
            "protective order",
        ),
    ) if quality == "readable" else None
    identity_required = None
    if id_sentence and re.search(r"\b(must|required|need|provide|upload|submit)\b", id_sentence.lower()):
        identity_required = True
    redaction = first_sentence(text, ("redact", "redacted")) if quality == "readable" else None

    agent_sentence = first_sentence(text, ("authorized agent", "authorised agent", "authorized representative")) if quality == "readable" else None
    captcha = captcha_info(evidence, text) if evidence and quality == "readable" else {
        "captcha_present": None,
        "captcha_type": None,
        "captcha_provider": None,
        "captcha_stage": None,
        "captcha_requirement": "unknown",
        "captcha_sentence": None,
    }
    login_sentence = first_sentence(text, ("sign in", "log in", "create an account")) if quality == "readable" else None
    account_required = None
    password_present = any(
        field.get("type") == "password"
        for form in (request_forms(evidence) if evidence else [])
        for field in form.get("fields") or []
    )
    login_required_phrase = bool(
        login_sentence
        and re.search(
            r"\b(log in to|sign in to|create an account to|account is required|must (log|sign) in)\b",
            login_sentence.lower(),
        )
    )
    if password_present or login_required_phrase:
        account_required = True
    elif fields_observation == "form_parsed" and not password_present and not login_sentence:
        account_required = False

    if profile_required is False and quality == "readable" and re.search(
        r"\b(profile url|url of your profile|url of the profile|record id)\b", text.lower()
    ):
        profile_required = True

    processing = None
    if quality == "readable":
        processing = first_sentence(
            text,
            ("business day", "business days", "within", "processed", "processing time"),
            extra=r"\b(\d+\s*([-–]|to)\s*\d+|\d+|two|three)\s*(business\s+)?(hours|days|weeks)\b",
        )
    reappear = None
    if quality == "readable":
        reappear = first_sentence(text, ("reappear", "re-appear", "appear again", "updated records", "new and updated"))
    limit_sentence = first_sentence(
        text, ("each listing", "individually", "one opt-out", "unique url", "abuse")
    ) if quality == "readable" else None
    coverage, eligibility = geography_and_eligibility(text) if quality == "readable" else (["unknown"], ["unknown"])

    ack = first_sentence(text, ("confirmation page", "we have received", "confirmation email", "request has been")) if quality == "readable" else None
    emails = [] if quality != "readable" else (evidence or {}).get("emails") or []
    contact = preferred_email(emails)

    searchable = None
    if quality == "readable" and any(
        phrase in text.lower() for phrase in ("profile url", "your listing", "people search")
    ):
        searchable = True
    exposure = "unknown"
    if kind == "defunct_or_unavailable":
        exposure = "not_applicable"
    elif profile_required is True:
        exposure = "public"
    elif kind in {"cookie_or_device_choice", "government_or_industry_choice"}:
        exposure = "not_applicable"

    search_inputs = ["unknown"]
    blockers = []
    if quality == "challenge":
        blockers.append("bot_wall")
    elif research_status == "blocked":
        blockers.append("inaccessible_page")
    if captcha["captcha_present"] is True:
        blockers.append("captcha")
    if account_required is True:
        blockers.append("login")
    if email_required is True or phone_required is True or identity_required is True:
        blockers.append("verification")

    human = []
    automatable = []
    if quality == "readable":
        automatable.append("Open the published URL and read the page that was fetched.")
    if fields_observation == "form_parsed":
        automatable.append("Locate the parsed form fields. Do not invent additional selectors.")
    if email_required:
        human.append("Complete the email confirmation described on the page.")
    if captcha["captcha_present"] is True:
        human.append("Complete the CAPTCHA observed on the page.")
    if identity_required:
        human.append("Provide the identity document the page requires.")
    if account_required:
        human.append("Sign in or create the account the page requires.")
    if profile_required:
        human.append("Supply the profile URL or record ID the page asks for.")
    if phone_required:
        human.append("Complete the phone or SMS verification described on the page.")

    if research_status in {"unresearched", "blocked"}:
        classification = "unknown"
        class_evidence = "The workflow page was not inspected, so automation was not classified."
    elif research_status == "not_applicable" or kind in {"defunct_or_unavailable", "cookie_or_device_choice"}:
        classification = "not_applicable"
        class_evidence = (
            "The inspected page is a defunct URL, a browser or device advertising choice, or not a broker removal workflow."
            if kind != "government_or_industry_choice"
            else "Inspected page is a government or industry choice tool rather than a broker listing form."
        )
        if kind == "government_or_industry_choice" and "web_form" in methods:
            classification = "assisted_workflow"
            class_evidence = "Inspected government or industry choice flow still requires the person, not a broker-site form adapter."
    elif identity_required or (methods != ["unknown"] and "web_form" not in methods and "official_api" not in methods):
        classification = "manual_workflow"
        class_evidence = "Inspected instructions use email, phone, mail, or identity documents without a parsed self-serve form."
    elif "web_form" in methods and (
        captcha["captcha_present"] is True
        or email_required is True
        or account_required is True
        or profile_required is True
        or phone_required is True
        or captcha["captcha_present"] is None
    ):
        classification = "assisted_workflow"
        class_evidence = "A web form or portal was identified, and at least one human or unknown verification step remains."
    elif (
        "web_form" in methods
        and captcha["captcha_present"] is False
        and email_required is False
        and account_required is False
        and identity_required is not True
        and phone_required is not True
    ):
        classification = "automation_candidate"
        class_evidence = "Parsed form had no observed CAPTCHA, login, identity document, or email-confirmation requirement."
    elif "web_form" in methods:
        classification = "assisted_workflow"
        class_evidence = "A form was identified, but confirmation, CAPTCHA, or later steps are not fully known."
    elif kind == "government_or_industry_choice" and research_status in {"verified", "partial"}:
        classification = "assisted_workflow"
        class_evidence = (
            "Inspected government or industry choice flow. It is not a broker listing adapter, and the remaining steps need the person."
        )
    else:
        classification = "unknown"
        class_evidence = "The fetched page does not establish a submission method that can be classified."

    if kind == "cookie_or_device_choice":
        classification = "not_applicable"
        class_evidence = "Choice is stored in the browser or device. A server-side broker adapter cannot complete it."

    notes_bits = []
    if type_quotes:
        notes_bits.append("Request-type quotes: " + " | ".join(type_quotes[:4]))
    if evidence and evidence.get("platform_markers"):
        notes_bits.append(
            "Platform markers in HTML: " + ", ".join(evidence["platform_markers"]) + ". No API endpoint was inferred."
        )
    if evidence and evidence.get("error"):
        notes_bits.append(f"Fetch error: {evidence['error']}")
    if claims["note_mentions_captcha"] or claims["note_mentions_email"] or claims["note_mentions_account"]:
        notes_bits.append(
            "Catalog notes mention captcha, email, or an account. Those mentions are preserved as catalog claims and were not copied into verified fields unless the fetched page also said so."
        )
    evidence_notes = " ".join(notes_bits) if notes_bits else "No additional page quotes were extracted."

    if research_status == "verified" and level == "workflow_partially_inspected":
        confidence = "high"
    elif research_status in {"verified", "partial"} and quality == "readable":
        confidence = "medium"
    elif research_status in {"partial", "blocked", "not_applicable"}:
        confidence = "low"
    else:
        confidence = "none"

    field_names = (evidence or {}).get("form_field_names") or []
    implementation = "No request was submitted. No API endpoint or CSS selector was invented."
    if field_names:
        implementation += " Observed field names or labels: " + ", ".join(field_names[:20]) + "."
    if evidence and evidence.get("platform_markers"):
        implementation += " Vendor platform markers: " + ", ".join(evidence["platform_markers"]) + "."

    homepage = (evidence or {}).get("official_homepage_candidate") if quality == "readable" else None
    privacy_link = None
    if quality == "readable":
        for link in (evidence or {}).get("privacy_policy_links") or []:
            if link.rstrip("/") != row["url"].rstrip("/"):
                privacy_link = link
                break
    request_url = (evidence or {}).get("final_url") if quality in {"readable", "empty_shell", "non_html"} else None

    distinct = None
    distinct_notes = None
    if quality == "readable" and len([item for item in types if item != "unknown"]) >= 2:
        contrast = first_sentence(text, ("difference", "instead of", "does not affect", "separately", "not the same"))
        if contrast:
            distinct = True
            distinct_notes = contrast[:500]

    steps = build_steps(evidence, text, email_sentence, ack) if evidence and quality in {"readable", "non_html"} else [
        {
            "order": 1,
            "action": "unknown",
            "observed": False,
            "source": "not_inspected",
            "evidence": "Request steps were not inspected.",
        }
    ]

    searchable_inputs = search_inputs
    return {
        "catalog_claims": claims,
        "identity": {
            "name": row["name"],
            "domains": [row["domain"]],
            "category": row["category"],
            "entry_type": entry_type,
            "entry_type_evidence": entry_evidence,
            "parent_company": None,
            "sister_brands": [],
            "shared_portal_group_id": None,
            "workflow_family_id": None,
            "single_request_covers_multiple_brands": None,
            "single_request_coverage_evidence": None,
            "geographic_coverage": coverage,
            "documented_request_eligibility": eligibility,
            "supported_request_types": types,
        },
        "discovery": {
            "official_homepage": homepage,
            "privacy_policy_url": privacy_link,
            "request_url": request_url,
            "privacy_contact_email": contact,
            "privacy_contact_emails": emails[:8],
            "public_profiles_searchable": searchable,
            "supported_search_inputs": searchable_inputs,
            "search_requires_login": None,
            "search_requires_payment": None,
            "search_requires_captcha": None,
            "profile_identification": (
                "Page asks for a profile URL or record identifier."
                if profile_required
                else None
            ),
            "exposure_check": exposure,
        },
        "request_workflow": {
            "submission_methods": methods,
            "steps": steps,
            "required_fields": required,
            "optional_fields": optional,
            "fields_observation": fields_observation,
            "profile_url_or_record_id_required": profile_required,
            "account_login_required": account_required,
            "email_verification_required": email_required,
            "email_verification_mechanism": email_mechanism,
            "phone_sms_verification_required": phone_required,
            "identity_document_required": identity_required,
            "identity_document_redaction": redaction,
            "authorized_agent_requirements": agent_sentence,
            "captcha_present": captcha["captcha_present"],
            "captcha_type": captcha["captcha_type"],
            "captcha_provider": captcha["captcha_provider"],
            "captcha_stage": captcha["captcha_stage"],
            "captcha_requirement": captcha["captcha_requirement"],
            "documented_processing_time": processing,
            "observed_turnaround": None,
            "published_request_limits": limit_sentence,
            "automation_restrictions": first_sentence(text, ("abuse", "automated", "scraping", "robots"))
            if quality == "readable"
            else None,
        },
        "automation": {
            "classification": classification,
            "classification_evidence": class_evidence,
            "automatable_steps": automatable,
            "human_steps": human,
            "observed_blockers": blockers,
            "direct_access": direct_access(evidence, quality),
            "implementation_notes": implementation,
        },
        "verification": {
            "submission_acknowledgement": (
                f"Documented on the page, not received by this research: {ack}" if ack else None
            ),
            "broker_confirmation": None,
            "independent_removal_verification": (
                "Page tells the person to check the public listing again. This research did not check a listing."
                if reappear or profile_required
                else None
            ),
            "distinct_suppression_deletion_marketing_outcomes": distinct,
            "outcome_distinction_notes": distinct_notes,
            "reappearance_or_recurring_checks": reappear,
            "workflow_kind": kind,
            "inspection_channel": "http_get"
            if quality in {"readable", "non_html", "empty_shell"}
            else "not_inspected",
            "researched_at": evidence.get("fetched_at") if evidence else None,
            "evidence_urls": [row["url"]] if evidence and quality != "not_fetched" else [],
            "evidence_notes": evidence_notes,
            "confidence": confidence,
            "research_status": research_status,
            "verification_level": level,
        },
    }


def apply_groups(records: list[dict]) -> tuple[list[dict], list[dict]]:
    by_id = {record["id"]: record for record in records}
    portals = []
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[record["catalog"]["url"]].append(record)
    for url, members in grouped.items():
        if len(members) < 2:
            continue
        group_id = portal_id(url)
        portals.append(
            {
                "group_id": group_id,
                "kind": "shared_request_url",
                "request_url": url,
                "member_ids": [member["id"] for member in members],
                "single_request_covers_multiple_brands": None,
                "evidence": (
                    "These catalog rows publish the same request URL. Sharing a URL shows a shared entry point. "
                    "It does not, by itself, prove that one submission removes every brand."
                ),
                "evidence_basis": "catalog_url",
            }
        )
        for member in members:
            member["identity"]["shared_portal_group_id"] = group_id
            member["identity"]["sister_brands"] = [
                {
                    "entry_id": other["id"],
                    "name": other["catalog"]["name"],
                    "relationship": "same_request_url",
                    "evidence_basis": "catalog_url",
                }
                for other in members
                if other["id"] != member["id"]
            ]

    families = []
    signature_groups: dict[tuple, list[dict]] = defaultdict(list)
    for record in records:
        if record["verification"]["verification_level"] != "workflow_partially_inspected":
            continue
        fields = tuple(
            sorted(
                {
                    re.sub(r"\d+", "#", label).strip()
                    for label in record["request_workflow"]["required_fields"]
                    + record["request_workflow"]["optional_fields"]
                }
            )
        )
        if len(fields) < 2:
            continue
        path = urlparse(record["catalog"]["url"]).path.rstrip("/") or "/"
        signature_groups[(path, fields)].append(record)
    for (path, fields), members in signature_groups.items():
        if len(members) < 3:
            continue
        digest = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "root"
        field_hash = hashlib.sha1("|".join(fields).encode("utf-8")).hexdigest()[:6]
        family_id = f"family-{digest[:60]}-{field_hash}"
        families.append(
            {
                "group_id": family_id,
                "kind": "shared_form_signature",
                "path": path,
                "field_names": list(fields),
                "member_ids": [member["id"] for member in members],
                "single_request_covers_multiple_brands": None,
                "evidence": (
                    f"{len(members)} fetched pages with path {path} exposed the same form structure after digits in field names were removed. "
                    "Treat them as one implementation family. Separate URLs still mean separate submissions unless a page says otherwise."
                ),
                "evidence_basis": "parsed_form_fields",
            }
        )
        for member in members:
            if member["identity"]["workflow_family_id"] is None:
                member["identity"]["workflow_family_id"] = family_id
    return portals, families


def parent_mappings(records: list[dict]) -> dict:
    verified = []
    seen = set()
    for record in records:
        parent = record["identity"]["parent_company"]
        if not parent:
            continue
        key = (parent, record["identity"]["shared_portal_group_id"])
        if key in seen:
            continue
        seen.add(key)
        members = [
            other["id"]
            for other in records
            if other["identity"]["parent_company"] == parent
            and other["identity"]["shared_portal_group_id"] == record["identity"]["shared_portal_group_id"]
        ]
        verified.append(
            {
                "parent_company": parent,
                "shared_portal_group_id": record["identity"]["shared_portal_group_id"],
                "member_ids": members,
                "evidence": record["identity"]["single_request_coverage_evidence"]
                or record["identity"]["entry_type_evidence"],
            }
        )
    leads = defaultdict(list)
    for record in records:
        for hint in record["catalog_claims"]["family_hints_from_name"]:
            leads[hint].append({"entry_id": record["id"], "name": record["catalog"]["name"]})
    catalog_leads = [
        {
            "hint": hint,
            "members": members,
            "evidence_basis": "catalog_name",
            "note": "Parenthetical text in the catalog name is a lead. It is not independent verification of ownership.",
        }
        for hint, members in sorted(leads.items())
        if len(members) >= 2
    ]
    return {"verified": verified, "catalog_leads_unverified": catalog_leads}


def progress_report(records: list[dict], portals: list[dict], families: list[dict]) -> dict:
    def count(field_path: str) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for record in records:
            value = record
            for part in field_path.split("."):
                value = value[part]
            counts[value] += 1
        return dict(sorted(counts.items()))

    return {
        "dataset_version": DATASET_VERSION,
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "requests_submitted": False,
        "catalog_entries": len(records),
        "unique_request_urls": len({record["catalog"]["url"] for record in records}),
        "research_status": count("verification.research_status"),
        "verification_level": count("verification.verification_level"),
        "automation_classification": count("automation.classification"),
        "entry_type": count("identity.entry_type"),
        "workflow_kind": count("verification.workflow_kind"),
        "content_quality": count("automation.direct_access.content_quality"),
        "shared_request_url_groups": len(portals),
        "rows_in_shared_request_url_groups": sum(len(group["member_ids"]) for group in portals),
        "workflow_families": len(families),
        "rows_in_workflow_families": sum(len(group["member_ids"]) for group in families),
        "end_to_end_tested": sum(
            1 for record in records if record["verification"]["verification_level"] == "end_to_end_tested"
        ),
        "observed_turnaround_recorded": sum(
            1 for record in records if record["request_workflow"]["observed_turnaround"] is not None
        ),
    }


def render_progress_md(report: dict) -> str:
    def lines(title: str, mapping: dict) -> str:
        body = "\n".join(f"| `{key}` | {value} |" for key, value in mapping.items())
        return f"## {title}\n\n| Value | Records |\n| --- | ---: |\n{body}\n"

    return "\n".join(
        [
            "# Research progress",
            "",
            f"Dataset version `{report['dataset_version']}`, schema `{report['schema_version']}`, generated {report['generated_at']}.",
            "",
            f"Catalog entries: **{report['catalog_entries']}**. Unique request URLs: **{report['unique_request_urls']}**.",
            f"Shared URL groups: **{report['shared_request_url_groups']}** covering **{report['rows_in_shared_request_url_groups']}** rows.",
            f"Workflow families with the same parsed form: **{report['workflow_families']}** covering **{report['rows_in_workflow_families']}** rows.",
            "",
            "No privacy request was submitted. `end_to_end_tested` is "
            f"{report['end_to_end_tested']}. Observed turnaround values recorded: {report['observed_turnaround_recorded']}.",
            "",
            "`not_applicable` means the inspected page is not a live consumer request workflow (for example a seized or defunct opt-out URL). "
            "Government registries and industry choice tools stay under their own entry types. "
            "Their automation classification is separate and is often `not_applicable` when the choice lives in a browser.",
            "",
            lines("Research status", report["research_status"]),
            lines("Verification level", report["verification_level"]),
            lines("Automation classification", report["automation_classification"]),
            lines("Entry type", report["entry_type"]),
            lines("Workflow kind", report["workflow_kind"]),
            lines("Fetch content quality", report["content_quality"]),
            "Catalog notes, including older HTTP statuses and open-dataset difficulty labels, are preserved on each record and are not counted as verified workflow facts.",
            "",
        ]
    )


def main() -> None:
    rows = assign_ids(load_catalog())
    evidence = load_evidence()
    overrides = load_overrides()
    url_overrides = load_url_overrides()
    records = []
    for row in rows:
        built = research_record(row, evidence.get(row["url"]))
        record = {
            "id": row["id"],
            "catalog": {
                "name": row["name"],
                "category": row["category"],
                "url": row["url"],
                "notes": row["notes"],
                "domain": row["domain"],
                "http_status": row["http_status"],
                "sources": row["sources_list"],
            },
        }
        record.update(built)
        if row["url"] in url_overrides:
            record = merge(record, url_overrides[row["url"]])
        if row["id"] in overrides:
            record = merge(record, overrides[row["id"]])
        record["id"] = row["id"]
        records.append(record)

    portals, families = apply_groups(records)
    # Grouping fills same-URL sisters. Re-apply reviewed identity facts after that.
    identity_keys = (
        "entry_type",
        "entry_type_evidence",
        "parent_company",
        "single_request_covers_multiple_brands",
        "single_request_coverage_evidence",
        "workflow_family_id",
        "shared_portal_group_id",
        "geographic_coverage",
        "documented_request_eligibility",
        "supported_request_types",
    )
    for row in rows:
        combined = merge(url_overrides.get(row["url"]) or {}, overrides.get(row["id"]) or {})
        identity = combined.get("identity") or {}
        record = next(item for item in records if item["id"] == row["id"])
        for key in identity_keys:
            if key in identity:
                record["identity"][key] = identity[key]
        for extra in identity.get("sister_brands") or []:
            if extra["entry_id"] not in {item["entry_id"] for item in record["identity"]["sister_brands"]}:
                record["identity"]["sister_brands"].append(extra)

    parents = parent_mappings(records)
    report = progress_report(records, portals, families)
    dataset = {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "catalog_path": "opt-outs.csv",
        "catalog_row_count": len(rows),
        "catalog_check_date": "2026-10-01",
        "research_generated_at": utc_now(),
        "requests_submitted": False,
        "id_join": "records[].id matches data/v1/catalog-ids.csv. records[].catalog.name plus records[].catalog.url matches opt-outs.csv. The catalog CSV columns are unchanged.",
        "methodology": (
            "Each catalog URL was fetched at most a few times with timeouts, per-host delay, and a single retry for 429/503 or network errors. "
            "403 responses were not retried. Forms were not submitted. Email was not sent. Accounts, identity documents, and SMS were not used. "
            "CAPTCHAs were not solved. Fields stay null or unknown unless the fetched page or a cited manual inspection supports them. "
            "Catalog notes remain under catalog_claims."
        ),
        "records": records,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "broker-opt-outs.enriched.json").write_text(
        json.dumps(dataset, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (OUT_DIR / "shared-portals.json").write_text(
        json.dumps({"shared_request_urls": portals, "workflow_families": families}, indent=2) + "\n",
        encoding="utf-8",
    )
    (OUT_DIR / "parent-companies.json").write_text(
        json.dumps(parents, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (OUT_DIR / "research-progress.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    (ROOT / "docs" / "research-progress.md").write_text(render_progress_md(report), encoding="utf-8")
    id_lines = ["id,name,category,domain,url"]
    for record in records:
        name = record["catalog"]["name"].replace('"', '""')
        url = record["catalog"]["url"].replace('"', '""')
        id_lines.append(
            f'{record["id"]},"{name}",{record["catalog"]["category"]},{record["catalog"]["domain"]},"{url}"'
        )
    (OUT_DIR / "catalog-ids.csv").write_text("\n".join(id_lines) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
