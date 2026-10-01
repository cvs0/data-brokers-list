# Findings

This pass researched all **968** catalog rows and **936** unique request URLs. No privacy request was submitted. Nothing is end-to-end tested. `observed_turnaround` is null everywhere.

`opt-outs.csv` was not edited. Stable ids live in `data/v1/catalog-ids.csv` and on each enriched record. Catalog notes and source names are preserved under `catalog` and `catalog_claims`. They were not treated as proof.

## What the counts mean

| Research status | Records | Meaning |
| --- | ---: | --- |
| verified | 160 | Fetched page stated both a request type and a concrete submission path, such as a parsed form, a DSAR form, or published step-by-step instructions. |
| partial | 556 | The page was readable, or only a shell loaded, and a removal workflow is still incomplete. Most privacy-policy URLs are here. |
| blocked | 238 | Cloudflare challenge, HTTP 403/429, timeout, or another access failure. The URL is not marked dead. A proxy was not tested. |
| unresearched | 0 | Every catalog URL was fetched. |
| not_applicable | 14 | The opt-out URL now shows that the domain was transferred by court order. There is no live form. |

Verification level: **180** workflow partially inspected (form fields or an early wizard step parsed, not submitted), **550** page inspected, **238** source documented only. Direct GET content: **613** readable, **210** bot-challenge, **106** empty shell, **34** error, **5** non-HTML.

Automation class: **185** assisted, **283** manual, **17** not applicable, **483** unknown. **Zero** unattended automation candidates. A parsed form still had a human step, or CAPTCHA and email confirmation were not ruled out.

Entry type is `unknown` for **900** rows. The catalog category and a CPPA registry source were not treated as a finding that the company is a data broker. Ten consumer-reporting hosts were labeled from the official site plus the page text. Thirty-three pages were labeled people-search sites from their own wording.

## Three different counts

- **968 catalog entries.** One row per name and URL, including sister brands.
- **936 unique request URLs.** Ten shared-URL groups cover 42 rows. The largest are the BeenVerified opt-out URL, the PeopleConnect suppression portal, and a TruthFinder opt-out path. A shared URL is a shared entry point. It does not by itself prove one submission covers every brand.
- **Verified removal coverage is much smaller than 160.** Forty-two records are public-listing flows. One hundred twenty-six are DSAR-style portals. Three hundred ninety-four readable pages are privacy policies. A policy that names a right and an email is partial, not a confirmed removal.

## Shared workflows

PeopleConnect's suppression page says the tool applies to public data from a name search on family people-search sites, and that it does not apply to user data or to Classmates.com. The research GET only returned the page title. Form fields are still unknown.

OptOutPrescreen states that a firm-offer opt-out, if the file is found, covers Equifax, Experian, Innovis, and TransUnion. It does not delete the rest of those files. The research GET was HTTP 403. That is an access observation, not evidence that a residential proxy is required.

Arrests.org state portals cluster into two WPForms families (20 and 9 URLs) after form ids are stripped. Each state URL is still a separate submission. A CAPTCHA marker was present. Removing a listing there does not erase the government arrest record.

Fourteen opt-out URLs, including Radaris, Rehold, and several Veriforia and related domains, now display a court-order domain transfer. They are not build targets.

BeenVerified, TruthFinder, TruePeopleSearch, MyLife, PeopleFinders, FamilyTreeNow, SmartBackgroundChecks, Nuwber, ZoomInfo, 192.com, and PimEyes were blocked or did not expose a form in the fetched HTML. Catalog notes about their CAPTCHAs and email steps stay in `catalog_claims` only.

## Geographic and product limits

Documented eligibility was recorded only when the page stated it. US opt-out language was not copied onto pages that never mentioned geography. Canada’s National Do Not Call List and the UK open register are official choice or public-record processes, not broker listing deletions. California DROP is a real one-to-many deletion path for California residents and registered brokers; it was not matched broker-by-broker to this catalog. Cookie and device tools (The Trade Desk, YourAdChoices Canada, YourOnlineChoices) are not server-side removal adapters.

## First build

The 25 workflows in `data/v1/automation-shortlist.json` are the implementation queue. Start with Spokeo, CheckPeople, CocoFinder, the GladIKnow form family, and USPhoneBook. Hold PeopleConnect, Whitepages, and FastPeopleSearch until the blocked or unopened steps are inspected in a normal browser. Do not automate Equifax SSN fields or LiveRamp identity documents.
