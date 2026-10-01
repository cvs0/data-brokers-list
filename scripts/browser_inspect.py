#!/usr/bin/env python3
"""Headless browser inspection of opt-out and search pages.

Reads pages and, for the shortlist only, clicks non-submit navigation controls
to see later wizard steps. Does not fill fields, submit forms, create accounts,
upload identity documents, send SMS, solve CAPTCHAs, or bypass access controls.
A challenge or HTTP 403 is recorded as seen. It is not evidence that a
residential proxy is required.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright

from catalog import ROOT, assign_ids, load_catalog

OUT_PATH = ROOT / "research" / "evidence" / "browser-pass.jsonl"
SHORTLIST_PATH = ROOT / "data" / "v1" / "automation-shortlist.json"
DATASET_PATH = ROOT / "data" / "v1" / "broker-opt-outs.enriched.json"

NAV_TIMEOUT_MS = 18000
WORKERS = 3
HOST_GAP = 0.5

CHALLENGE_RE = re.compile(
    r"just a moment|attention required|cf-browser-verification|verify you are human|"
    r"checking your browser|enable javascript and cookies|sorry, you have been blocked|"
    r"access denied|you are unable to access|bot verification",
    re.I,
)
WIZARD_RE = re.compile(r"step\s+\d+\s+of\s+\d+", re.I)
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
SAFE_NAME_RE = re.compile(
    r"^(start(?:\s+here)?|get started|begin|next|continue|learn more|removal steps|how do i remove my listing\??)$",
    re.I,
)
SKIP_HREF_RE = re.compile(r"/(login|sign-?in|sign-?up|register|account|checkout)(/|$|\?)", re.I)

# Official pages opened only to read eligibility language. Not added to opt-outs.csv
# unless a later reviewed edit does so. Failures stay failures.
CANADIAN_EXTRA = [
    "https://www.priv.gc.ca/en/privacy-topics/privacy-laws-in-canada/the-personal-information-protection-and-electronic-documents-act-pipeda/pipeda_brief/",
    "https://www.priv.gc.ca/en/privacy-topics/privacy-laws-in-canada/the-personal-information-protection-and-electronic-documents-act-pipeda/",
    "https://crtc.gc.ca/eng/internet/anti.htm",
    "https://www.fightspam.gc.ca/eic/site/030.nsf/eng/home",
    "https://www.elections.ab.ca/voters/voter-registration/",
    "https://www.elections.sk.ca/voters/registration/",
    "https://www.electionsmanitoba.ca/en/Voting/RegisterToVote",
    "https://www.elections.on.ca/en/register-to-vote.html",
    "https://www.elections.ns.ca/",
    "https://www.electionsnb.ca/",
    "https://www.electionspei.ca/",
    "https://www.elections.gov.nl.ca/",
    "https://elections.yukon.ca/",
    "https://www.electionsnwt.ca/",
    "https://www.elections.nu.ca/",
    "https://www.ipc.on.ca/en/privacy-individuals",
    "https://oipc.sk.ca/",
    "https://www.ombudsman.mb.ca/info/access-and-privacy.html",
    "https://oipc.nl.ca/",
    "https://oipc.novascotia.ca/",
    "https://www.oic-bci.ca/",
    "https://www.ombudsman.yk.ca/",
    "https://www.assembly.gov.nt.ca/content/information-and-privacy-commissioner",
    "https://www.info-privacy.nu.ca/",
]

MAJOR_SEARCH_DOMAINS = {
    "beenverified.com",
    "spokeo.com",
    "whitepages.com",
    "intelius.com",
    "truthfinder.com",
    "instantcheckmate.com",
    "checkpeople.com",
    "mylife.com",
    "nuwber.com",
    "truepeoplesearch.com",
    "fastpeoplesearch.com",
    "peoplefinders.com",
    "smartbackgroundchecks.com",
    "familytreenow.com",
    "usphonebook.com",
    "radaris.com",
    "pimeyes.com",
    "192.com",
    "socialcatfish.com",
    "cocofinder.com",
    "numlookup.com",
    "canada411.ca",
    "411.ca",
    "yellowpages.ca",
    "canadapages.com",
    "whitepagescanada.ca",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def load_done(path: Path) -> set[tuple[str, str]]:
    done = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        done.add((item.get("mode"), item.get("url")))
    return done


def append(path: Path, item: dict, lock: asyncio.Lock) -> None:
    # sync write under the event-loop lock; the file is small per line
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


async def extract(page) -> dict:
    return await page.evaluate(
        """() => {
          const text = (document.body && document.body.innerText) ? document.body.innerText : "";
          const title = document.title || "";
          const forms = [...document.querySelectorAll("form")].slice(0, 8).map(form => ({
            action: form.getAttribute("action") || "",
            method: (form.getAttribute("method") || "get").toLowerCase(),
            id: form.id || "",
            fields: [...form.querySelectorAll("input, select, textarea")].slice(0, 40).map(el => ({
              tag: el.tagName.toLowerCase(),
              type: (el.getAttribute("type") || el.tagName || "").toLowerCase(),
              name: el.getAttribute("name") || "",
              id: el.id || "",
              placeholder: el.getAttribute("placeholder") || "",
              aria_label: el.getAttribute("aria-label") || "",
              required: el.required === true,
            })).filter(field => !["hidden", "submit", "button", "image"].includes(field.type))
          }));
          const captcha = [];
          const html = document.documentElement ? document.documentElement.innerHTML.slice(0, 400000) : "";
          const markers = [
            ["g-recaptcha", "recaptcha"],
            ["h-captcha", "hcaptcha"],
            ["hcaptcha", "hcaptcha"],
            ["cf-turnstile", "turnstile"],
            ["challenges.cloudflare.com", "turnstile"],
            ["funcaptcha", "funcaptcha"],
            ["arkoselabs", "arkose"],
          ];
          for (const [needle, label] of markers) {
            if (html.toLowerCase().includes(needle) && !captcha.includes(label)) captcha.push(label);
          }
          for (const frame of document.querySelectorAll("iframe")) {
            const src = (frame.getAttribute("src") || "").toLowerCase();
            if (src.includes("recaptcha") && !captcha.includes("recaptcha")) captcha.push("recaptcha");
            if (src.includes("hcaptcha") && !captcha.includes("hcaptcha")) captcha.push("hcaptcha");
            if (src.includes("turnstile") || src.includes("challenges.cloudflare.com")) {
              if (!captcha.includes("turnstile")) captcha.push("turnstile");
            }
          }
          const hreflang = [...document.querySelectorAll("link[rel='alternate'][hreflang], a[hreflang]")]
            .slice(0, 12)
            .map(el => ({
              lang: el.getAttribute("hreflang") || "",
              href: el.getAttribute("href") || "",
            }));
          const links = [...document.querySelectorAll("a[href]")]
            .slice(0, 80)
            .map(el => ({text: (el.innerText || "").trim().slice(0, 120), href: el.href}));
          return {title, text: text.slice(0, 7000), text_length: text.length, forms, captcha, hreflang, links};
        }"""
    )


def outcome_for(status: int | None, extracted: dict, error: str | None) -> tuple[str, str | None]:
    if error:
        if "Timeout" in error:
            return "timeout", error
        return "error", error
    text = f"{extracted.get('title') or ''} {(extracted.get('text') or '')[:1500]}"
    if status in {401, 403, 429, 503} or CHALLENGE_RE.search(text):
        return "challenge", "Browser still showed a challenge, access denial, or anti-bot interstitial."
    if status in {404, 410}:
        return "http_error", f"HTTP {status}"
    if (extracted.get("text_length") or 0) < 180 and not extracted.get("forms"):
        return "http_error", "Page rendered with almost no visible text."
    return "readable", None


async def safe_clicks(page, limit: int = 2) -> list[dict]:
    clicks = []
    for _ in range(limit):
        candidate = await page.evaluate(
            """() => {
              const nodes = [...document.querySelectorAll("a, button, [role='button']")];
              for (const el of nodes) {
                const text = (el.innerText || el.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim();
                if (!text || text.length > 60) continue;
                const type = (el.getAttribute("type") || "").toLowerCase();
                const tag = el.tagName.toLowerCase();
                if (tag === "button" && type !== "button") continue;
                if (tag === "input") continue;
                const href = el.getAttribute("href") || "";
                if (/^mailto:|^tel:/i.test(href)) continue;
                const form = el.closest("form");
                if (form && tag !== "a") continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) continue;
                return {text, href, tag};
              }
              return null;
            }"""
        )
        if not candidate or not SAFE_NAME_RE.match(candidate["text"]):
            # The evaluate above returns the first visible control, not the first safe name.
            # Search specifically for a safe label instead.
            candidate = await page.evaluate(
                """() => {
                  const wanted = /^(start(?:\\s+here)?|get started|begin|next|continue|learn more|removal steps|how do i remove my listing\\??)$/i;
                  const nodes = [...document.querySelectorAll("a, button, [role='button']")];
                  for (const el of nodes) {
                    const text = (el.innerText || el.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim();
                    if (!wanted.test(text)) continue;
                    const type = (el.getAttribute("type") || "").toLowerCase();
                    const tag = el.tagName.toLowerCase();
                    if (tag === "button" && type !== "button") {
                      return {text, skipped: "submit_button"};
                    }
                    if (el.closest("form") && tag !== "a") {
                      return {text, skipped: "inside_form"};
                    }
                    const href = el.href || el.getAttribute("href") || "";
                    return {text, href, tag, skipped: ""};
                  }
                  return null;
                }"""
            )
        if not candidate:
            break
        if candidate.get("skipped"):
            clicks.append({"text": candidate.get("text"), "result": "not_clicked", "reason": candidate["skipped"]})
            break
        href = candidate.get("href") or ""
        if href and SKIP_HREF_RE.search(href):
            clicks.append({"text": candidate.get("text"), "href": href, "result": "not_clicked", "reason": "account_or_login_url"})
            break
        before = page.url
        try:
            locator = page.get_by_text(candidate["text"], exact=True).first
            await locator.click(timeout=4000)
            await page.wait_for_timeout(1500)
        except Exception as exc:
            clicks.append({"text": candidate.get("text"), "result": "click_failed", "reason": str(exc)[:180]})
            break
        clicks.append({"text": candidate.get("text"), "from_url": before, "to_url": page.url, "result": "clicked"})
        if page.url == before and len(clicks) >= 1:
            # One in-page click is enough if the URL did not change; a second click can loop.
            break
    return clicks


def search_signals(extracted: dict, final_url: str) -> dict:
    text = extracted.get("text") or ""
    lowered = text.lower()
    labels = []
    inputs = []
    for form in extracted.get("forms") or []:
        for field in form.get("fields") or []:
            blob = " ".join(
                part for part in (field.get("name"), field.get("id"), field.get("placeholder"), field.get("aria_label"), field.get("type")) if part
            ).lower()
            labels.append(blob[:160])
            kind = None
            if re.search(r"e-?mail", blob):
                kind = "email"
            elif re.search(r"phone|mobile|tel", blob):
                kind = "phone"
            elif re.search(r"first|last|full.?name|\bname\b", blob):
                kind = "name"
            elif re.search(r"city|state|zip|postal|address|location", blob):
                kind = "location"
            elif re.search(r"search|query|q\b", blob):
                kind = "other"
            if kind and kind not in inputs:
                inputs.append(kind)
    captcha = bool(extracted.get("captcha"))
    login_wall = bool(re.search(r"\b(sign in to|log in to|create an account to|login to search)\b", lowered))
    payment_wall = bool(
        re.search(r"\b(subscription|start( your)? (free )?trial|unlock (the )?report|pay to|pricing|membership)\b", lowered)
    )
    free_copy = None
    for needle in ("free people search", "search for free", "free search", "no charge to search"):
        if needle in lowered:
            free_copy = needle
            break
    return {
        "homepage_final_url": final_url,
        "inputs": inputs or ["unknown"],
        "input_labels": labels[:12],
        "captcha_on_search": captcha,
        "login_wall_copy": login_wall,
        "payment_copy_visible": payment_wall,
        "free_search_copy": free_copy,
        "query_submitted": False,
        "notes": (
            "Search form was observed on a public page. No name, phone, or email was typed and no search was submitted, "
            "so a result-page paywall was not tested."
            if inputs
            else "No people-search style inputs were parsed on the loaded page. No query was submitted."
        ),
    }


def bilingual_candidate(extracted: dict, page_url: str) -> str | None:
    for item in extracted.get("hreflang") or []:
        lang = (item.get("lang") or "").lower()
        href = item.get("href") or ""
        if lang.startswith("fr") and href:
            return urljoin(page_url, href)
    for link in extracted.get("links") or []:
        text = (link.get("text") or "").strip().lower()
        href = link.get("href") or ""
        if text in {"fr", "français", "francais", "french"} and href:
            if host_of(href) == host_of(page_url) or host_of(href).endswith(host_of(page_url)):
                return href
    parsed = urlparse(page_url)
    if "/en/" in parsed.path or parsed.path.startswith("/en"):
        return page_url.replace("/en/", "/fr/", 1).replace("/en", "/fr", 1)
    return None


async def inspect_one(browser, target: dict, host_lock: dict, host_guard: asyncio.Lock) -> dict:
    url = target["url"]
    mode = target["mode"]
    host = host_of(url)
    async with host_guard:
        now = asyncio.get_event_loop().time()
        last = host_lock.get(host, 0.0)
        wait = HOST_GAP - (now - last)
        host_lock[host] = now + max(wait, 0.0)
    if wait > 0:
        await asyncio.sleep(wait)
    started = utc_now()
    context = await browser.new_context(locale="en-CA", viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    status = None
    error = None
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        status = response.status if response else None
        await page.wait_for_timeout(1600 if mode == "shortlist" else 900)
        extracted = await extract(page)
        kind, reason = outcome_for(status, extracted, None)
        clicks = []
        post_click_captcha = None
        if mode == "shortlist" and kind == "readable":
            before_captcha = list(extracted.get("captcha") or [])
            clicks = await safe_clicks(page)
            if any(click.get("result") == "clicked" for click in clicks):
                extracted = await extract(page)
                kind, reason = outcome_for(status, extracted, None)
                after = extracted.get("captcha") or []
                if after and after != before_captcha:
                    post_click_captcha = after
                elif after and not before_captcha:
                    post_click_captcha = after
        wizard = WIZARD_RE.findall(extracted.get("text") or "")
        emails = []
        for match in EMAIL_RE.findall(extracted.get("text") or "")[:8]:
            if not any(skip in match.lower() for skip in ("example.com", "sentry", "wixpress", "schema.org")):
                emails.append(match)
        bilingual = None
        if mode == "canadian" and kind == "readable":
            follow = bilingual_candidate(extracted, page.url)
            if follow and follow.rstrip("/") != page.url.rstrip("/"):
                try:
                    follow_response = await page.goto(follow, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    await page.wait_for_timeout(800)
                    follow_extracted = await extract(page)
                    follow_status = follow_response.status if follow_response else None
                    follow_kind, follow_reason = outcome_for(follow_status, follow_extracted, None)
                    bilingual = {
                        "url": follow,
                        "final_url": page.url,
                        "http_status": follow_status,
                        "outcome": follow_kind,
                        "reason": follow_reason,
                        "title": follow_extracted.get("title"),
                        "text_excerpt": (follow_extracted.get("text") or "")[:2500],
                    }
                except Exception as exc:
                    bilingual = {"url": follow, "outcome": "error", "reason": str(exc)[:240]}
        search = None
        if mode == "search":
            search = search_signals(extracted, page.url)
        item = {
            "url": url,
            "mode": mode,
            "entry_ids": target.get("entry_ids") or [],
            "started_at": started,
            "final_url": page.url,
            "http_status": status,
            "title": (extracted.get("title") or "")[:240],
            "text_excerpt": extracted.get("text") or "",
            "text_length": extracted.get("text_length") or 0,
            "outcome": kind,
            "outcome_reason": reason,
            "captcha_markers": extracted.get("captcha") or [],
            "captcha_visible": bool(extracted.get("captcha")),
            "forms": extracted.get("forms") or [],
            "wizard_hints": wizard[:4],
            "safe_clicks": clicks,
            "post_click_captcha": post_click_captcha,
            "emails": emails,
            "hreflang": extracted.get("hreflang") or [],
            "bilingual_follow": bilingual,
            "search": search,
            "query_submitted": False,
            "form_submitted": False,
            "error": None,
        }
    except Exception as exc:
        item = {
            "url": url,
            "mode": mode,
            "entry_ids": target.get("entry_ids") or [],
            "started_at": started,
            "final_url": None,
            "http_status": status,
            "title": None,
            "text_excerpt": "",
            "text_length": 0,
            "outcome": "timeout" if "Timeout" in type(exc).__name__ or "Timeout" in str(exc) else "error",
            "outcome_reason": str(exc)[:300],
            "captcha_markers": [],
            "captcha_visible": None,
            "forms": [],
            "wizard_hints": [],
            "safe_clicks": [],
            "post_click_captcha": None,
            "emails": [],
            "hreflang": [],
            "bilingual_follow": None,
            "search": None,
            "query_submitted": False,
            "form_submitted": False,
            "error": type(exc).__name__,
        }
    finally:
        await context.close()
    return item


def targets_for(mode: str) -> list[dict]:
    rows = assign_ids(load_catalog())
    by_url = defaultdict(list)
    for row in rows:
        by_url[row["url"]].append(row)
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    by_id = {record["id"]: record for record in dataset["records"]}
    if mode == "shortlist":
        shortlist = json.loads(SHORTLIST_PATH.read_text(encoding="utf-8"))
        targets = []
        seen = set()
        for candidate in shortlist["candidates"]:
            record = by_id[candidate["entry_id"]]
            url = record["catalog"]["url"]
            if url in seen:
                continue
            seen.add(url)
            targets.append({"mode": mode, "url": url, "entry_ids": [row["id"] for row in by_url[url]]})
        return targets
    if mode == "blocked":
        targets = []
        seen = set()
        for record in dataset["records"]:
            if record["verification"]["research_status"] != "blocked":
                continue
            url = record["catalog"]["url"]
            if url in seen:
                continue
            seen.add(url)
            quality = record["automation"]["direct_access"]["content_quality"]
            blockers = record["automation"]["observed_blockers"]
            if quality not in {"challenge", "error"} and "bot_wall" not in blockers and "inaccessible_page" not in blockers:
                continue
            targets.append({"mode": mode, "url": url, "entry_ids": [row["id"] for row in by_url[url]]})
        return targets
    if mode == "search":
        shortlist = json.loads(SHORTLIST_PATH.read_text(encoding="utf-8"))
        wanted_ids = {candidate["entry_id"] for candidate in shortlist["candidates"]}
        targets = []
        seen_hosts = set()
        ordered = []
        for record in dataset["records"]:
            domain = host_of("https://" + record["catalog"]["domain"])
            priority = 0
            if record["id"] in wanted_ids or domain in MAJOR_SEARCH_DOMAINS:
                priority = 0
            elif record["catalog"]["category"] == "people-search" or record["identity"]["entry_type"] == "people_search_site":
                priority = 1
            else:
                continue
            ordered.append((priority, record))
        ordered.sort(key=lambda item: (item[0], item[1]["id"]))
        for _, record in ordered:
            domain = record["catalog"]["domain"]
            host = host_of("https://" + domain)
            if host in seen_hosts:
                continue
            seen_hosts.add(host)
            homepage = f"https://{domain}/" if domain.startswith("www.") or "." in domain else record["catalog"]["url"]
            if not domain:
                continue
            homepage = "https://" + domain.strip("/") + "/"
            if not homepage.startswith("http"):
                homepage = "https://" + homepage
            targets.append({"mode": mode, "url": homepage, "entry_ids": [record["id"]]})
        return targets
    if mode == "canadian":
        targets = []
        seen = set()
        for record in dataset["records"]:
            blob = " ".join(
                [
                    record["catalog"]["name"],
                    record["catalog"]["url"],
                    record["catalog"]["domain"],
                    record["catalog"]["notes"],
                ]
            ).lower()
            if not any(
                token in blob
                for token in (
                    "canada",
                    "canadian",
                    ".ca",
                    "quebec",
                    "ontario",
                    "alberta",
                    "pipeda",
                    "priv.gc",
                    "oipc",
                    "elections",
                    "lnnte",
                    "canadapost",
                    "thecma",
                )
            ):
                continue
            url = record["catalog"]["url"]
            if url in seen:
                continue
            seen.add(url)
            targets.append({"mode": mode, "url": url, "entry_ids": [record["id"]]})
        for url in CANADIAN_EXTRA:
            if url not in seen:
                seen.add(url)
                targets.append({"mode": mode, "url": url, "entry_ids": [], "extra": True})
        return targets
    raise SystemExit(f"unknown mode {mode}")


async def run(modes: list[str]) -> None:
    done = load_done(OUT_PATH)
    queue = []
    for mode in modes:
        for target in targets_for(mode):
            if (mode, target["url"]) in done:
                continue
            queue.append(target)
    print(f"queued {len(queue)} pages across {modes}", flush=True)
    if not queue:
        return
    write_lock = asyncio.Lock()
    host_lock: dict[str, float] = {}
    host_guard = asyncio.Lock()
    sem = asyncio.Semaphore(WORKERS)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        async def worker(target: dict) -> None:
            async with sem:
                item = await inspect_one(browser, target, host_lock, host_guard)
                async with write_lock:
                    append(OUT_PATH, item, write_lock)
                print(f"{item['mode']} {item['outcome']} {item.get('http_status')} {target['url'][:90]}", flush=True)

        await asyncio.gather(*(worker(target) for target in queue))
        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modes", nargs="+", required=True, choices=["shortlist", "blocked", "search", "canadian"])
    args = parser.parse_args()
    asyncio.run(run(args.modes))


if __name__ == "__main__":
    main()
