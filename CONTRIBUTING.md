# Contributing

`opt-outs.csv` is the source of truth. Category files under `brokers/` are generated from it. Change the CSV first, then regenerate the tables so the counts still match.

## Add or correct a row

Use these columns, in this order:

`name,category,url,notes,domain,http_status,sources`

- `category` is one of `people-search`, `background-checks`, `marketing`, `credit-financial`, `public-records`, `other`.
- `url` must be a page you opened, or a URL already published by the company, a registry, or one of the datasets named in [NOTICE](NOTICE). Do not guess a path.
- `notes` should say what the page is (opt-out form, privacy policy, mail-only, account required) and the check date if you rechecked it.
- `http_status` is the status from the check, or empty if you could not record one. Do not invent a 200.
- `sources` names where the URL came from, such as `persprotect`, `markup`, or `manual`. Combine with semicolons when more than one applies.

Keep one row per URL. If two brand names share a portal, both rows may use that same URL.

The full public-page scan is `data/v3/brokers.json`, one profile per catalog id. v4 (`data/v4/brokers.json`) is rebuilt from those records plus `research/evidence/v4.jsonl`. Do not edit either JSON file by hand. The enriched records in `data/v1/` are built from this CSV. Do not add or drop CSV rows without rebuilding the v3 profiles and those v1 records. Stable IDs are derived from the name, with the domain added only when two slugs collide. Leave the CSV columns in the order above so existing consumers keep working. The ID join lives in `data/v1/catalog-ids.csv`.

Research, automation, and profile commands live in subfolders of `scripts/`. The index is [scripts/README.md](scripts/README.md).

## After editing the CSV

Regenerate `brokers/*.md` so each file is a table:

`| Broker | Opt-out / privacy URL | Notes |`

Sort each category by broker name. Update the entry counts in `README.md`. The six category files plus the CSV must contain the same  rows, with none dropped and none added that are not in the CSV.

## Commits

Use a conventional commit (`fix:`, `docs:`, `feat:`). A one-line summary of which broker changed is enough.
