# v1 layer (frozen snapshot)

The files in this folder describe an earlier, larger catalog of 1,122 rows, researched between 2026-10-01 and 2026-10-12. They are kept for reference and are no longer maintained. `opt-outs.csv` has since been reviewed and trimmed, so many v1 records point at rows that are no longer in the catalog, and rows added later have no v1 record.

Use these instead:

- `opt-outs.csv` for the current links.
- `data/v3/brokers.json` for the full public-page scan of every current row.
- `data/v4/brokers.json` for search, removal, location, and legal facts.

The v1 files are not rebuilt when the catalog changes. `scripts/research/validate_dataset.py` checks the snapshot against its own catalog fields and prints a warning instead of comparing it with the current catalog.

One file here is still live: `catalog-ids.csv` pins the stable id for each name and URL pair. Every current catalog row must have a pinned id there, and older pins stay so a published id is never reused. Add a pin when you add a row, and move the pin when you rename a row or change its URL so its id stays the same.
