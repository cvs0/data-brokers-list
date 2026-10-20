# Sources and CSV columns

`opt-outs.csv` is the catalog. It has **710** data rows. Link checks recorded in `http_status` and in the notes were made on **2026-10-01**. A same-day freshness recheck is in `data/v1/freshness-report.json` and `docs/freshness.md`. That recheck did not rewrite `http_status`. A blocked recheck is not a dead page and is not evidence that a residential proxy is required. The 211 rows added after the original 704 came from PersProtect URLs that were not already listed, The Markup / CalMatters pages that were not already listed, privacy-rights URLs in the California Privacy Protection Agency data broker registry, and official portals that were not already listed: the National Do Not Call Registry, DMAChoice, the Digital Advertising Alliance opt-out, and California DROP. A later pass added 53 rows from company opt-out and privacy pages and from official choice sites: YourOnlineChoices (EDAA), YourAdChoices control, YourAdChoices Canada, the Network Advertising Initiative opt-out instructions, Canada's National Do Not Call List, and the UK open electoral register. A Canadian pass added 27 rows from official Canadian pages: Canada411 listing removal, Canada Pages and White Pages Canada removal forms, Canada Post advertising-mail choices, the Canadian Marketing Association Do Not Mail service, Elections Canada and the provincial voters-list pages for Ontario, Quebec, and British Columbia, BC Assessment and MPAC privacy pages, telecom directory do-not-list pages, Canadian background-check privacy pages, and privacy-commissioner pages (the Office of the Privacy Commissioner of Canada, OIPC British Columbia, OIPC Alberta, and Quebec's Commission d'acces a l'information). A coverage pass used the broker names Optery publishes in its public directory (https://www.optery.com/data-brokers/) as a checklist only. Optery's directory is CC BY-NC-SA 4.0, so this catalog does not copy that database, its opt-out guides, or its descriptions. Where a listed broker was missing, the opt-out or privacy URL was taken from the company's own site and checked on 2026-10-01. Dead domains and HTTP 404/410 pages were left out. A 2026-10-18 review moved 14 rows to `data/removed-brokers.csv`, each with a reason: four elections agencies (Elections Canada, Elections Ontario, Elections Quebec, Elections BC), two property-assessment agencies (MPAC, BC Assessment), the FamilySearch genealogy archive, and seven employment-screening platforms (Certn, Checkr, HireRight, First Advantage, Sterling, Backgrounds Online, Triton Canada). They are not people-search sites or data brokers a person opts out of.

On 2026-10-20 Canada Pages (canadapages.com) was returned to the catalog. A browser check found a free people search, a surname in a city, and the removal form at /remove-info.php. An earlier pass had dropped the row because the homepage looked like a publisher site.

On 2026-10-13, 15 InfoTracer-family lookup sites were added after a browser check found a captcha-free name form on each site and `privacy@infotracer.com` on InfoTracer's do-not-sell page. No search was submitted. The same check added ThisNumber after its do-not-sell page told people to email `thisnumber.com@gmail.com` and its homepage phone lookup had no CAPTCHA. A CC BY 4.0 copy of the PersProtect list used as a lead checklist that day is in `research/registries/persprotect-data-brokers.csv`. Optery's directory was used as a name checklist only and was not copied into this repo.

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
| `manual` | Official opt-out or privacy-request page recorded from the publisher. Includes the FTC Do Not Call Registry, DMAChoice, YourAdChoices, company opt-out pages, YourOnlineChoices (EDAA), YourAdChoices Canada, the Network Advertising Initiative instructions, Canada's National Do Not Call List, the UK open electoral register, Canadian directory, advertising-mail, voters-list, telecom do-not-list, and privacy-commissioner pages, and company opt-out pages confirmed while checking Optery's public broker-name list. |

A row can list more than one source, for example `persprotect;markup`.

## Categories

| Category | File | Entries |
| --- | --- | ---: |
| `people-search` | [brokers/people-search.md](brokers/people-search.md) | 138 |
| `background-checks` | [brokers/background-checks.md](brokers/background-checks.md) | 74 |
| `marketing` | [brokers/marketing.md](brokers/marketing.md) | 120 |
| `credit-financial` | [brokers/credit-financial.md](brokers/credit-financial.md) | 16 |
| `public-records` | [brokers/public-records.md](brokers/public-records.md) | 87 |
| `other` | [brokers/other.md](brokers/other.md) | 288 |
