# Sources and CSV columns

`opt-outs.csv` is the catalog. It has **704** data rows. Link checks recorded in `http_status` and in the notes were made on **2026-10-01**.

The Markdown files in `brokers/` repeat `name`, `url`, and `notes` for reading. They do not add URLs.

## Columns

| Column | Meaning |
| --- | --- |
| `name` | Broker or site name. |
| `category` | One of `people-search`, `background-checks`, `marketing`, `credit-financial`, `public-records`, `other`. |
| `url` | Opt-out or privacy-request URL. Not invented for this file. |
| `notes` | What kind of page it is, mail or account hints, and the 2026-10-01 check result when that result was not a normal success. |
| `domain` | Registrable or primary domain for the broker. |
| `http_status` | HTTP status from the 2026-10-01 automated check. `403` and `429` usually mean a bot wall; open the URL in a browser. |
| `sources` | Where the URL was taken from. Values are combined with semicolons. |

## `sources` values

| Value | Dataset |
| --- | --- |
| `persprotect` | PersProtect Data Broker Opt-Out List, CC BY 4.0. See [NOTICE](NOTICE). |
| `markup` | The Markup and CalMatters opt-out page dataset, Apache 2.0. See [NOTICE](NOTICE). |
| `manual` | URL confirmed from the company's own site or another public page, recorded by hand. |

A row can list more than one source, for example `persprotect;markup`.

## Categories

| Category | File | Entries |
| --- | --- | ---: |
| `people-search` | [brokers/people-search.md](brokers/people-search.md) | 112 |
| `background-checks` | [brokers/background-checks.md](brokers/background-checks.md) | 40 |
| `marketing` | [brokers/marketing.md](brokers/marketing.md) | 69 |
| `credit-financial` | [brokers/credit-financial.md](brokers/credit-financial.md) | 9 |
| `public-records` | [brokers/public-records.md](brokers/public-records.md) | 65 |
| `other` | [brokers/other.md](brokers/other.md) | 409 |
