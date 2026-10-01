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


EXPECTED_CATALOG_ROWS = 1122
PINNED_IDS_PATH = ROOT / "data" / "v1" / "catalog-ids.csv"


def load_catalog(path: Path = CATALOG_PATH) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CATALOG_COLUMNS:
            raise SystemExit(
                f"Unexpected opt-outs.csv columns: {reader.fieldnames}. "
                "Refusing to continue so existing consumers keep a stable file."
            )
        rows = list(reader)
    if len(rows) != EXPECTED_CATALOG_ROWS:
        raise SystemExit(
            f"Expected {EXPECTED_CATALOG_ROWS} catalog rows, found {len(rows)}."
        )
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


def _pinned_ids() -> dict[tuple[str, str], str]:
    """Keep ids already published for a name+url pair.

    New catalog rows still use the slug rules. A later name collision must
    not rename a row that already has an id in catalog-ids.csv.
    """
    if not PINNED_IDS_PATH.exists():
        return {}
    pinned: dict[tuple[str, str], str] = {}
    with PINNED_IDS_PATH.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("id") and row.get("name") and row.get("url"):
                pinned[(row["name"], row["url"])] = row["id"]
    return pinned


def assign_ids(rows: list[dict]) -> list[dict]:
    """Return rows with a stable `id`. Collision suffix uses the domain."""
    pinned = _pinned_ids()
    buckets: dict[str, list[int]] = defaultdict(list)
    slugs = []
    for index, row in enumerate(rows):
        slug = _base_slug(row["name"])
        slugs.append(slug)
        buckets[slug].append(index)

    assigned: list[str | None] = [None] * len(rows)
    used: set[str] = set()
    for index, row in enumerate(rows):
        entry_id = pinned.get((row["name"], row["url"]))
        if not entry_id:
            continue
        if entry_id in used:
            raise SystemExit(f"Pinned entry id {entry_id} is duplicated.")
        assigned[index] = entry_id
        used.add(entry_id)

    for index, row in enumerate(rows):
        if assigned[index]:
            continue
        slug = slugs[index]
        if len(buckets[slug]) == 1 and slug not in used:
            entry_id = slug
        else:
            entry_id = f"{slug}-{_domain_slug(row['domain'])}"
            if entry_id in used:
                digest = hashlib.sha1(row["url"].encode("utf-8")).hexdigest()[:6]
                entry_id = f"{entry_id}-{digest}"
        if entry_id in used:
            raise SystemExit(f"Entry id still collides: {entry_id}")
        assigned[index] = entry_id
        used.add(entry_id)

    enriched = []
    for index, row in enumerate(rows):
        item = dict(row)
        item["id"] = assigned[index]
        item["sources_list"] = [
            part for part in row["sources"].split(";") if part
        ]
        enriched.append(item)
    return enriched


def shared_url_groups(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["url"]].append(row)
    return {url: members for url, members in grouped.items() if len(members) > 1}
