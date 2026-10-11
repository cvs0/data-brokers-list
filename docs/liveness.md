# Liveness checks and the archive

Brokers shut down, get seized, move domains and let pages rot. The catalog is only useful if dead rows leave it, but one bad night must never remove a live broker. This is how rows are checked and retired.

## 1. Check (`scripts/liveness_check.py`)

One pass looks at every catalog row:

- DNS for the domain and `www.` (and the URL's own host when it differs).
- A GET of the opt-out URL and, when that URL isn't the homepage, of `https://<domain>/`.
- Browser-like User-Agent with a project contact string, no proxy, no cookies kept, 1 s between requests to the same host, 16 workers, 8 s connect / 15 s read timeouts, two retries with backoff on network errors and 5xx.
- The first 120 KB of each page are read to spot parking pages and seizure banners. Nothing is submitted.

Each pass appends to `research/evidence/liveness.jsonl`, labelled (`--pass-label 2026-10-11-a`).

## 2. Classify (`scripts/classify_liveness.py`)

| Verdict | Meaning | Action |
|---|---|---|
| `alive` | 2xx/3xx | none |
| `blocked` | 401/403/429/503 or a challenge page | none: a bot wall is a sign of life |
| `url_drift` | site alive, but the opt-out URL 404s or lands on another host | fix the URL (reviewed) |
| `https_upgrade` | http:// times out, https answers | switch to https |
| `flaky` | passes disagree | keep, recheck next run |
| `pending_recheck` | only one pass so far | keep |
| `seized` | court-order / seizure banner | archive |
| `parked` | registrar parking or for-sale page (short page, non-privacy title, or a redirect to a domain marketplace) | archive |
| `dns_dead` | no DNS record | archive |
| `gone` | opt-out URL and homepage both 404/410 | archive |
| `unreachable` | connection failures on both | archive |

An archive verdict needs **every** pass to agree, with at least two passes and the first and last at least 45 minutes apart (`--min-gap-minutes`). Otherwise the row is `flaky` or `pending_recheck` and stays.

## 3. Fix URLs (`scripts/apply_url_fixes.py`)

URL drift is fixed from a reviewed file in `research/manual/url-fixes-*.json`. Each replacement is a link on the broker's own homepage or the final URL of the broker's own redirect, opened on the check date. The script updates `opt-outs.csv`, `data/v1/catalog-ids.csv` and the v1 record together so the row keeps its stable id, and appends a "Rechecked" note.

## 4. Archive (`scripts/archive_entries.py`)

Moves archive-verdict rows out of the catalog into `data/archived.json` with the full original CSV row, id, reason, date and the evidence from every pass. It updates everything that counts or lists rows in one go: `opt-outs.csv`, `EXPECTED_CATALOG_ROWS`, `catalog-ids.csv`, the v1 dataset and its index files, v2 specs and priority files, the overlays, `brokers/*.md`, and the counts in `README.md` and `SOURCES.md`.

Archived rows are never silently re-added. If a broker comes back (a seized network can reappear on new domains), add it as a new row with a fresh check.

## Run it

```
pip install -r requirements.txt
cd scripts
python liveness_check.py --pass-label "$(date +%F)-a"
# wait at least 45 minutes
python liveness_check.py --pass-label "$(date +%F)-b"
python classify_liveness.py
python archive_entries.py --dry-run
python archive_entries.py
python validate_dataset.py && python validate_v2.py
```
