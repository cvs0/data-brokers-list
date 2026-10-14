#!/usr/bin/env python3
"""Scan every catalog broker's public pages into a v3 profile.

One record per catalog id, including brokers whose pages are dead, blocked,
or disallowed. Null means that fact was not on a page we could read.

Reads the homepage, the catalog opt-out URL, and a few same-site privacy,
contact, and legal pages already mapped in data/v1/broker-maps.json.
robots.txt is honored. Forms are not submitted. CAPTCHAs are not solved.
People-search listing URLs are not stored. Image fields are links only.

--render-missing opens, once in a browser, lookup homepages the static fetch
never read, and catalog URLs that came back blocked or failed. robots.txt is
still honored. A page that stays on a challenge is left unchanged.
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
import asyncio
import csv
import json
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from jsonschema import Draft202012Validator

from catalog import ROOT, assign_ids, load_catalog
from map_brokers import (
    EMAIL_RE,
    MappingFetcher,
    catalog_domain,
    classify_role,
    entry_status,
    filter_email,
    is_parked,
    is_profile_url,
    is_soft_404,
    load_parents,
    norm_url,
    utc_now,
)
from rescrape_fields import CHALLENGE, JURIS
from v2_common import canonical_field, processing_days

LOG_PATH = ROOT / "research" / "evidence" / "v3.jsonl"
OUT_PATH = ROOT / "data" / "v3" / "brokers.json"
CSV_PATH = ROOT / "data" / "v3" / "brokers.csv"
DOC_PATH = ROOT / "docs" / "v3.md"
SCHEMA_PATH = ROOT / "data" / "schema" / "broker-v3.schema.json"
MAPS_PATH = ROOT / "data" / "v1" / "broker-maps.json"
MAX_PAGES = 8
WORKERS = 8

PORTALS = (
    ("onetrust", "onetrust.com"),
    ("trustarc", "trustarc.com"),
    ("datagrail", "datagrail.com"),
    ("transcend", "transcend.io"),
    ("securiti", "securiti.ai"),
    ("ketch", "ketch.com"),
    ("osano", "osano.com"),
    ("jotform", "jotform.com"),
)
DIRECTORY_PREFIXES = ("people", "person", "phone", "search", "name", "email", "address", "background")
RENDER_CATEGORIES = {"people-search", "background-checks", "public-records"}
_QUERY_NAME = re.compile(r"^(q|query|search|keyword|keywords|search[-_]?keyword|k)$", re.I)
_OPT_OUT_BITS = ("opt-out", "optout", "privacy", "contact", "remove", "suppress", "dsar", "deletion", "ccpa", "do-not-sell", "donotsell")
_FACE_RE = re.compile(r"face|photo|image", re.I)
DATA_CATEGORIES = (
    ("identifiers", r"\b(name|email address|phone number|postal address)\b"),
    ("precise_location", r"\b(precise geolocation|precise location|gps)\b"),
    ("commercial", r"\b(purchase history|commercial information|transaction)\b"),
    ("internet_activity", r"\b(browsing history|internet activity|cookie)\b"),
    ("employment", r"\b(employment|professional information|job title)\b"),
    ("education", r"\b(education information|student)\b"),
    ("inferences", r"\b(inferences|profiles? reflecting)\b"),
    ("sensitive", r"\b(social security|biometric|health information|racial or ethnic)\b"),
)
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}")
PO_RE = re.compile(r"P\.?\s*O\.?\s*Box\s+\d+[^.]{0,80}", re.I)
AUTO_RE = re.compile(
    r"[^.]{0,80}\b(scrap(?:e|ing)|automated (?:access|means|tool)|robot|crawler|spider)\b[^.]{0,80}\b(prohibit|forbidden|not (?:permitted|allowed)|may not|without (?:our )?consent)\b[^.]*\.?",
    re.I,
)
SUCCESS_RE = re.compile(r".{0,60}\b(request (has been|was) (received|submitted)|we have received your (request|opt-?out))\b.{0,80}", re.I)
PROC_RE = re.compile(
    r"[^.]{0,80}\b(within|in|up to|no later than)\s+(\d{1,3})\s+(business |calendar )?(hours?|days?|weeks?)\b[^.]{0,80}",
    re.I,
)
SITEKEY_RE = re.compile(r"""(?:data-sitekey|data-site-key)\s*=\s*["']([0-9A-Za-z_-]{20,})["']""", re.I)
RENDER_KEY_RE = re.compile(r"""recaptcha/api\.js\?[^"'<>]*render=([0-9A-Za-z_-]{20,})""", re.I)
SKIP_IMG = ("pixel", "spacer", "1x1", "tracking", "analytics", "sprite", "blank.gif", "facebook.com", "twitter.com", "doubleclick")


class Doc(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.lang = ""
        self.text: list[str] = []
        self.metas: list[tuple[str, str]] = []
        self.links: list[tuple[str, str, str]] = []
        self.images: list[dict] = []
        self.forms: list[dict] = []
        self.iframes: list[str] = []
        self.scripts: list[str] = []
        self.json_ld: list[str] = []
        self.anchors: list[tuple[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._in_script = False
        self._script: list[str] = []
        self._script_type = ""
        self._ld = False
        self._a: str | None = None
        self._atext: list[str] = []
        self._form: dict | None = None
        self._select: dict | None = None
        self._in_option = False
        self._opt_value = ""
        self._opt_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"][:16]
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
            if tag == "script":
                self._in_script = True
                self._script = []
                self._script_type = a.get("type", "").lower()
                self._ld = "ld+json" in self._script_type
                if a.get("src"):
                    self.scripts.append(a["src"])
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key:
                self.metas.append((key, a.get("content", "")))
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "")
            if href:
                self.links.append((rel, href, a.get("sizes", "")))
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or ""
            if not src and a.get("srcset"):
                src = a["srcset"].split(",")[0].strip().split(" ")[0]
            blob = " ".join((src, a.get("alt", ""), a.get("class", ""), a.get("id", ""))).lower()
            if src and "logo" in blob:
                self.images.append({"src": src, "kind": "logo", "hint": blob[:80]})
        elif tag == "iframe" and a.get("src"):
            self.iframes.append(a["src"])
        elif tag == "a" and a.get("href"):
            self._a, self._atext = a["href"], []
        elif tag == "form":
            self._form = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(), "inputs": []}
        elif tag == "select" and self._form is not None:
            self._select = {"name": a.get("name") or a.get("id") or "", "type": "select", "required": "required" in a, "autocomplete": a.get("autocomplete") or None, "placeholder": None, "options": []}
        elif tag == "option" and self._select is not None:
            self._close_option()
            self._in_option = True
            self._opt_value = a.get("value") or ""
            self._opt_text = []
        elif tag in ("input", "textarea") and self._form is not None:
            kind = (a.get("type") or "text").lower()
            if kind not in ("submit", "button", "image", "reset"):
                self._form["inputs"].append({
                    "name": a.get("name") or a.get("id") or "",
                    "type": kind,
                    "required": "required" in a,
                    "autocomplete": a.get("autocomplete") or None,
                    "placeholder": (a.get("placeholder") or a.get("aria-label") or None),
                    "options": [],
                })

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
            if tag == "script" and self._in_script:
                body = "".join(self._script)
                if self._ld:
                    self.json_ld.append(body[:200_000])
                elif body:
                    self.scripts.append(body[:100_000])
                self._in_script = False
                self._script = []
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._a is not None:
            self.anchors.append((self._a, " ".join(self._atext).strip()))
            self._a = None
        elif tag == "select" and self._select is not None and self._form is not None:
            self._close_option()
            self._form["inputs"].append(self._select)
            self._select = None
        elif tag == "option":
            self._close_option()
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def handle_data(self, data):
        if self._in_option:
            self._opt_text.append(data.strip())
        if self._in_title:
            self.title += data
        if self._in_script:
            if sum(len(part) for part in self._script) < 120_000:
                self._script.append(data)
            return
        if self._skip:
            return
        self.text.append(data)
        if self._a is not None:
            self._atext.append(data.strip())

    def _close_option(self) -> None:
        if not self._in_option or self._select is None:
            self._in_option = False
            return
        self._in_option = False
        if len(self._select["options"]) >= 320:
            return
        label = option_label(self._opt_value, " ".join(self._opt_text))
        if label and label not in self._select["options"]:
            self._select["options"].append(label[:80])


def option_label(value: str, text: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    text = re.sub(r"\s+", " ", text or "").strip()
    if value and not value.isdigit() and value.lower() not in {"select", "choose", "none"}:
        return value
    if text.lower() in {"", "select", "choose", "select state", "select a state", "choose state"}:
        return ""
    return text or value


def page_state(res: dict, title: str, text: str) -> str:
    if res.get("skipped") == "robots":
        return "robots_disallowed"
    if res.get("error"):
        from map_brokers import is_dns
        return "dead" if is_dns(res["error"]) else "error"
    status = res.get("status")
    blob = f"{title} {text[:3000]}".lower()
    if status in (401, 403, 429, 451, 503) or any(marker in blob for marker in CHALLENGE):
        return "blocked"
    if "human verification" in blob[:800]:
        return "blocked"
    if status in (404, 410) or is_soft_404(title, text):
        return "dead"
    if is_parked(title, text):
        return "parked"
    if isinstance(status, int) and 200 <= status < 400:
        return "alive"
    return "error"


def abs_url(base: str, href: str) -> str | None:
    if not href or href.startswith(("data:", "javascript:", "mailto:", "tel:")):
        return None
    url = urljoin(base, href).split("#", 1)[0]
    if urlparse(url).scheme not in ("http", "https"):
        return None
    return url


def keep_image(url: str) -> bool:
    lowered = url.lower()
    return not any(token in lowered for token in SKIP_IMG)


def walk_ld(node, found: list[dict]) -> None:
    if isinstance(node, list):
        for item in node:
            walk_ld(item, found)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    if any(isinstance(item, str) and item.lower() in ("organization", "website", "corporation") for item in kinds):
        logo = node.get("logo") or node.get("image")
        if isinstance(logo, dict):
            logo = logo.get("url")
        if isinstance(logo, list):
            logo = logo[0] if logo else None
        found.append({"name": node.get("name") if isinstance(node.get("name"), str) else None, "logo": logo if isinstance(logo, str) else None})
    for value in node.values():
        if isinstance(value, (dict, list)):
            walk_ld(value, found)


def field_from_input(raw: dict) -> dict:
    label = " ".join(filter(None, [raw.get("name"), raw.get("placeholder"), raw.get("type")]))
    field = {
        "name": (raw.get("name") or "")[:80],
        "type": (raw.get("type") or "text")[:40],
        "canonical": canonical_field(label),
        "required": bool(raw.get("required")),
        "autocomplete": (raw.get("autocomplete") or None),
        "placeholder": (raw.get("placeholder") or None),
    }
    if raw.get("options"):
        field["options"] = raw["options"][:320]
    return field


def empty_captcha_detail() -> dict:
    return {
        "mode": None,
        "invisible": None,
        "size": None,
        "theme": None,
        "action": None,
        "badge": None,
        "language": None,
        "appearance": None,
        "enterprise": None,
        "api_domain": None,
        "script_path": None,
        "token_field": None,
        "callback": None,
    }


def captcha_widget(html_bits: str) -> dict:
    """Public widget settings from markup. A sentence that names a CAPTCHA is not a widget."""
    blank = {"provider": None, "sitekey": None, "detail": None}
    if not html_bits:
        return blank
    lowered = html_bits.lower()

    def has(*patterns: str) -> bool:
        return any(re.search(pattern, lowered) for pattern in patterns)

    def attr(name: str, allowed: set[str]) -> str | None:
        match = re.search(rf"""data-{name}\s*=\s*["']([a-z0-9_-]{{1,32}})["']""", lowered)
        if not match:
            match = re.search(rf"""[?&]{name}=([a-z0-9_-]{{1,32}})(?:&|["'\s]|$)""", lowered)
        if match and match.group(1) in allowed:
            return match.group(1)
        return None

    hcaptcha = has(r"js\.hcaptcha\.com", r"hcaptcha\.com/1/api\.js", r"\bh-captcha\b", r"data-hcaptcha")
    turnstile = has(r"challenges\.cloudflare\.com/turnstile", r"\bcf-turnstile\b", r"turnstile/v0/api\.js")
    arkose = has(r"arkoselabs\.com", r"funcaptcha\.com", r"\bfuncaptcha\b")
    geetest = has(r"geetest\.com", r"initgeetest")
    friendly = has(r"friendlycaptcha\.com", r"\bfrc-captcha\b")
    mtcaptcha = has(r"mtcaptcha\.com", r"class=[\"'][^\"']*\bmtcaptcha\b")
    datadome = has(r"captcha-delivery\.com")
    perimeterx = has(r"\bpx-captcha\b", r"captcha\.px-cdn\.net")
    enterprise = has(r"recaptcha/enterprise", r"grecaptcha\.enterprise")
    checkbox = has(r"\bg-recaptcha\b", r"recaptcha/api2/")
    api_js = has(r"google\.com/recaptcha/api\.js") or has(r"recaptcha\.net/recaptcha/api\.js")
    v3 = has(
        r"recaptcha/api\.js\?[^\"'<>]*render=(?!explicit\b)[0-9a-z_-]{20,}",
        r"grecaptcha\.execute\s*\(",
    )
    v2 = checkbox or (api_js and not v3)
    provider = None
    if hcaptcha:
        provider = "hcaptcha"
    elif turnstile:
        provider = "turnstile"
    elif arkose:
        provider = "arkose"
    elif geetest:
        provider = "geetest"
    elif friendly:
        provider = "friendly"
    elif mtcaptcha:
        provider = "mtcaptcha"
    elif datadome:
        provider = "datadome"
    elif perimeterx:
        provider = "perimeterx"
    elif checkbox:
        provider = "recaptcha_v2"
    elif enterprise:
        provider = "recaptcha_enterprise"
    elif v3:
        provider = "recaptcha_v3"
    elif v2:
        provider = "recaptcha_v2"
    sitekey = None
    for pattern in (
        r"""data-sitekey\s*=\s*["']([0-9A-Za-z_-]{20,})["']""",
        r"""data-pkey\s*=\s*["']([0-9A-Fa-f-]{20,})["']""",
        r"""["'](?:sitekey|captchaId|gt)["']\s*:\s*["']([0-9A-Za-z_-]{20,})["']""",
        r"""[?&]render=([0-9A-Za-z_-]{20,})""",
        r"""[?&](?:k|sitekey)=([0-9A-Za-z_-]{20,})""",
        r"""\bgt=([0-9a-f]{32})\b""",
    ):
        match = re.search(pattern, html_bits, re.I)
        if match and match.group(1).lower() not in {"explicit", "onload"}:
            sitekey = match.group(1)
            break
    if provider is None and sitekey and has(r"\bcaptcha\b"):
        provider = "custom"
    if provider is None:
        return blank
    detail = empty_captcha_detail()
    detail["size"] = attr("size", {"normal", "compact", "invisible", "flexible"})
    detail["theme"] = attr("theme", {"light", "dark", "auto"})
    detail["badge"] = attr("badge", {"bottomright", "bottomleft", "inline"})
    detail["appearance"] = attr("appearance", {"always", "execute", "interaction-only"})
    action = re.search(r"""data-action\s*=\s*["']([\w:./-]{1,64})["']""", html_bits)
    if not action:
        action = re.search(
            r"""grecaptcha(?:\.enterprise)?\.execute\s*\([\s\S]{0,180}?action\s*:\s*["']([\w:./-]{1,64})["']""",
            html_bits,
            re.I,
        )
    if action:
        detail["action"] = action.group(1)
    callback = re.search(r"""data-callback\s*=\s*["']([A-Za-z_$][\w.$]{0,80})["']""", html_bits)
    if callback:
        detail["callback"] = callback.group(1)
    language = re.search(
        r"""data-(?:hl|lang|language)\s*=\s*["']([a-z]{2}(?:-[A-Za-z]{2})?)["']""",
        html_bits,
        re.I,
    )
    if not language:
        language = re.search(
            r"""(?:recaptcha|hcaptcha|turnstile)[^"'<>]{0,100}?[?&]hl=([a-z]{2}(?:-[A-Za-z]{2})?)""",
            html_bits,
            re.I,
        )
    if language:
        detail["language"] = language.group(1)
    for pattern in (
        r"https?://((?:www\.)?recaptcha\.net)(/[^\"'\s?#]{1,120})",
        r"https?://(www\.google\.com)(/recaptcha/[^\"'\s?#]{1,120})",
        r"https?://(challenges\.cloudflare\.com)(/turnstile/[^\"'\s?#]{1,120})",
        r"https?://(js\.hcaptcha\.com)(/[^\"'\s?#]{1,120})",
        r"https?://([a-z0-9.-]*arkoselabs\.com)(/[^\"'\s?#]{1,120})",
        r"https?://([a-z0-9.-]*funcaptcha\.com)(/[^\"'\s?#]{1,120})",
        r"https?://([a-z0-9.-]*geetest\.com)(/[^\"'\s?#]{1,120})",
        r"https?://([a-z0-9.-]*friendlycaptcha\.com)(/[^\"'\s?#]{1,120})",
        r"https?://([a-z0-9.-]*mtcaptcha\.com)(/[^\"'\s?#]{1,120})",
        r"https?://([a-z0-9.-]*captcha-delivery\.com)(/[^\"'\s?#]{1,120})",
        r"https?://(captcha\.px-cdn\.net)(/[^\"'\s?#]{1,120})",
    ):
        script = re.search(pattern, lowered)
        if script:
            detail["api_domain"] = script.group(1)
            detail["script_path"] = script.group(2)
            break
    token_names = {
        "recaptcha_v2": "g-recaptcha-response",
        "recaptcha_v3": "g-recaptcha-response",
        "recaptcha_enterprise": "g-recaptcha-response",
        "hcaptcha": "h-captcha-response",
        "turnstile": "cf-turnstile-response",
        "friendly": "frc-captcha-solution",
    }
    token = token_names.get(provider or "")
    if token and token in lowered:
        detail["token_field"] = token
    if provider in {"recaptcha_v2", "recaptcha_v3", "recaptcha_enterprise"} or enterprise:
        detail["enterprise"] = bool(enterprise or provider == "recaptcha_enterprise")
    score = provider == "recaptcha_v3" or (provider == "recaptcha_enterprise" and v3 and not checkbox)
    widget_el = checkbox or (hcaptcha and has(r"\bh-captcha\b")) or (turnstile and has(r"\bcf-turnstile\b"))
    if score:
        detail["mode"] = "score"
        detail["invisible"] = True
    elif detail["size"] == "invisible" or detail["appearance"] == "execute":
        detail["mode"] = "invisible"
        detail["invisible"] = True
    elif widget_el and detail["appearance"] != "execute":
        detail["mode"] = "checkbox"
        detail["invisible"] = False
    return {"provider": provider, "sitekey": sitekey, "detail": detail}


def captcha_of(html_bits: str) -> tuple[str | None, str | None]:
    """Widget evidence only. A sentence that mentions a CAPTCHA is not a widget."""
    found = captcha_widget(html_bits)
    return found["provider"], found["sitekey"]


def bot_vendor(headers_blob: str, body: str) -> str | None:
    blob = f"{headers_blob} {body[:1500]}".lower()
    for name, token in (
        ("cloudflare", "cloudflare"),
        ("datadome", "datadome"),
        ("perimeterx", "perimeterx"),
        ("perimeterx", "px-captcha"),
        ("kasada", "kasada"),
        ("radware", "perfdrive"),
        ("radware", "radware"),
    ):
        if token in blob:
            return name
    return None


def portal_of(urls: list[str]) -> dict | None:
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        for vendor, token in PORTALS:
            if token in host or token in url.lower():
                return {"vendor": vendor, "url": url}
    return None


# Off-domain inboxes a fetched page tells people to use for a do-not-sell or removal
# request. filter_email drops these. scan_v4 promotes the same addresses to stated_removal.
STATED_OFF_DOMAIN = {
    "thisnumber.com": {"thisnumber.com@gmail.com"},
}


def keep_stated(domain: str, email: str) -> tuple[str, str] | None:
    cleaned = email.strip().strip(".").lower()
    if cleaned in STATED_OFF_DOMAIN.get(domain, ()):
        return cleaned, "uncertain"
    return None


def analyze(res: dict, domain: str, parents: set[str]) -> dict:
    html = res.get("html") or ""
    doc = Doc()
    if html and "html" in (res.get("ctype") or "text/html").lower() or "<html" in html[:600].lower():
        try:
            doc.feed(html[:900_000])
        except Exception:
            pass
    text = re.sub(r"\s+", " ", " ".join(doc.text)).strip()
    final = res.get("final_url") or res.get("url") or ""
    liveness = page_state(res, doc.title, text)
    images = []
    seen_img: set[str] = set()

    def add_image(href: str, kind: str) -> None:
        url = abs_url(final, href) if final else None
        if not url or not keep_image(url) or url in seen_img or len(images) >= 16:
            return
        seen_img.add(url)
        images.append({"url": url, "kind": kind, "source_page": final})

    for rel, href, _sizes in doc.links:
        if "apple-touch-icon" in rel:
            add_image(href, "apple_touch_icon")
        elif "icon" in rel:
            add_image(href, "icon")
    for key, content in doc.metas:
        if key in ("og:image", "og:image:url") and content:
            add_image(content, "og_image")
        elif key == "twitter:image" and content:
            add_image(content, "twitter_image")
    ld_names = []
    for blob in doc.json_ld:
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            continue
        found: list[dict] = []
        walk_ld(payload, found)
        for item in found:
            if item.get("name"):
                ld_names.append(item["name"][:120])
            if item.get("logo"):
                add_image(item["logo"], "json_ld")
    for image in doc.images:
        add_image(image["src"], "logo")

    forms = []
    for form in doc.forms:
        action = abs_url(final, form["action"]) if form["action"] else final
        forms.append({
            "action": action,
            "method": "post" if form["method"] == "post" else "get",
            "inputs": [
                field_from_input(item)
                for item in form["inputs"]
                if item.get("name") or item.get("placeholder") or item.get("type") in ("select", "file", "search")
            ],
        })
    emails = []
    seen_email: set[str] = set()
    for href, _text in doc.anchors:
        if href.lower().startswith("mailto:"):
            candidate = href[7:].split("?")[0]
            if "%" in candidate or "mailto" in candidate.lower():
                continue
            decision = filter_email(candidate, domain, parents) or keep_stated(domain, candidate)
            if decision and decision[0] not in seen_email:
                seen_email.add(decision[0])
                emails.append({"email": decision[0], "confidence": decision[1], "via": "mailto"})
    for match in EMAIL_RE.finditer(text):
        decision = filter_email(match.group(1), domain, parents) or keep_stated(domain, match.group(1))
        if not decision or "%" in decision[0]:
            continue
        if decision and decision[0] not in seen_email:
            seen_email.add(decision[0])
            emails.append({"email": decision[0], "confidence": decision[1], "via": "text"})
        if len(emails) >= 12:
            break
    phones = []
    for match in PHONE_RE.finditer(text):
        start = max(0, match.start() - 60)
        window = text[start:match.end() + 20].lower()
        if any(token in window for token in ("privacy", "opt", "call", "phone", "toll", "support", "contact")):
            number = re.sub(r"\s+", " ", match.group(0)).strip()
            if number not in phones:
                phones.append(number)
        if len(phones) >= 3:
            break
    postal = None
    po = PO_RE.search(text)
    if po:
        postal = re.sub(r"\s+", " ", po.group(0)).strip()[:180]
    provider, sitekey = captcha_of(html[:400_000] if html else "")
    vendor = bot_vendor("", html or "")
    portal = portal_of(doc.iframes + [href for href, _text in doc.anchors])
    theme = next((content[:32] for key, content in doc.metas if key == "theme-color" and content), None)
    languages = []
    if doc.lang:
        languages.append(doc.lang.split("-")[0].lower())
    consent = None
    script_blob = " ".join(doc.scripts).lower()
    for name, token in (("onetrust", "onetrust"), ("cookiebot", "cookiebot"), ("osano", "osano"), ("trustarc", "trustarc")):
        if token in script_blob:
            consent = name
            break
    csrf = None
    for form in forms:
        for field in form["inputs"]:
            if field["type"] == "hidden" and re.search(r"csrf|token", field["name"], re.I):
                csrf = field["name"][:80]
                break
    return {
        "url": res.get("url"),
        "final_url": res.get("final_url"),
        "status": res.get("status"),
        "error": res.get("error"),
        "skipped": res.get("skipped"),
        "liveness": liveness,
        "title": doc.title.strip()[:140],
        "text": text[:8_000],
        "images": images,
        "forms": forms,
        "emails": emails,
        "phones": phones,
        "postal_address": postal,
        "captcha_provider": provider,
        "captcha_sitekey": sitekey,
        "bot_vendor": vendor,
        "portal": portal,
        "theme_color": theme,
        "languages": languages,
        "legal_name": ld_names[0] if ld_names else None,
        "consent_vendor": consent,
        "csrf_field_name": csrf,
        "anchors": [(abs_url(final, href) or href, text[:80]) for href, text in doc.anchors[:80]],
    }


def host_matches(domain: str, url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host == domain or host.endswith("." + domain)


def _opt_out_page(page_url: str) -> bool:
    parsed = urlparse(page_url)
    path = parsed.path.lower()
    host = (parsed.hostname or "").lower()
    if host.startswith("ccpa.") or any(token in path for token in _OPT_OUT_BITS):
        return True
    return classify_role(page_url, "") in {"opt_out", "privacy", "contact", "legal"}


def _form_has_email(form: dict) -> bool:
    for field in form.get("inputs") or []:
        blob = f"{field.get('placeholder') or ''} {field.get('name') or ''}".lower()
        if (field.get("type") or "") == "email" or field.get("canonical") == "email" or "email" in blob:
            return True
    return False


def _visible_inputs(form: dict) -> list[dict]:
    return [
        field for field in form.get("inputs") or []
        if field.get("type") not in ("hidden", "submit", "button", "image", "reset", "radio", "checkbox")
    ]


def is_search_form(form: dict, page_url: str, text: str = "") -> bool:
    inputs = _visible_inputs(form)
    if not inputs:
        return False
    action_path = urlparse(form.get("action") or "").path.lower()
    opt_out = _opt_out_page(page_url)
    canons = {field["canonical"] for field in inputs if field.get("canonical")}
    nameish = bool(canons & {"first_name", "last_name", "full_name", "phone", "street_address"})
    queryish = any(_QUERY_NAME.match(field.get("name") or "") or field.get("type") == "search" for field in inputs)
    files = [field for field in inputs if field.get("type") == "file"]
    file_hint = " ".join(
        filter(None, [*(field.get("name") or "" for field in files), *(field.get("placeholder") or "" for field in files), text[:1500]])
    )
    clear_action = any(token in action_path for token in ("/search", "/people-search", "/lookup"))
    blob = " ".join(f"{field.get('placeholder') or ''} {field.get('name') or ''}" for field in inputs)
    has_email = any(
        (field.get("type") or "") == "email"
        or field.get("canonical") == "email"
        or "email" in f"{field.get('placeholder') or ''} {field.get('name') or ''}".lower()
        for field in inputs
    )
    if re.search(r"cookie|request for deletion|request for access|do not sell|opt-?out request", blob, re.I):
        return False
    if files and not opt_out and (_FACE_RE.search(file_hint) or clear_action):
        return True
    if opt_out:
        if has_email:
            return False
        personish = nameish or bool(re.search(r"first name|last name|full name|enter a name|\b(phone|address|city)\b", blob, re.I))
        if clear_action and personish:
            return True
        return bool(inputs) and all((field.get("type") or "") in ("tel", "search") for field in inputs)
    if (clear_action and inputs) or nameish:
        return True
    path = urlparse(page_url).path.lower()
    if path in ("", "/") and queryish:
        return True
    if re.search(r"first name|last name|full name|enter a name|your name", blob, re.I):
        return True
    return bool(re.search(r"\bstreet\b|home address|property address|search your address", blob, re.I))


def search_inputs(form: dict) -> list[dict]:
    inputs = []
    for field in form["inputs"]:
        item = {key: value for key, value in field.items() if key != "options" or value}
        if _QUERY_NAME.match(item.get("name") or "") and item.get("canonical") in (None, "email"):
            item["canonical"] = "phone" if item.get("type") == "tel" else "full_name"
        inputs.append(item)
    return inputs


def search_pattern(form: dict) -> str | None:
    names = [field["name"] for field in form["inputs"] if field.get("name") and field["type"] != "hidden"]
    if not form.get("action") or not names:
        return None
    if form["method"] == "get":
        return form["action"] + "?" + "&".join(f"{name}={{{name}}}" for name in names[:8])
    return form["action"]


def removal_kinds(url: str, text: str) -> list[str]:
    blob = f"{url} {text[:4000]}".lower()
    kinds = []
    checks = (
        ("opt_out", ("opt-out", "opt out", "optout", "remove my", "suppression")),
        ("do_not_sell", ("do not sell", "do-not-sell", "share my personal")),
        ("dsar", ("dsar", "access request", "right to know", "right to delete")),
        ("unsubscribe", ("unsubscribe", "email preference")),
    )
    for name, tokens in checks:
        if any(token in blob for token in tokens):
            kinds.append(name)
    return kinds or ["opt_out"]


def clause(text: str) -> str | None:
    match = AUTO_RE.search(text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(0)).strip()[:400]


def pick_logo(images: list[dict]) -> str | None:
    order = ("logo", "json_ld", "apple_touch_icon", "icon", "og_image", "twitter_image")
    for kind in order:
        for image in images:
            if image["kind"] == kind:
                return image["url"]
    return None


def crawl_domain(fetcher: MappingFetcher, domain: str, catalog_urls: list[str], seed_urls: list[str], prefixes: list[str], parents: set[str]) -> dict:
    queue: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str, role: str) -> None:
        if not url or is_profile_url(url):
            return
        parsed = urlparse(url)
        key = f"{parsed.scheme}://{(parsed.hostname or '').lower()}{parsed.path or '/'}"
        if key in seen or len(queue) >= MAX_PAGES:
            return
        seen.add(key)
        queue.append((url, role))

    add(f"https://{domain}/", "home")
    add(f"https://www.{domain}/", "home")
    for url in catalog_urls:
        add(url, "catalog")
    for url in seed_urls:
        add(url, classify_role(url, ""))
    pages = []
    for url, role in queue:
        res = fetcher.get(url)
        parsed = analyze(res, domain, parents)
        parsed["role"] = role
        parsed.pop("anchors", None)
        pages.append(parsed)
    directory = [prefix for prefix in prefixes if prefix.strip("/").split("/")[0].lower() in DIRECTORY_PREFIXES]
    return {"domain": domain, "checked_at": utc_now(), "pages": pages, "directory_prefixes": directory[:12]}


def empty_search(method: str = "unknown") -> dict:
    return {
        "available": None if method == "unknown" else False,
        "method": method,
        "page_url": None,
        "form_action": None,
        "inputs": [],
        "url_pattern": None,
        "directory_prefixes": [],
        "paywall": None,
        "login_wall": None,
    }


def build_record(row: dict, site: dict | None) -> dict:
    domain = catalog_domain(row)
    pages = (site or {}).get("pages") or []
    homes = [page for page in pages if page.get("role") == "home"]
    home = next((page for page in homes if page.get("liveness") == "alive"), homes[0] if homes else None)
    catalog = next((page for page in pages if norm_url(page.get("url") or "") == norm_url(row["url"])), None)
    readable = [page for page in pages if page.get("liveness") == "alive" or page.get("browser_read")]
    if not site:
        liveness, detail = "not_checked", "no crawl result in this log"
    elif catalog is None:
        liveness, detail = "error", "catalog URL was not fetched"
    else:
        liveness, detail = entry_status(
            {"url": row["url"], "final_url": catalog.get("final_url"), "liveness": "dead" if catalog.get("liveness") == "parked" else catalog.get("liveness"), "error": catalog.get("error"), "status": catalog.get("status")},
            {"liveness": "dead" if home and home.get("liveness") == "parked" else (home or {}).get("liveness"), "error": (home or {}).get("error")} if home else None,
        )
    images = []
    seen = set()
    languages = []
    legal_name = None
    theme = None
    for page in readable:
        for image in page.get("images") or []:
            if image["url"] not in seen:
                seen.add(image["url"])
                images.append(image)
        for lang in page.get("languages") or []:
            if lang and lang not in languages:
                languages.append(lang)
        if not legal_name and page.get("legal_name"):
            legal_name = page["legal_name"]
        if not theme and page.get("theme_color"):
            theme = page["theme_color"]
    images = images[:24]

    search = empty_search("unknown" if not (home and home.get("liveness") == "alive") else "none")
    search["directory_prefixes"] = (site or {}).get("directory_prefixes") or []
    if home and home.get("liveness") == "alive":
        search["available"] = False
        blob = (home.get("text") or "").lower()
        if any(token in blob for token in ("view full report", "unlock report", "starting at $", "subscribe to see")):
            search["paywall"] = True
        if any(token in blob for token in ("sign in to search", "log in to search", "login to view")):
            search["login_wall"] = True
    for page in readable:
        for form in page.get("forms") or []:
            page_url = page.get("final_url") or page.get("url") or ""
            if not host_matches(domain, page_url) and not page.get("followed_people_link"):
                continue
            if is_search_form(form, page_url, page.get("text") or ""):
                search = {
                    "available": True,
                    "method": "http_form_post" if form["method"] == "post" else "http_get",
                    "page_url": page.get("final_url") or page.get("url"),
                    "form_action": form.get("action"),
                    "inputs": search_inputs(form),
                    "url_pattern": search_pattern(form),
                    "directory_prefixes": (site or {}).get("directory_prefixes") or [],
                    "paywall": search.get("paywall"),
                    "login_wall": search.get("login_wall"),
                }
                break
        if search["available"]:
            break

    removal_page = catalog if catalog and (catalog.get("liveness") == "alive" or catalog.get("browser_read")) else None
    texts = " ".join(page.get("text") or "" for page in readable)
    kinds = removal_kinds(row["url"], (removal_page or {}).get("text") or "")
    channels = []
    form_fields = []
    csrf = None
    if removal_page:
        def _removalish(form: dict) -> bool:
            labels = " ".join(f"{field.get('placeholder') or ''} {field.get('name') or ''}" for field in form.get("inputs") or [])
            return _form_has_email(form) and bool(re.search(r"first|last|full name|name|address|phone", labels, re.I))

        removalish_present = any(_removalish(form) for form in removal_page.get("forms") or [])
        for form in removal_page.get("forms") or []:
            page_url = removal_page.get("final_url") or removal_page.get("url") or ""
            if is_search_form(form, page_url, removal_page.get("text") or ""):
                continue
            labels = " ".join(f"{field.get('placeholder') or ''} {field.get('name') or ''}" for field in form.get("inputs") or [])
            if removalish_present and not _form_has_email(form) and re.search(r"first|last|full name|phone|address|city|state", labels, re.I):
                continue
            if re.search(r"\bcookie\b", labels, re.I) and not _form_has_email(form):
                continue
            if re.search(r"company name", labels, re.I) and not _form_has_email(form):
                continue
            useful = [field for field in form["inputs"] if field.get("canonical") or field["type"] in ("email", "text", "select", "textarea", "checkbox")]
            bare = {(field.get("name") or field.get("placeholder") or "").strip().lower() for field in useful}
            if bare and bare <= {"search", "q", "query", "s"}:
                continue
            if useful and not form_fields:
                form_fields = useful
                channels.append("web_form")
            if not csrf:
                for field in form["inputs"]:
                    if field["type"] == "hidden" and re.search(r"csrf|token", field["name"], re.I):
                        csrf = field["name"]
    emails = []
    seen_email = set()
    phones = []
    postal = None
    portal = None
    captcha_provider = None
    captcha_sitekey = None
    captcha_pages = []
    consent = None
    for page in readable:
        for email in page.get("emails") or []:
            if email["email"] not in seen_email:
                seen_email.add(email["email"])
                emails.append({"email": email["email"], "confidence": email["confidence"], "source_url": page.get("final_url") or page.get("url") or ""})
        for phone in page.get("phones") or []:
            if phone not in phones:
                phones.append(phone)
        if not postal and page.get("postal_address"):
            postal = page["postal_address"]
        if not portal and page.get("portal"):
            portal = page["portal"]
        if page.get("captcha_provider"):
            captcha_provider = captcha_provider or page["captcha_provider"]
            captcha_sitekey = captcha_sitekey or page.get("captcha_sitekey")
            page_url = page.get("final_url") or page.get("url")
            if page_url and page_url not in captcha_pages:
                captcha_pages.append(page_url)
        if not consent and page.get("consent_vendor"):
            consent = page["consent_vendor"]
    if emails and "email" not in channels:
        channels.append("email")
    if postal and "postal_mail" not in channels:
        channels.append("postal_mail")
    if phones and "phone" not in channels:
        channels.append("phone")
    if portal and "official_portal" not in channels:
        channels.append("official_portal")
    success = None
    suppression = None
    processing_text = None
    processing_days_value = None
    if removal_page:
        body = removal_page.get("text") or ""
        found = SUCCESS_RE.search(body)
        if found:
            success = re.sub(r"\s+", " ", found.group(0)).strip()[:220]
        lowered = body.lower()
        if "not delete" in lowered or "suppress" in lowered and "retain" in lowered:
            suppression = True
        elif "permanently delete" in lowered or "erase your personal" in lowered:
            suppression = False
        proc = PROC_RE.search(body)
        if proc:
            processing_text = re.sub(r"\s+", " ", proc.group(0)).strip()[:220]
            processing_days_value = processing_days(processing_text)
    profile_required = True if any(field.get("canonical") == "profile_url" for field in form_fields) else None
    privacy_url = None
    terms_url = None
    for page in pages:
        url = page.get("final_url") or page.get("url") or ""
        role = classify_role(url, page.get("title") or "")
        if role == "privacy" and page.get("liveness") == "alive" and not privacy_url:
            privacy_url = url
        if role == "legal" and page.get("liveness") == "alive" and not terms_url and "term" in url.lower():
            terms_url = url
    auto = clause(texts)
    residency = []
    for code, pattern in JURIS:
        if re.search(pattern, texts, re.I) and code not in residency:
            residency.append(code)
    categories = []
    for name, pattern in DATA_CATEGORIES:
        if re.search(pattern, texts, re.I):
            categories.append(name)
    vendor = next((page.get("bot_vendor") for page in pages if page.get("bot_vendor")), None)
    outcomes = [page.get("liveness") for page in pages]
    if any(item == "blocked" for item in outcomes):
        bot_outcome = "hard_block" if any((page.get("status") in (401, 403)) for page in pages) else "challenge_page"
    elif pages:
        bot_outcome = "none"
    else:
        bot_outcome = "unknown"
    slim_pages = [
        {
            "url": page.get("url") or "",
            "final_url": page.get("final_url"),
            "status": page.get("status"),
            "role": page.get("role") or "other",
            "title": page.get("title") or "",
            "liveness": page.get("liveness") or "error",
        }
        for page in pages
    ]
    return {
        "schema_version": "3.0.0",
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "domain": domain,
        "checked_at": (site or {}).get("checked_at") or utc_now(),
        "liveness": liveness,
        "liveness_detail": detail,
        "homepage": (home or {}).get("final_url") or (home or {}).get("url"),
        "logo_url": pick_logo(images),
        "images": images,
        "theme_color": theme,
        "languages": languages,
        "legal_name": legal_name,
        "search": search,
        "removal": {
            "url": row["url"],
            "final_url": (catalog or {}).get("final_url"),
            "http_status": (catalog or {}).get("status"),
            "kinds": kinds,
            "channels": channels,
            "emails": emails[:12],
            "phones": phones[:3],
            "postal_address": postal,
            "portal": portal,
            "form_fields": form_fields[:40],
            "csrf_field_name": csrf,
            "requires_profile_url": profile_required,
            "captcha": {"provider": captcha_provider, "sitekey": captcha_sitekey, "pages": captcha_pages[:6]},
            "consent_vendor": consent,
            "success_text": success,
            "suppression_not_deletion": suppression,
            "processing_days": processing_days_value,
            "processing_text": processing_text,
        },
        "bot_defense": {"vendor": vendor, "outcome": bot_outcome},
        "legal": {
            "privacy_policy_url": privacy_url,
            "terms_url": terms_url,
            "automation_prohibited": True if auto else None,
            "automation_clause": auto,
            "residency": residency,
            "authorized_agent": True if re.search(r"authorized agent", texts, re.I) else None,
            "data_categories": categories,
        },
        "pages": slim_pages,
    }


def load_seeds() -> dict[str, dict]:
    if not MAPS_PATH.exists():
        return {}
    data = json.loads(MAPS_PATH.read_text(encoding="utf-8"))
    seeds = {}
    for domain, site in (data.get("domains") or {}).items():
        urls = []
        for page in site.get("pages") or []:
            if page.get("liveness") == "alive" and page.get("role") in {"privacy", "contact", "legal", "opt_out"}:
                urls.append(page.get("final_url") or page.get("url"))
        for link in site.get("link_map") or []:
            if link.get("role") in {"privacy", "contact", "opt_out"}:
                urls.append(link.get("url"))
        prefixes = list((site.get("link_prefix_counts") or {}).keys())
        sitemap = (site.get("sitemap") or {}).get("prefix_counts") or {}
        prefixes.extend(sitemap.keys())
        seeds[domain] = {"urls": list(dict.fromkeys(u for u in urls if u))[:12], "prefixes": prefixes}
    return seeds


def load_done(path) -> dict[str, dict]:
    done = {}
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("domain"):
                done[item["domain"]] = item
    return done


def scrape(args: argparse.Namespace) -> dict[str, dict]:
    rows = assign_ids(load_catalog())
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(catalog_domain(row), []).append(row)
    domains = sorted(grouped)
    if args.ids:
        wanted = {catalog_domain(row) for row in rows if row["id"] in set(args.ids.split(","))}
        domains = [domain for domain in domains if domain in wanted]
    if args.limit:
        domains = domains[: args.limit]
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and LOG_PATH.exists() and not args.ids and not args.limit:
        LOG_PATH.unlink()
    done = load_done(LOG_PATH)
    if args.fresh:
        done = {domain: item for domain, item in done.items() if domain not in set(domains)}
    pending = [domain for domain in domains if domain not in done]
    print(f"domains: {len(domains)} pending: {len(pending)}", flush=True)
    seeds = load_seeds()
    parents = load_parents()
    fetcher = MappingFetcher()
    lock = threading.Lock()
    finished = 0
    with LOG_PATH.open("a", encoding="utf-8") as log, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for domain in pending:
            info = seeds.get(domain) or {"urls": [], "prefixes": []}
            catalog_urls = list(dict.fromkeys(row["url"] for row in grouped[domain]))
            futures[pool.submit(crawl_domain, fetcher, domain, catalog_urls, info["urls"], info["prefixes"], parents)] = domain
        for future in as_completed(futures):
            domain = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"domain": domain, "checked_at": utc_now(), "error": repr(exc)[:240], "pages": [], "directory_prefixes": []}
            with lock:
                log.write(json.dumps(result, ensure_ascii=False) + "\n")
                log.flush()
                finished += 1
                if finished % 25 == 0 or finished == len(pending):
                    print(f"scanned {finished}/{len(pending)}", flush=True)
    return load_done(LOG_PATH)


def compile_profiles(raw: dict[str, dict]) -> dict:
    rows = assign_ids(load_catalog())
    brokers = {}
    for row in rows:
        domain = catalog_domain(row)
        brokers[row["id"]] = build_record(row, raw.get(domain))
    counts = {
        "liveness": dict(sorted(Counter(item["liveness"] for item in brokers.values()).items())),
        "with_logo": sum(1 for item in brokers.values() if item["logo_url"]),
        "with_image": sum(1 for item in brokers.values() if item["images"]),
        "with_search": sum(1 for item in brokers.values() if item["search"]["available"]),
        "with_email": sum(1 for item in brokers.values() if item["removal"]["emails"]),
        "with_captcha": sum(1 for item in brokers.values() if item["removal"]["captcha"]["provider"]),
        "with_form": sum(1 for item in brokers.values() if item["removal"]["form_fields"]),
    }
    return {
        "schema_version": "3.0.0",
        "checked_at": utc_now(),
        "method": (
            "Public pages only: homepage, catalog URL, and mapped privacy, contact, and legal links. "
            "robots.txt honored. No form submitted, no login, no CAPTCHA solved. "
            "A browser opened lookup homepages the static fetch never read, and catalog URLs that were blocked or failed, when robots.txt allowed the URL. "
            "Image fields are URLs, not copied files. People-search listing URLs are not stored. "
            "Null means the fetched page did not establish that fact. Every catalog id has a record."
        ),
        "broker_count": len(brokers),
        "counts": counts,
        "brokers": brokers,
    }


def write_csv(report: dict) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id", "name", "domain", "liveness", "logo_url", "search_method", "search_url", "removal_url", "channels", "email", "captcha", "bot_vendor"]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in report["brokers"].values():
            emails = item["removal"]["emails"]
            writer.writerow({
                "id": item["id"],
                "name": item["name"],
                "domain": item["domain"],
                "liveness": item["liveness"],
                "logo_url": item["logo_url"] or "",
                "search_method": item["search"]["method"],
                "search_url": item["search"]["page_url"] or "",
                "removal_url": item["removal"]["final_url"] or item["removal"]["url"] or "",
                "channels": ";".join(item["removal"]["channels"]),
                "email": emails[0]["email"] if emails else "",
                "captcha": item["removal"]["captcha"]["provider"] or "",
                "bot_vendor": item["bot_defense"]["vendor"] or "",
            })


def write_doc(report: dict) -> None:
    counts = report["counts"]
    lines = [
        "# v3 broker profiles",
        "",
        "v3 is the default broker record. v1 and v2 stay in the repo as earlier layers.",
        "",
        f"Check date: {report['checked_at']}. Brokers: {report['broker_count']}.",
        "",
        report["method"],
        "",
        "The file is [`data/v3/brokers.json`](../data/v3/brokers.json). A flat view is [`data/v3/brokers.csv`](../data/v3/brokers.csv). Schema: [`data/schema/broker-v3.schema.json`](../data/schema/broker-v3.schema.json). Rebuild with `python scripts/profiles/scan_v3.py`. A finished log can be republished with `python scripts/profiles/scan_v3.py --compile-only`.",
        "",
        "| Fact | Brokers |",
        "| --- | ---: |",
        f"| Logo link | {counts['with_logo']} |",
        f"| Any image link | {counts['with_image']} |",
        f"| Public search form | {counts['with_search']} |",
        f"| Removal email | {counts['with_email']} |",
        f"| Removal form fields | {counts['with_form']} |",
        f"| CAPTCHA marker | {counts['with_captcha']} |",
        "",
        "## Liveness",
        "",
        "| Status | Brokers |",
        "| --- | ---: |",
    ]
    for key, value in counts["liveness"].items():
        lines.append(f"| `{key}` | {value} |")
    lines.extend([
        "",
        "Image links are the logo, icon, `og:image`, or Organization JSON-LD logo published on the broker's own page. They are not downloaded into this repo.",
        "",
        "`search.available` is true when a public name, phone, address, or photo form was on a page that is not the opt-out form, including a search box on the homepage. `none` means that homepage loaded and no such form was there. `unknown` means the homepage could not be read, including when robots.txt disallowed it or a challenge page did not clear. `search.url_pattern` is the form action plus input names. It is not a tested people-search URL.",
        "",
        "A CAPTCHA `sitekey` is the public key in the page HTML. Nothing in this scan solves a CAPTCHA.",
        "",
    ])
    DOC_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate(report: dict) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)


def publish(raw: dict[str, dict]) -> dict:
    report = compile_profiles(raw)
    validate(report)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    write_csv(report)
    write_doc(report)
    print(json.dumps(report["counts"], indent=2), flush=True)
    return report


def forms_from_render(raw_forms: list, page_url: str) -> list[dict]:
    forms = []
    for form in raw_forms or []:
        action = form.get("action") or ""
        if action and not action.startswith("http"):
            action = abs_url(page_url, action) or page_url
        forms.append({
            "action": action or page_url,
            "method": "post" if (form.get("method") or "").lower() == "post" else "get",
            "inputs": [
                field_from_input(item)
                for item in form.get("inputs") or []
                if item.get("type") not in ("submit", "button", "image", "reset", "hidden")
                and (item.get("name") or item.get("placeholder") or item.get("type") in ("file", "search", "select"))
            ],
        })
    return [form for form in forms if form["inputs"]]


RENDER_JS = """
() => {
  const labelOf = (el) => {
    const parentText = el.parentElement ? (el.parentElement.innerText || "").replace(/\\s+/g, " ").trim() : "";
    const shortParent = parentText.length > 0 && parentText.length <= 40 ? parentText : "";
    return (
      (el.labels && [...el.labels].map((label) => label.innerText).join(" "))
      || el.getAttribute("aria-label")
      || el.getAttribute("placeholder")
      || shortParent
    ).replace(/\\s+/g, " ").trim().slice(0, 80);
  };
  const take = (el) => ({
    name: el.getAttribute("name") || el.id || "",
    type: (el.getAttribute("type") || (el.tagName === "TEXTAREA" ? "textarea" : el.tagName === "SELECT" ? "select" : "text")).toLowerCase(),
    placeholder: labelOf(el),
    required: el.required === true,
    options: el.tagName === "SELECT" ? [...el.options].slice(0, 320).map((opt) => {
      const value = (opt.value || "").trim();
      const text = (opt.textContent || "").replace(/\\s+/g, " ").trim();
      const label = value && !/^\\d+$/.test(value) ? value : (text || value);
      return label.slice(0, 80);
    }).filter((label) => label && !/^(select|choose|select state|select a state|choose state)$/i.test(label)) : [],
  });
  const forms = [...document.querySelectorAll("form")].slice(0, 8).map((form) => ({
    action: form.getAttribute("action") || "",
    method: (form.getAttribute("method") || "get").toLowerCase(),
    inputs: [...form.querySelectorAll("input, textarea, select")].slice(0, 40).map(take),
  }));
  const loose = [...document.querySelectorAll("input, textarea, [role=searchbox], [role=combobox]")]
    .filter((el) => !el.closest("form"))
    .filter((el) => {
      const kind = (el.getAttribute("type") || "").toLowerCase();
      if (["hidden", "submit", "button", "image", "reset", "checkbox", "radio"].includes(kind)) return false;
      if (kind === "file" || kind === "search" || el.getAttribute("role") === "searchbox") return true;
      return /first name|last name|full name|enter a name|phone|address|city|search/i.test(labelOf(el));
    })
    .slice(0, 8);
  if (loose.length) {
    forms.push({
      action: "",
      method: "get",
      inputs: loose.map((el) => {
        const item = take(el);
        if (el.getAttribute("role") === "searchbox" || el.getAttribute("role") === "combobox") item.type = item.type === "text" ? "search" : item.type;
        return item;
      }),
    });
  }
  const people = [...document.querySelectorAll("a")]
    .filter((el) => /people search|person search|find a person|search by owner name/i.test(el.textContent || ""))
    .slice(0, 2)
    .map((el) => el.href)
    .filter((href) => href && href.startsWith("http"));
  const text = ((document.body && document.body.innerText) || "").replace(/\\s+/g, " ").trim().slice(0, 4000);
  const html = (document.documentElement.innerHTML || "").slice(0, 80000);
  const emails = [...new Set((html.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/g) || []))].slice(0, 12);
  const iframes = [...document.querySelectorAll("iframe")].map((el) => el.src).filter(Boolean).slice(0, 12);
  return { title: (document.title || "").slice(0, 140), forms, people, text, html, emails, iframes };
}
"""


def _challenge(title: str, text: str) -> bool:
    blob = f"{title} {text[:300]}".lower()
    if any(token in blob for token in CHALLENGE):
        return True
    return "performing security verification" in blob or "just a moment" in blob


def _hard_error(title: str, text: str) -> bool:
    blob = f"{title} {text[:240]}".lower()
    return any(token in blob for token in (
        "connection timed out",
        "error code 522",
        "error code 520",
        "domain not configured",
        "this site can’t be reached",
        "this site can't be reached",
    ))


def render_targets(raw: dict[str, dict]) -> list[dict]:
    rows = assign_ids(load_catalog())
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(catalog_domain(row), []).append(row)
    targets = []
    for domain, group in sorted(grouped.items()):
        site = raw.get(domain) or {}
        pages = site.get("pages") or []
        record = build_record(group[0], site or None)
        homes: list[str] = []
        if (
            any(row["category"] in RENDER_CATEGORIES for row in group)
            and record["search"]["available"] is None
            and record["liveness"] != "dead"
        ):
            homes = [f"https://www.{domain}/", f"https://{domain}/"]
        catalogs: list[str] = []
        for row in group:
            catalog = next((page for page in pages if norm_url(page.get("url") or "") == norm_url(row["url"])), None)
            if catalog and catalog.get("liveness") in {"blocked", "error"} and not catalog.get("browser_read"):
                catalogs.append(row["url"])
        catalogs = list(dict.fromkeys(catalogs))[:3]
        if homes or catalogs:
            targets.append({"domain": domain, "homes": homes, "catalogs": catalogs})
    return targets


def signals_from_render(data: dict, domain: str, parents: set[str], page_url: str) -> dict:
    text = re.sub(r"\s+", " ", data.get("text") or "").strip()[:4000]
    html = data.get("html") or ""
    provider, sitekey = captcha_of(html[:80_000])
    emails = []
    seen: set[str] = set()
    for raw_email in list(data.get("emails") or []) + [match.group(1) for match in EMAIL_RE.finditer(text)]:
        decision = filter_email(str(raw_email), domain, parents) or keep_stated(domain, str(raw_email))
        if not decision or "%" in decision[0] or decision[0] in seen:
            continue
        seen.add(decision[0])
        emails.append({"email": decision[0], "confidence": decision[1], "via": "text"})
        if len(emails) >= 12:
            break
    phones = []
    for match in PHONE_RE.finditer(text):
        start = max(0, match.start() - 60)
        window = text[start:match.end() + 20].lower()
        if any(token in window for token in ("privacy", "opt", "call", "phone", "toll", "support", "contact")):
            number = re.sub(r"\s+", " ", match.group(0)).strip()
            if number not in phones:
                phones.append(number)
        if len(phones) >= 3:
            break
    return {
        "title": (data.get("title") or "")[:140],
        "text": text,
        "forms": forms_from_render(data.get("forms") or [], page_url),
        "emails": emails,
        "phones": phones,
        "captcha_provider": provider,
        "captcha_sitekey": sitekey,
        "portal": portal_of(data.get("iframes") or []),
        "bot_vendor": bot_vendor("", html[:1500]),
    }


def _stored_home(url: str, status: int | None, signals: dict, followed: bool) -> dict:
    return {
        "url": url,
        "final_url": url,
        "status": status if status is not None else 200,
        "liveness": "alive",
        "role": "home",
        "title": signals["title"],
        "text": signals["text"],
        "forms": signals["forms"],
        "emails": signals["emails"],
        "phones": signals["phones"],
        "captcha_provider": signals["captcha_provider"],
        "captcha_sitekey": signals["captcha_sitekey"],
        "portal": signals["portal"],
        "bot_vendor": signals["bot_vendor"],
        "images": [],
        "error": "",
        "followed_people_link": followed,
    }


def _catalog_acceptable(domain: str, final_url: str) -> bool:
    if host_matches(domain, final_url):
        return True
    blob = final_url.lower()
    return any(token in blob for token in ("opt", "privacy", "suppress", "removal", "jotform", "dsar", "donotsell"))


def render_missing(raw: dict[str, dict], workers: int) -> None:
    from playwright.async_api import async_playwright

    targets = render_targets(raw)
    home_n = sum(1 for item in targets if item["homes"])
    catalog_n = sum(1 for item in targets if item["catalogs"])
    print(f"render targets: {len(targets)} unread homepages: {home_n} blocked catalogs: {catalog_n}", flush=True)
    if not targets:
        return
    fetcher = MappingFetcher()
    parents = load_parents()

    async def run() -> None:
        sem = asyncio.Semaphore(max(1, workers))
        write_lock = asyncio.Lock()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )

            async def open_url(page, url: str) -> dict | None:
                if not await asyncio.to_thread(fetcher.allowed, url):
                    return {"robots": True}
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    return {"failed": True}
                status = response.status if response else None
                if status in (404, 410):
                    return {"failed": True}
                await page.wait_for_timeout(1500)
                data = await page.evaluate(RENDER_JS)
                if _challenge(data.get("title") or "", data.get("text") or ""):
                    await page.wait_for_timeout(6000)
                    data = await page.evaluate(RENDER_JS)
                    if _challenge(data.get("title") or "", data.get("text") or ""):
                        return {"challenge": True}
                title = data.get("title") or ""
                text = data.get("text") or ""
                if _hard_error(title, text) or (not title and len(text) < 40):
                    return {"failed": True}
                if status and status >= 400 and status not in (401, 403, 429, 503):
                    return {"failed": True}
                for frame in page.frames[:6]:
                    if frame == page.main_frame:
                        continue
                    frame_url = frame.url or ""
                    if not any(token in frame_url for token in ("jotform", "formstack", "typeform")):
                        continue
                    try:
                        framed = await frame.evaluate(RENDER_JS)
                    except Exception:
                        continue
                    data["forms"] = list(data.get("forms") or []) + list(framed.get("forms") or [])
                    break
                return {"status": status, "final_url": page.url, "data": data}

            async def one(target: dict) -> None:
                domain = target["domain"]
                async with sem:
                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
                        ),
                        locale="en-CA",
                        viewport={"width": 1366, "height": 768},
                    )
                    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                    page = await context.new_page()
                    home_page = None
                    catalog_hits: list[tuple[str, dict]] = []
                    try:
                        people: list[str] = []
                        loaded = []
                        for url in target["homes"]:
                            result = await open_url(page, url)
                            if not result or result.get("robots") or result.get("failed") or result.get("challenge"):
                                continue
                            final = result["final_url"]
                            if not host_matches(domain, final):
                                people = (result["data"].get("people") or people)
                                continue
                            signals = signals_from_render(result["data"], domain, parents, final)
                            built = _stored_home(final, result.get("status"), signals, False)
                            loaded.append(built)
                            people = result["data"].get("people") or people
                            if any(is_search_form(form, final, signals["text"]) for form in signals["forms"]):
                                break
                        if not any(
                            is_search_form(form, item["final_url"], item.get("text") or "")
                            for item in loaded
                            for form in item.get("forms") or []
                        ):
                            for link in people[:1]:
                                result = await open_url(page, link)
                                if not result or result.get("robots") or result.get("failed") or result.get("challenge"):
                                    continue
                                final = result["final_url"]
                                signals = signals_from_render(result["data"], domain, parents, final)
                                if any(is_search_form(form, final, signals["text"]) for form in signals["forms"]):
                                    loaded.append(_stored_home(final, result.get("status"), signals, True))
                                    break
                        if loaded:
                            home_page = next(
                                (
                                    item for item in loaded
                                    if any(is_search_form(form, item["final_url"], item.get("text") or "") for form in item.get("forms") or [])
                                ),
                                loaded[-1],
                            )
                        for url in target["catalogs"]:
                            result = await open_url(page, url)
                            if not result or result.get("robots") or result.get("failed") or result.get("challenge"):
                                print(f"catalog {domain} still closed", flush=True)
                                continue
                            final = result["final_url"]
                            if not _catalog_acceptable(domain, final):
                                print(f"catalog {domain} left the host", flush=True)
                                continue
                            catalog_hits.append((url, signals_from_render(result["data"], domain, parents, final) | {"final_url": final, "status": result.get("status")}))
                    except Exception as exc:
                        print(f"render {domain} {type(exc).__name__}: {exc}", flush=True)
                    finally:
                        await context.close()
                    if not home_page and not catalog_hits:
                        return
                    site = dict(raw.get(domain) or {"domain": domain, "pages": [], "directory_prefixes": []})
                    pages = list(site.get("pages") or [])
                    for url, signals in catalog_hits:
                        match = next((item for item in pages if norm_url(item.get("url") or "") == norm_url(url)), None)
                        if match is None:
                            continue
                        match["browser_read"] = True
                        match["title"] = signals["title"] or match.get("title")
                        match["text"] = signals["text"] or match.get("text")
                        match["final_url"] = signals["final_url"]
                        if signals.get("status"):
                            match["status"] = signals["status"]
                        if signals["forms"]:
                            match["forms"] = signals["forms"]
                        if signals["emails"]:
                            match["emails"] = signals["emails"]
                        if signals["phones"]:
                            match["phones"] = signals["phones"]
                        if signals["captcha_provider"]:
                            match["captcha_provider"] = signals["captcha_provider"]
                            match["captcha_sitekey"] = signals["captcha_sitekey"]
                        if signals["portal"]:
                            match["portal"] = signals["portal"]
                        if signals["bot_vendor"] and not match.get("bot_vendor"):
                            match["bot_vendor"] = signals["bot_vendor"]
                        field_count = sum(len(form.get("inputs") or []) for form in signals["forms"])
                        print(
                            f"catalog {domain} fields={field_count} emails={len(signals['emails'])} captcha={signals['captcha_provider']}",
                            flush=True,
                        )
                    if home_page:
                        pages.append(home_page)
                        found = any(is_search_form(form, home_page["final_url"], home_page.get("text") or "") for form in home_page.get("forms") or [])
                        print(f"home {domain} {'search' if found else 'no-search'} {home_page['final_url']}", flush=True)
                    site["pages"] = pages
                    site["rendered_gap"] = True
                    async with write_lock:
                        raw[domain] = site
                        with LOG_PATH.open("a", encoding="utf-8") as log:
                            log.write(json.dumps(site, ensure_ascii=False) + "\n")
                    await asyncio.sleep(0.3)

            await asyncio.gather(*(one(target) for target in targets))
            await browser.close()

    asyncio.run(run())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--compile-only", action="store_true")
    parser.add_argument("--render-missing", action="store_true")
    args = parser.parse_args()
    if args.compile_only:
        publish(load_done(LOG_PATH))
        return 0
    if args.render_missing:
        raw = load_done(LOG_PATH)
        render_missing(raw, min(args.workers, 3))
        publish(load_done(LOG_PATH))
        return 0
    raw = scrape(args)
    if args.limit or args.ids:
        print("partial run; compile with --compile-only after the full scan", flush=True)
        return 0
    publish(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
