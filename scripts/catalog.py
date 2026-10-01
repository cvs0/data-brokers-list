"""Shared helpers for the opt-out catalog and enriched records.

opt-outs.csv stays the link catalog. Stable IDs are derived from the broker
name and, only when that slug collides, the domain. name+url is unique.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "opt-outs.csv"

CATALOG_COLUMNS = [
    "name",
    "category",
    "url",
    "notes",
    "domain",
    "http_status",
    "sources",
]

CATEGORIES = {
    "people-search",
    "background-checks",
    "marketing",
    "credit-financial",
    "public-records",
    "other",
}


def load_catalog(path: Path = CATALOG_PATH) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CATALOG_COLUMNS:
            raise SystemExit(
                f"Unexpected opt-outs.csv columns: {reader.fieldnames}. "
                "Refusing to continue so existing consumers keep a stable file."
            )
        rows = list(reader)
    if len(rows) != 968:
        raise SystemExit(f"Expected 968 catalog rows, found {len(rows)}.")
    keys = [(row["name"], row["url"]) for row in rows]
    if len(keys) != len(set(keys)):
        raise SystemExit("Catalog name+url is no longer unique.")
    return rows


def _base_slug(name: str) -> str:
    text = name.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-+", "-", text)
    if len(text) <= 72:
        return text or "entry"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return text[:64].rstrip("-") + "-" + digest


def _domain_slug(domain: str) -> str:
    text = domain.lower().removeprefix("www.")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:40] or "domain"


def assign_ids(rows: list[dict]) -> list[dict]:
    """Return rows with a stable `id`. Collision suffix uses the domain."""
    buckets: dict[str, list[int]] = defaultdict(list)
    slugs = []
    for index, row in enumerate(rows):
        slug = _base_slug(row["name"])
        slugs.append(slug)
        buckets[slug].append(index)

    enriched = []
    for index, row in enumerate(rows):
        slug = slugs[index]
        entry_id = slug
        if len(buckets[slug]) > 1:
            entry_id = f"{slug}-{_domain_slug(row['domain'])}"
        item = dict(row)
        item["id"] = entry_id
        item["sources_list"] = [
            part for part in row["sources"].split(";") if part
        ]
        enriched.append(item)

    ids = [item["id"] for item in enriched]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise SystemExit(f"Entry IDs still collide: {dupes}")
    return enriched


def shared_url_groups(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["url"]].append(row)
    return {url: members for url, members in grouped.items() if len(members) > 1}
