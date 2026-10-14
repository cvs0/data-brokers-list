# Scripts

Run these from the repository root. Shared catalog helpers live in `scripts/catalog.py`. Everything else is grouped by the layer it writes. v3 is the full public-page scan. v4 is the profile that adds search fillability, location, and legal facts.

## Profiles (`scripts/profiles/`)

v3 is one public-page record per catalog broker. v4 starts from that record.

| Command | Writes |
| --- | --- |
| `python scripts/profiles/scan_v3.py` | `data/v3/brokers.json` and `data/v3/brokers.csv`. This is the full public-page scan |
| `python scripts/profiles/scan_v3.py --compile-only` | Rebuilds those files from `research/evidence/v3.jsonl`. Rebuilding every record can change rows that were already kept |
| `python scripts/profiles/scan_v4.py --compile-only` | Rebuilds `data/v4/brokers.json`, `data/v4/brokers.csv`, `data/v4/removal-blockers.csv`, and `docs/v4.md` from the v3 records and `research/evidence/v4.jsonl` |
| `python scripts/profiles/scan_v4.py --search-missing` | Reads homepages whose public search form is still unknown, then republishes v4 |
| `python scripts/profiles/scan_v4.py --legal-missing` | Reads unread privacy and terms pages when robots.txt allows, then republishes v4 |
| `python scripts/profiles/scan_v4.py --network` | Looks up public DNS and the site certificate, and reads removal-URL response headers when robots.txt allows |
| `python scripts/profiles/scan_v4.py --network-extra` | Adds IPv6, SPF, DMARC, DKIM selectors, CAA, DNSSEC, ASN, certificate dates, RDAP registration, and removal-response security headers |
| `python scripts/profiles/scan_v4.py --captcha` | Re-reads removal and search pages for a CAPTCHA widget. A browser opens when the static page has no widget and the page has a removal form, a stored CAPTCHA, or a public search form |
| `python scripts/profiles/scan_v4.py --captcha-detail` | Reads public widget settings (checkbox, invisible, score, theme, action, script) where a CAPTCHA provider is already stored |
| `python scripts/profiles/scan_v4.py --form-detail` | Re-reads stored search and removal forms for labels, limits, autocomplete, the submit button, and hidden field names. Nothing is submitted |

## Research (`scripts/research/`)

Builds and refreshes the v1 evidence layer.

| Command | Writes |
| --- | --- |
| `python scripts/research/fetch_evidence.py --batch all` | `research/evidence/url-evidence.jsonl` |
| `python scripts/research/build_dataset.py` | `data/v1/broker-opt-outs.enriched.json` and the generated broker tables |
| `python scripts/research/validate_dataset.py` | Check only |
| `python scripts/research/freshness_check.py` | `data/v1/freshness-report.json` |
| `python scripts/research/browser_inspect.py` | Headless read of a page. No form is submitted |
| `python scripts/research/apply_browser_pass.py` | Merges a browser pass into the v1 records |
| `python scripts/research/fetch_registries.py` | `data/v1/registry-matches.json` |
| `python scripts/research/rescrape_fields.py` | `data/v1/field-fills.json` |
| `python scripts/research/map_brokers.py` | `data/v1/broker-maps.json`, liveness and email CSVs |

## Automation (`scripts/automation/`)

Hand-reviewed v2 specs under `data/v2/brokers/`. v3 is the default record for every broker.

| Command | Writes |
| --- | --- |
| `python scripts/automation/priority.py` | `data/v2/priority.csv` |
| `python scripts/automation/bootstrap_v2.py` | Starter `data/v2/brokers/<id>.yaml` files |
| `python scripts/automation/validate_v2.py` | Check only |
| `python scripts/automation/build_v2.py` | `data/v2/brokers.json` and `data/v2/brokers.csv` |
| `python scripts/automation/search_test.py` | Submits one synthetic search on each fully automatable broker. Does not consult robots.txt. Skips a broker whose terms prohibit automation unless `--include-prohibited` is set. Writes `research/evidence/search-tests.jsonl` and `data/v4/search-tests.csv`. `python scripts/profiles/scan_v4.py --compile-only` copies the latest line for each id onto `search_test` |

A finished map log can be republished with `python scripts/research/map_brokers.py --compile-only`.
