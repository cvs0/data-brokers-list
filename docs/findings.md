# Findings

This pass kept the **1122** catalog rows and their stable ids. It added a headless-browser read of the automation shortlist and of catalog URLs whose research GET was blocked, a freshness recheck of every catalog URL, search-page observations, Canadian official-page notes, and verification playbooks. No privacy request was submitted. No account was created. No identity document was uploaded. No SMS was sent. No CAPTCHA was solved.

`opt-outs.csv` columns are unchanged. Its `http_status` values remain the 2026-10-01 catalog check. Newer HTTP results are in `data/v1/freshness-report.json`.

## Research status before and after

| Status | Before | After |
| --- | ---: | ---: |
| verified | 184 | 201 |
| partial | 651 | 717 |
| blocked | 273 | 190 |
| not_applicable | 14 | 14 |

Verification level after this pass: 243 workflow partially inspected, 689 page inspected, 190 source documented.
Browser outcomes on the catalog rows whose URL was opened for the shortlist or the blocked retry: 114 readable, 190 still a challenge, 4 HTTP errors, 4 timeouts, 1 other error. Shared URLs are counted once per row.

A blocked URL that stayed blocked was opened again in a headless browser. If that browser still showed a challenge, HTTP 403, or timeout, the record stays blocked. `proxy_required` was not set to true. A browser that rendered the page is also not proof that a residential proxy is unnecessary for a plain HTTP client.

## Shortlist

The 30 shortlist URLs were opened in headless Chromium. Submit buttons were not clicked, so a CAPTCHA that appears only after submit was not observed. Readable: CheckPeople, GladIKnow, USPhoneBook, NumLookup, SearchUSAPeople, PeopleConnect suppression portal, Arrests.org state request portals, Sterling, Acxiom, LexisNexis Risk Solutions, Equifax, LiveRamp, California DROP, Social Catfish, Sync.me, Apollo Interactive, UK open electoral register, Canada National Do Not Call List, DMAchoice, Elections Canada, Elections Quebec, Elections BC, Canada Post, CMA Do Not Mail. Not readable: Spokeo (challenge), CocoFinder (challenge), PublicRecords.info (challenge), FastPeopleSearch (challenge), Whitepages (challenge), OptOutPrescreen (challenge). Per-workflow notes are on each shortlist candidate and in data/v1/verification-playbooks.json.

## Freshness

Direct GET recheck on 2026-10-01T23:11:41+00:00. Drift counts: `{"blocked": 259, "moved_other_host": 10, "moved_same_site": 43, "status_changed": 10, "unchanged": 767, "unresolved_or_network_error": 1}`.

Moved means the final URL's host or path differs after ignoring a leading www and a trailing slash. Blocked means the recheck saw a challenge or HTTP 401/403/429/503. That is an access observation, not a dead page and not a proxy finding. Unresolved hostnames are network errors, not HTTP 404s. The catalog CSV was not rewritten.

## Search signals

Homepage observations, one per searched domain: 67 public search forms with no result page opened, 8 pages whose copy called the search free, 29 pages with no parsed search form, and 84 homepages that did not render. No people-search query was submitted. `exposure_check` stays unknown when a result page was not opened. `search_observation` records whether the landing page showed a search form, a CAPTCHA marker, login copy, or paywall copy.

## Canada

51 catalog rows with a Canadian name, domain, or URL were opened in the headless browser. French-language links were followed when the page exposed one, including Canada Post, the National Do Not Call List, Élections Québec, and the Office of the Privacy Commissioner of Canada. A French page load is not a second workflow unless that page's text was quoted.

Eligibility was copied only when the loaded page stated it. Elections BC's page says a person who believes distributed voter lists put their privacy or security at risk can apply to omit their name and address from those lists. It does not say the voter record is deleted. The Office of the Privacy Commissioner page says to contact the organization's privacy officer before a complaint, and describes the federal and provincial paths. Environics Analytics' privacy policy says its practices are consistent with PIPEDA as well as GLBA, HIPAA, CCPA, and GDPR. That is a policy statement, not a finding that every Canadian resident can use one form.

The PIPEDA brief on priv.gc.ca loaded. Its captured text defines personal information and which organizations PIPEDA covers. It does not state a consumer broker opt-out. The CRTC CASL page returned a challenge. Elections Alberta's guessed registration URL was HTTP 404. Elections Saskatchewan's guessed URL was HTTP 404. Elections Ontario returned HTTP 406. Elections PEI showed a Radware captcha page. Several other provincial election and commissioner URLs timed out, returned 404, or were blocked. Those URLs were not added to `opt-outs.csv`. Outcomes are in `data/v1/canadian-coverage.json`.

## Unique workflows

Shared request URLs and parsed-form families are indexed in `data/v1/workflow-groups.json`. One shared URL is one entry point. PeopleConnect's page says the suppression tool does not apply to Classmates.com or to user data; that exception is preserved. Arrests.org form families remove a commercial repost, not the government record. Catalog parenthetical brand hints remain unverified in `parent-companies.json`.

## Origin mirror

This agent could not update the Origin mirror at https://cursor.com/codebase/cvs0/data-brokers-list. A fetch of that URL returned HTTP 500, and this environment has no Origin write tool. GitHub `main` before this branch is commit `18faa83`, with 1122 catalog rows and `data/v1/`. The task described Origin as still on the early catalog of about 704 rows. That drift was not re-read from Origin.

To sync after this pull request merges, fast-forward the Origin codebase to GitHub `main`. Confirm `opt-outs.csv` has 1122 data rows and that `data/v1/broker-opt-outs.enriched.json` is present. Do not copy a third-party opt-out guide into the mirror.

## Still unresolved

- Headless-browser challenges and HTTP 403/429 responses. A residential proxy was not tested.
- Screens behind submit buttons, email links, accounts, SMS, and CAPTCHA.
- Whether a free search form leads to a full listing without payment. Result pages were not opened.
- Provincial voter and commissioner pages that did not load in this environment.
- Sister brands that share only a catalog-name hint.
- End-to-end acknowledgement, broker confirmation, and removal. None were tested.
