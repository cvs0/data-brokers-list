# Field fills and registry matches

Two overlays sit on top of the v1 evidence layer. Neither rewrites `data/v1/broker-opt-outs.enriched.json`; v1 values always win.

## `data/v1/field-fills.json` (scripts/rescrape_fields.py)

For every catalog row missing one of the key fields (request URL, submission methods, required info, search page, jurisdiction, privacy email, verification, processing time, privacy policy URL), the script reads up to four of the broker's own public pages: the catalog URL, the homepage and the privacy policy.

- robots.txt is honoured for the `data-brokers-list-research` user agent and for `*`. A 401/403 on robots.txt is treated as "disallow all".
- 2 seconds between requests to the same host, 16 workers overall.
- Bot walls, challenge pages and logins are recorded as blocked and skipped. Nothing is submitted, no CAPTCHA is touched, no account is created.

Every filled field records `value`, `source_url`, `checked_at` and `confidence`:

- `extracted`: parsed directly from a form on the page or a sentence that states it ("within 45 days of receiving a verifiable request").
- `uncertain`: a keyword match that needs a human to confirm. Jurisdiction is always `uncertain`, because naming a law isn't proof of where a broker publishes data.

The raw per-page log is `research/evidence/rescrape.jsonl`.

## `data/v1/registry-matches.json` (scripts/fetch_registries.py)

Matches catalog rows to the California Privacy Protection Agency's data broker registry (2026 registrations, https://cppa.ca.gov/data_broker_registry/). The raw CSV is kept in `research/registries/`.

- A match needs the same registrable domain (registrant website or privacy-rights URL vs. catalog domain or URL). Name-only lookalikes go to `review` and are never applied.
- Kept per match: legal name, DBA, website, contact email, privacy-rights URL, the registrant's own delete-request statistics (mean and median days to respond) and its FCRA, minors and precise-geolocation answers.
- A CPPA-registered broker has to process California DROP deletion requests, so the export marks `drop_participant: true` for matches.

Re-run both with:

```
pip install -r requirements.txt
python scripts/fetch_registries.py
python scripts/rescrape_fields.py --workers 16
```
