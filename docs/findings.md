# Findings

This pass researched all **1122** catalog rows and **1090** unique request URLs. The 154 rows added on main after the first research pass were fetched under the same rules and appended. No privacy request was submitted. Nothing is end-to-end tested. `observed_turnaround` is null everywhere.

`opt-outs.csv` on this branch is main's 1122-row catalog. Columns are unchanged. Stable ids for the original rows live in `data/v1/catalog-ids.csv` and on each enriched record. Catalog notes and source names are preserved under `catalog` and `catalog_claims`. They were not treated as proof.

## What the counts mean

| Research status | Records | Meaning |
| --- | ---: | --- |
| verified | 184 | Fetched page stated both a request type and a concrete submission path, such as a parsed form, a DSAR form, or published step-by-step instructions. |
| partial | 651 | The page was readable, or only a shell loaded, and a removal workflow is still incomplete. Most privacy-policy URLs are here. |
| blocked | 273 | Cloudflare challenge, HTTP 403/429, timeout, or another access failure. The URL is not marked dead. A proxy was not tested. |
| unresearched | 0 | Every catalog URL was fetched. |
| not_applicable | 14 | The opt-out URL now shows that the domain was transferred by court order. There is no live form. |

Verification level: **208** workflow partially inspected (form fields or an early wizard step parsed, not submitted), **641** page inspected, **273** source documented only. Direct GET content: **719** readable, **237** bot-challenge, **117** empty shell, **42** error, **7** non-HTML.

Automation class: **212** assisted, **325** manual, **17** not applicable, **568** unknown. **Zero** unattended automation candidates. A parsed form still had a human step, or CAPTCHA and email confirmation were not ruled out.

Entry type is `unknown` for **1042** rows. The catalog category and a CPPA registry source were not treated as a finding that the company is a data broker. Ten consumer-reporting hosts were labeled from the official site plus the page text. Thirty-five pages were labeled people-search sites from their own wording. Official hosts added in this pass include Elections Canada, Elections BC, Élections Québec, Canada Post, the Canadian Marketing Association Do Not Mail service, and the federal and provincial privacy commissioners that were fetched.

## Three different counts

- **1122 catalog entries.** One row per name and URL, including sister brands. Stable ids for the original 968 name-and-URL pairs are unchanged.
- **1090 unique request URLs.** Ten shared-URL groups cover 42 rows. The largest are the BeenVerified opt-out URL, the PeopleConnect suppression portal, and a TruthFinder opt-out path. A shared URL is a shared entry point. It does not by itself prove one submission covers every brand. None of the 154 added rows share a URL with an older row.
- **Verified removal coverage is much smaller than 184.** Forty-four records are public-listing flows. One hundred forty-two are DSAR-style portals. Four hundred sixty readable pages are privacy policies. A policy that names a right and an email is partial, not a confirmed removal.

## Shared workflows

PeopleConnect's suppression page says the tool applies to public data from a name search on family people-search sites, and that it does not apply to user data or to Classmates.com. The research GET only returned the page title. Form fields are still unknown.

OptOutPrescreen states that a firm-offer opt-out, if the file is found, covers Equifax, Experian, Innovis, and TransUnion. It does not delete the rest of those files. The research GET was HTTP 403. That is an access observation, not evidence that a residential proxy is required.

Arrests.org state portals cluster into two WPForms families (20 and 9 URLs) after form ids are stripped. Each state URL is still a separate submission. A CAPTCHA marker was present. Removing a listing there does not erase the government arrest record.

Fourteen opt-out URLs, including Radaris, Rehold, and several Veriforia and related domains, now display a court-order domain transfer. They are not build targets.

BeenVerified, TruthFinder, TruePeopleSearch, MyLife, PeopleFinders, FamilyTreeNow, SmartBackgroundChecks, Nuwber, ZoomInfo, 192.com, and PimEyes were blocked or did not expose a form in the fetched HTML. Catalog notes about their CAPTCHAs and email steps stay in `catalog_claims` only.

## Geographic and product limits

Documented eligibility was recorded only when the page stated it. US opt-out language was not copied onto pages that never mentioned geography. Canada’s National Do Not Call List, the UK open register, Elections Canada, Elections BC, and Élections Québec are official choice or public-record processes, not broker listing deletions. Elections BC’s inspected path omits a name and address from distributed lists; it does not say the voter record is deleted. Canada Post’s inspected phone path and linked database form reduce advertising mail; unaddressed mail is a separate mailbox note. The CMA Do Not Mail page describes the service, and its Register Now form was not opened. The telephone number printed there is the National Do Not Call List. California DROP is a real one-to-many deletion path for California residents and registered brokers; it was not matched broker-by-broker to this catalog. Cookie and device tools (The Trade Desk, YourAdChoices Canada, YourOnlineChoices) are not server-side removal adapters.

Canada411’s fetched page is a help index. It links to “How do I remove my listing?” and the removal steps were not on that page. Canada Pages and White Pages Canada returned bot challenges. Cogeco, Eastlink, TELUS, and SaskTel were access failures (including HTTP 401 and 403). Those URLs are not marked dead.

A search box or newsletter field on a privacy page was not treated as the opt-out. That correction also dropped a site-search form on the PhoneLookup opt-out page, on California Criminal Records Search, and on Texas Arrest Warrants Search. PhoneLookup stays verified from the written account, email, and phone-PIN steps. 411.info, Homeyou, Spectrum Mailing Lists, and SentiLink stay partial because the linked removal or do-not-sell form was not the form in the HTML.

## First build

The 30 workflows in `data/v1/automation-shortlist.json` are the implementation queue. Start with Spokeo, CheckPeople, CocoFinder, the GladIKnow form family, and USPhoneBook. Hold PeopleConnect, Whitepages, and FastPeopleSearch until the blocked or unopened steps are inspected in a normal browser. Do not automate Equifax SSN fields or LiveRamp identity documents. The added Canadian rows are official mail, voter-list, and Do Not Mail processes, not people-search adapters.
