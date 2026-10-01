# Sources and CSV columns

`opt-outs.csv` is the catalog. It has **968** data rows. Link checks recorded in `http_status` and in the notes were made on **2026-10-01**. The 211 rows added after the original 704 came from PersProtect URLs that were not already listed, The Markup / CalMatters pages that were not already listed, privacy-rights URLs in the California Privacy Protection Agency data broker registry, and official portals that were not already listed: the National Do Not Call Registry, DMAChoice, the Digital Advertising Alliance opt-out, and California DROP. A later pass added 53 rows from company opt-out and privacy pages and from official choice sites: YourOnlineChoices (EDAA), YourAdChoices control, YourAdChoices Canada, the Network Advertising Initiative opt-out instructions, Canada's National Do Not Call List, and the UK open electoral register. Dead domains and HTTP 404/410 pages were left out.

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
| `cppa` | California Privacy Protection Agency data broker registry privacy-rights URL. See [NOTICE](NOTICE). |
| `manual` | Official opt-out or privacy-request page recorded from the publisher. Includes the FTC Do Not Call Registry, DMAChoice, YourAdChoices, company opt-out pages, YourOnlineChoices (EDAA), YourAdChoices Canada, the Network Advertising Initiative instructions, Canada's National Do Not Call List, and the UK open electoral register. |

A row can list more than one source, for example `persprotect;markup`.

## Categories

| Category | File | Entries |
| --- | --- | ---: |
| `people-search` | [brokers/people-search.md](brokers/people-search.md) | 141 |
| `background-checks` | [brokers/background-checks.md](brokers/background-checks.md) | 52 |
| `marketing` | [brokers/marketing.md](brokers/marketing.md) | 103 |
| `credit-financial` | [brokers/credit-financial.md](brokers/credit-financial.md) | 15 |
| `public-records` | [brokers/public-records.md](brokers/public-records.md) | 76 |
| `other` | [brokers/other.md](brokers/other.md) | 581 |
