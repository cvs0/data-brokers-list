#!/usr/bin/env python3
"""Fill missing key fields from each broker's own public privacy / opt-out pages.

Writes data/v1/field-fills.json: a per-field overlay on top of the v1 evidence
layer. v1 records are never rewritten. Each filled field carries the page it
came from, the check date, a confidence ("extracted" or "uncertain") and a
short quote.

Key fields: request_url, submission_methods, required_info, search_page,
jurisdiction, privacy_contact_email, verification, processing_time,
privacy_policy_url.

Rules:
- only public pages (catalog URL, privacy policy, homepage); at most 4 pages per entry;
- robots.txt is honoured for our User-Agent; disallowed pages are skipped and recorded;
- challenge / bot-wall / login pages are recorded as blocked, never bypassed;
- no form is submitted, no account is created, no CAPTCHA is touched.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from catalog import ROOT, assign_ids, load_catalog
from v2_common import canonical_field, processing_days

V1_DATASET = ROOT / "data" / "v1" / "broker-opt-outs.enriched.json"
LIVENESS_LOG = ROOT / "research" / "evidence" / "liveness.jsonl"
OUT_PATH = ROOT / "data" / "v1" / "field-fills.json"
RAW_LOG = ROOT / "research" / "evidence" / "rescrape.jsonl"
UA_TOKEN = "data-brokers-list-research"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/141.0.0.0 Safari/537.36 {UA_TOKEN}/1.0 (+https://github.com/cvs0/data-brokers-list)"
)
TIMEOUT = (8, 20)
MAX_BYTES = 1_500_000
HOST_DELAY = 2.0
KEY_FIELDS = (
    "request_url", "submission_methods", "required_info", "search_page", "jurisdiction",
    "privacy_contact_email", "verification", "processing_time", "privacy_policy_url",
)
CHALLENGE = ("just a moment", "attention required", "verify you are human", "checking your browser",
             "enable javascript and cookies", "sorry, you have been blocked", "px-captcha",
             "captcha-delivery", "performing security verification", "access denied")
EMAIL_RE = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9.-]+\.[a-z]{2,24})(?![\w-])", re.I)
PRIVACY_LOCALS = ("privacy", "optout", "opt-out", "opt_out", "dpo", "dataprotection", "data-protection",
                  "ccpa", "cpra", "gdpr", "dsar", "dsr", "remove", "removal", "delete", "suppression",
                  "datenschutz", "legal", "compliance", "consumer", "rights", "unsubscribe", "support", "help", "info")
BAD_EMAIL = ("example.com", "sentry", "wixpress", "domain.com", "email.com", "yourdomain", ".png", ".jpg",
             ".gif", ".webp", ".svg", "u003e")
JURIS = [
    ("US-CA", r"\b(ccpa|cpra|california consumer privacy|california residents?|shine the light|cal\. civ\. code)\b"),
    ("US-VA", r"\b(vcdpa|virginia consumer data|virginia residents?)\b"),
    ("US-CO", r"\b(colorado privacy act|colorado residents?)\b"),
    ("US-CT", r"\b(ctdpa|connecticut (data privacy|residents?))\b"),
    ("US-UT", r"\b(utah consumer privacy|utah residents?)\b"),
    ("US-TX", r"\b(texas data privacy|tdpsa|texas residents?)\b"),
    ("US-OR", r"\b(oregon consumer privacy|oregon residents?)\b"),
    ("US-VT", r"\b(vermont (data broker|residents?))\b"),
    ("US-NV", r"\b(nevada residents?|nrs 603a)\b"),
    ("US-NJ", r"\b(daniel'?s law|new jersey residents?)\b"),
    ("US", r"\b(united states|u\.s\. residents?|fcra|fair credit reporting act)\b"),
    ("EU", r"\b(gdpr|general data protection regulation|european economic area|\beea\b)\b"),
    ("GB", r"\b(uk gdpr|united kingdom|information commissioner'?s office)\b"),
    ("CA", r"\b(pipeda|personal information protection and electronic documents|canadian residents?|residents of canada)\b"),
    ("CA-QC", r"\b(law 25|loi 25|bill 64|quebec|québec)\b"),
    ("CA-BC", r"\b(british columbia|pipa \(bc\)|bc pipa)\b"),
    ("CA-AB", r"\b(alberta pipa|alberta residents?)\b"),
    ("AU", r"\b(australian privacy principles|privacy act 1988)\b"),
]
VERIF = [
    ("email_link", r"(verification|confirmation) (email|e-mail|link)|click (on )?the link|confirm your (email|request)|verify your email"),
    ("email_code", r"(verification|confirmation) code (sent )?(to|by) (your )?e-?mail"),
    ("phone_sms", r"\b(text message|sms|one-time (pass)?code (sent )?to your (mobile|phone))\b"),
    ("phone_call", r"\b(call (us|our) (toll[- ]free )?at|by (tele)?phone at)\b"),
    ("id_document", r"government[- ]issued|photo id|driver'?s licen[cs]e|copy of (your )?(id|identification)|passport"),
    ("captcha", r"recaptcha|hcaptcha|turnstile|captcha"),
    ("account_login", r"(sign|log) in to (your )?account to (submit|make|manage)"),
    ("profile_url", r"(url|link) (of|to) (your|the) (profile|listing|record)|paste the (url|link)"),
]
INFO = [
    ("full_name", r"\b(full name|first (and|&) last name|your name)\b"),
    ("email", r"\b(e-?mail address)\b"),
    ("street_address", r"\b(street address|mailing address|home address|current address)\b"),
    ("city", r"\bcity\b"),
    ("region", r"\b(state|province)\b"),
    ("postal_code", r"\b(zip code|postal code|zip)\b"),
    ("phone", r"\b(phone number|telephone number)\b"),
    ("birth_date", r"\b(date of birth|birth ?date|\bdob\b)\b"),
    ("age", r"\bage\b"),
    ("profile_url", r"\b(profile url|listing url|url of (the|your) (profile|listing))\b"),
]
PROC_SENT = re.compile(
    r"[^.]{0,200}\b(within|in|up to|no later than|take(s)? up to|allow( up to)?)\s+(\d{1,3}|one|two|three|five|seven|ten|fifteen|thirty|forty[- ]five|ninety)"
    r"\s+(business |calendar |working )?(hours?|days?|weeks?|months?)\b[^.]{0,160}", re.I)
PROC_CTX = re.compile(r"request|opt[- ]?out|remov|delet|suppress|process|respond|verif", re.I)
WORD = {"one": 1, "two": 2, "three": 3, "five": 5, "seven": 7, "ten": 10, "fifteen": 15, "thirty": 30,
        "forty-five": 45, "forty five": 45, "ninety": 90}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def bare(host: str | None) -> str:
    return (host or "").lower().removeprefix("www.")


def registrable(host: str) -> str:
    parts = bare(host).split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.forms: list[dict] = []
        self._a: str | None = None
        self._atext: list[str] = []
        self._skip = 0
        self._form: dict | None = None
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            self._a, self._atext = a["href"], []
        elif tag == "form":
            self._form = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(), "inputs": []}
        elif tag in ("input", "select", "textarea") and self._form is not None:
            if (a.get("type") or "").lower() not in ("hidden", "submit", "button", "image", "reset"):
                label = " ".join(filter(None, [a.get("name"), a.get("id"), a.get("placeholder"), a.get("aria-label"), a.get("type")]))
                self._form["inputs"].append(label)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._a is not None:
            self.links.append((self._a, " ".join(self._atext).strip()))
            self._a = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.text.append(data)
        if self._a is not None:
            self._atext.append(data.strip())


class Fetcher:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-CA,en;q=0.9,fr-CA;q=0.6",
                               "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
        ad = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
        self.s.mount("https://", ad)
        self.s.mount("http://", ad)
        self.lock = threading.Lock()
        self.next: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser | None] = {}

    def wait(self, host: str) -> None:
        with self.lock:
            now = time.monotonic()
            ready = self.next.get(host, now)
            self.next[host] = max(ready, now) + HOST_DELAY
        time.sleep(max(0.0, ready - now))

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        key = f"{p.scheme}://{p.netloc}"
        with self.lock:
            cached = key in self.robots
        if not cached:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                self.wait(p.netloc)
                r = self.s.get(key + "/robots.txt", timeout=TIMEOUT)
                if r.status_code in (401, 403):
                    rp = None  # treat as disallow-all, per RFC 9309 guidance for auth errors
                elif r.status_code >= 400:
                    rp.parse([])
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp.parse([])
            with self.lock:
                self.robots[key] = rp
        rp = self.robots[key]
        if rp is None:
            return False
        return rp.can_fetch(UA_TOKEN, url) and rp.can_fetch("*", url)

    def get(self, url: str) -> dict:
        if not self.allowed(url):
            return {"url": url, "skipped": "robots"}
        self.wait(urlparse(url).netloc)
        try:
            with self.s.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True) as r:
                body = b""
                for chunk in r.iter_content(65_536):
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
                ctype = r.headers.get("content-type", "")
                html = body.decode("utf-8", errors="replace")
                return {"url": url, "final_url": r.url, "status": r.status_code, "ctype": ctype, "html": html}
        except requests.RequestException as exc:
            return {"url": url, "error": type(exc).__name__}


def parse(res: dict) -> dict | None:
    if "html" not in res or "html" not in res.get("ctype", "html"):
        return None
    pg = Page()
    try:
        pg.feed(res["html"])
    except Exception:  # malformed HTML: keep what was parsed
        pass
    text = re.sub(r"\s+", " ", " ".join(pg.text)).strip()
    low = text.lower()
    blocked = res["status"] in (401, 403, 429, 503) or any(m in (pg.title + " " + low[:3000]).lower() for m in CHALLENGE)
    mailtos = [h[7:].split("?")[0] for h, _ in pg.links if h.lower().startswith("mailto:")]
    return {"text": text, "low": low, "links": pg.links, "forms": pg.forms, "title": pg.title.strip(),
            "blocked": blocked, "mailtos": mailtos, "url": res["final_url"], "status": res["status"]}


def quote(text: str, m: re.Match, pad: int = 90) -> str:
    a, b = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return text[a:b].strip()[:280]


def pick_email(pages: list[dict], domain: str, parents: set[str]) -> tuple[str, dict, str] | None:
    best = None
    for p in pages:
        cands = [(e, True) for e in p["mailtos"]] + [(m.group(1), False) for m in EMAIL_RE.finditer(p["text"])]
        for email, is_mailto in cands:
            e = email.strip().strip(".").lower()
            if any(b in e for b in BAD_EMAIL) or "@" not in e:
                continue
            local, dom = e.split("@", 1)
            same = registrable(dom) == registrable(domain) or registrable(dom) in parents
            privacyish = next((i for i, k in enumerate(PRIVACY_LOCALS) if k in local), None)
            if not same and privacyish is None:
                continue
            score = (0 if same else 50) + (privacyish if privacyish is not None else 30) - (2 if is_mailto else 0)
            conf = "extracted" if same and privacyish is not None and privacyish < 17 else "uncertain"
            if best is None or score < best[0]:
                best = (score, e, p, conf)
    return (best[1], best[2], best[3]) if best else None


def analyse(entry: dict, rec: dict, pages: list[dict], fetched: list[dict], today: str, parents: set[str]) -> dict:
    fills: dict = {}
    usable = [p for p in pages if p and not p["blocked"] and p["status"] < 400]

    def put(field, value, page, conf, ev=None):
        fills[field] = {"value": value, "source_url": page["url"], "checked_at": today, "confidence": conf,
                        "evidence": ev}

    disc, wf, ident = rec.get("discovery", {}), rec.get("request_workflow", {}), rec.get("identity", {})
    optout = next((p for p in usable if p.get("role") == "optout"), None)
    policy = next((p for p in usable if p.get("role") == "policy"), None)
    home = next((p for p in usable if p.get("role") == "home"), None)
    alltext = " \n ".join(p["text"] for p in usable)

    looks_like_request = optout and (
        any(any(canonical_field(i) for i in fm["inputs"]) for fm in optout["forms"])
        or re.search(r"opt[- ]?out|remov|suppress|do not sell|privacy (request|choices|center)|delete|dsar|your rights", optout["title"] or "", re.I))
    if not disc.get("request_url") and looks_like_request:
        put("request_url", optout["url"], optout, "extracted", "Catalog URL loaded (HTTP %s)." % optout["status"])
    if not disc.get("privacy_policy_url") and policy:
        put("privacy_policy_url", policy["url"], policy, "extracted", "Linked from the site as the privacy policy.")
    if not disc.get("privacy_contact_email"):
        hit = pick_email(usable, entry["domain"] or "", parents)
        if hit:
            put("privacy_contact_email", hit[0], hit[1], hit[2], None)
    methods = [m for m in (wf.get("submission_methods") or []) if m != "unknown"]
    if not methods and usable:
        found, ev_page = [], None
        if optout and any(len(f["inputs"]) >= 1 and any(canonical_field(i) for i in f["inputs"]) for f in optout["forms"]):
            found.append("web_form"); ev_page = optout
        for p in usable:
            l = p["low"]
            if re.search(r"(e-?mail us at|send an e-?mail to|by e-?mail(ing)? (us )?at|contact us at [\w.+-]+@)", l):
                found.append("email"); ev_page = ev_page or p
            if re.search(r"\b(p\.?\s?o\.? box|attn:|attention:|by (postal )?mail to|write to us at)\b", l):
                found.append("postal_mail"); ev_page = ev_page or p
            if re.search(r"(toll[- ]free|call us at|by (tele)?phone)[^.]{0,40}\(?\b8(00|33|44|55|66|77|88)\)?[ .-]?\d{3}[ .-]?\d{4}", l):
                found.append("phone"); ev_page = ev_page or p
        found = sorted(set(found))
        if found and ev_page:
            put("submission_methods", found, ev_page, "extracted" if "web_form" in found else "uncertain")
    if not wf.get("required_fields") and usable:
        canon = set()
        src = None
        if optout:
            for f in optout["forms"]:
                for i in f["inputs"]:
                    c = canonical_field(i)
                    if c:
                        canon.add(c); src = optout
        conf = "extracted" if canon else "uncertain"
        if not canon:
            for p in usable:
                seg = re.search(r"(provide|include|enter|submit|need)[^.]{0,40}(following|your)[^.]{0,260}", p["low"])
                if seg:
                    for name, pat in INFO:
                        if re.search(pat, seg.group(0)):
                            canon.add(name); src = src or p
        if canon and src:
            put("required_info", sorted(canon), src, conf)
    if not disc.get("supported_search_inputs") and home:
        for f in home["forms"]:
            names = {canonical_field(i) for i in f["inputs"]} - {None}
            if names & {"first_name", "last_name", "full_name", "phone"} and "email" not in names or (names & {"first_name", "last_name"}):
                action = urljoin(home["url"], f["action"] or home["url"])
                put("search_page", {"url": action, "method": f["method"], "inputs": sorted(names)}, home, "uncertain",
                    "Public search form on the homepage. Not submitted.")
                break
    geo = [g for g in (ident.get("geographic_coverage") or []) if g != "unknown"]
    if not geo and usable:
        codes = []
        for code, pat in JURIS:
            if re.search(pat, alltext, re.I):
                codes.append(code)
        if codes:
            src = policy or optout or usable[0]
            put("jurisdiction", codes, src, "uncertain", "Laws or regions named on the page; this is not proof of where the broker publishes data.")
    if wf.get("email_verification_mechanism") in (None, "unknown") and wf.get("identity_document_required") is None and usable:
        needs, ev, srcp = [], None, None
        for p in [optout, policy] if optout or policy else usable:
            if not p:
                continue
            for name, pat in VERIF:
                m = re.search(pat, p["text"], re.I)
                if m and name not in needs:
                    needs.append(name); ev = ev or quote(p["text"], m); srcp = srcp or p
        if needs and srcp:
            put("verification", needs, srcp, "uncertain", ev)
    if not wf.get("documented_processing_time") and usable:
        for p in [optout, policy, home]:
            if not p:
                continue
            for m in PROC_SENT.finditer(p["text"]):
                s = m.group(0)
                if not PROC_CTX.search(s):
                    continue
                n = m.group(4).lower()
                days = processing_days(f"{WORD.get(n, n)} {(m.group(5) or '')}{m.group(6)}")
                put("processing_time", {"text": s.strip()[:280], "days_upper_bound": days}, p, "uncertain" if days is None else "extracted")
                break
            if "processing_time" in fills:
                break
    return fills


def missing_fields(rec: dict) -> list[str]:
    d, w, i = rec.get("discovery", {}), rec.get("request_workflow", {}), rec.get("identity", {})
    out = []
    if not d.get("request_url"): out.append("request_url")
    if not [m for m in (w.get("submission_methods") or []) if m != "unknown"]: out.append("submission_methods")
    if not w.get("required_fields"): out.append("required_info")
    if not d.get("supported_search_inputs"): out.append("search_page")
    if not [g for g in (i.get("geographic_coverage") or []) if g != "unknown"]: out.append("jurisdiction")
    if not d.get("privacy_contact_email"): out.append("privacy_contact_email")
    if w.get("email_verification_mechanism") in (None, "unknown") and w.get("identity_document_required") is None: out.append("verification")
    if not w.get("documented_processing_time"): out.append("processing_time")
    if not d.get("privacy_policy_url"): out.append("privacy_policy_url")
    return out


def find_policy_link(pages: list[dict], domain: str) -> str | None:
    for p in pages:
        if not p:
            continue
        for href, text in p["links"]:
            t = (text or "").lower()
            if "privacy" in t and ("policy" in t or "notice" in t or "statement" in t or t.strip() == "privacy"):
                u = urljoin(p["url"], href)
                if registrable(urlparse(u).hostname or "") == registrable(domain):
                    return u
    return None


def process(f: Fetcher, entry: dict, rec: dict, today: str, parents: set[str]) -> tuple[dict, dict]:
    domain = bare(entry["domain"] or urlparse(entry["url"]).hostname)
    entry["domain"] = domain
    raw = {"id": entry["id"], "checked_at": utc_now(), "pages": []}
    pages = []
    url = entry["url"].replace("http://", "https://", 1) if entry.get("_https") else entry["url"]
    r = f.get(url)
    p = parse(r)
    if p: p["role"] = "optout"
    pages.append(p)
    raw["pages"].append({k: r.get(k) for k in ("url", "final_url", "status", "error", "skipped")} | {"blocked": bool(p and p["blocked"])})
    disc = rec.get("discovery", {})
    home_url = disc.get("official_homepage") or f"https://{domain}/"
    need_home = (not disc.get("supported_search_inputs")) or not disc.get("privacy_policy_url")
    hp = None
    if need_home and urlparse(url).path not in ("", "/"):
        rh = f.get(home_url)
        hp = parse(rh)
        if hp: hp["role"] = "home"
        pages.append(hp)
        raw["pages"].append({k: rh.get(k) for k in ("url", "final_url", "status", "error", "skipped")} | {"blocked": bool(hp and hp["blocked"])})
    elif p:
        hp = dict(p); hp["role"] = "home"; pages.append(hp)
    policy_url = disc.get("privacy_policy_url") or find_policy_link([p, hp], domain)
    if policy_url and policy_url.rstrip("/") != (p or {}).get("url", "").rstrip("/"):
        rp = f.get(policy_url)
        pp = parse(rp)
        if pp: pp["role"] = "policy"
        pages.append(pp)
        raw["pages"].append({k: rp.get(k) for k in ("url", "final_url", "status", "error", "skipped")} | {"blocked": bool(pp and pp["blocked"])})
    fills = analyse(entry, rec, [x for x in pages if x], raw["pages"], today, parents)
    raw["filled"] = sorted(fills)
    return fills, raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--ids")
    ap.add_argument("--skip-ids-file", help="file with ids to skip (archived/dead)")
    ap.add_argument("--https-ids-file", help="ids whose http:// catalog URL should be fetched over https")
    args = ap.parse_args()
    today = datetime.now().date().isoformat()
    recs = {r["id"]: r for r in json.loads(V1_DATASET.read_text())["records"]}
    rows = assign_ids(load_catalog())
    skip = set(Path(args.skip_ids_file).read_text().split()) if args.skip_ids_file else set()
    https = set(Path(args.https_ids_file).read_text().split()) if args.https_ids_file else set()
    parents_doc = json.loads((ROOT / "data" / "v1" / "parent-companies.json").read_text())
    parents = set()
    for txt in re.findall(r"[a-z0-9-]+\.(?:com|net|org|us|io|co)", json.dumps(parents_doc).lower()):
        parents.add(registrable(txt))
    todo = []
    for row in rows:
        if row["id"] in skip or (args.ids and row["id"] not in args.ids.split(",")):
            continue
        rec = recs[row["id"]]
        if rec.get("verification", {}).get("research_status") == "not_applicable":
            continue
        miss = missing_fields(rec)
        if miss:
            row["_https"] = row["id"] in https
            todo.append((row, rec, miss))
    print(f"entries with missing key fields: {len(todo)}", flush=True)
    existing = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {"entries": {}}
    f = Fetcher()
    done = 0
    RAW_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(args.workers) as pool, RAW_LOG.open("a") as log:
        futs = {pool.submit(process, f, row, rec, today, parents): (row, miss) for row, rec, miss in todo}
        for fut in as_completed(futs):
            row, miss = futs[fut]
            try:
                fills, raw = fut.result()
            except Exception as exc:  # keep going; record the failure
                fills, raw = {}, {"id": row["id"], "error": repr(exc)[:200]}
            raw["missing_before"] = miss
            log.write(json.dumps(raw) + "\n")
            if fills:
                existing["entries"].setdefault(row["id"], {}).update(fills)
            done += 1
            if done % 50 == 0:
                print(f"{done}/{len(todo)}", flush=True)
    counts: dict[str, int] = {}
    for e in existing["entries"].values():
        for k, v in e.items():
            counts[f"{k}:{v['confidence']}"] = counts.get(f"{k}:{v['confidence']}", 0) + 1
    out = {
        "schema": "field-fills/1.0",
        "generated_at": utc_now(),
        "method": ("Overlay on data/v1. Each value was read from the broker's own public page named in source_url "
                   "(robots.txt honoured, no form submitted, no login, no CAPTCHA). 'extracted' = parsed directly "
                   "from a form or a sentence on the page; 'uncertain' = a keyword match that needs a human check. "
                   "v1 values always win; this file only fills fields v1 left empty."),
        "key_fields": list(KEY_FIELDS),
        "counts": dict(sorted(counts.items())),
        "entries": dict(sorted(existing["entries"].items())),
    }
    OUT_PATH.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(out["counts"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
