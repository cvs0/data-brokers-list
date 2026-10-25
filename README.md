# Data broker opt-out list

A catalog of **734** data broker, people-search, and consumer-reporting opt-out or privacy-request pages. Each row is a real URL, checked on **2026-10-01**.

This is a directory of links, not a removal service. Read the [disclaimer](DISCLAIMER.md) before you use it. Pages move, forms ask for different ID, and a successful request on one site does not remove you from the rest.

## Start here

If you only do one sitting, use these. URLs are copied from [`opt-outs.csv`](opt-outs.csv). Sister brands that share a portal are listed once.

| Broker | Opt-out / privacy URL |
| --- | --- |
| BeenVerified | https://www.beenverified.com/app/optout/search |
| Spokeo | https://www.spokeo.com/optout |
| Whitepages | https://www.whitepages.com/suppression-requests |
| Intelius (PeopleConnect) | https://suppression.peopleconnect.us/ |
| InstantCheckmate (PeopleConnect) | https://suppression.peopleconnect.us/ |
| TruthFinder (PeopleConnect) | https://www.truthfinder.com/opt-out/ |
| CheckPeople | https://checkpeople.com/opt-out |
| MyLife | https://www.mylife.com/privacyrequest |
| TruePeopleSearch | https://www.truepeoplesearch.com/removal |
| FastPeopleSearch | https://www.fastpeoplesearch.com/optout |
| PeopleFinders (Confi-Chek) | https://www.peoplefinders.com/opt-out |
| SmartBackgroundChecks | https://www.smartbackgroundchecks.com/optout |
| FamilyTreeNow | https://www.familytreenow.com/optout |
| USPhoneBook | https://www.usphonebook.com/opt-out |
| Acxiom | https://www.acxiom.com/optout/ |
| Epsilon | https://legal.epsilon.com/dsr/ |
| LiveRamp | https://liveramp.com/privacy/my-privacy-choices/ |
| The Trade Desk | https://adsrvr.org/ |
| ZoomInfo Technologies, LLC | https://privacyrequest.zoominfo.com/remove/verify |
| LexisNexis | https://consumer.risk.lexisnexis.com/opt |
| Checkr | https://checkr.com/legal/privacy-policy |
| Sterling | https://sterling.com/privacyrequest/ |
| Equifax | https://myprivacy.equifax.com/opt-in-opt-out/personal-info |
| Experian | https://www.experian.com/privacy/opting_out |
| TransUnion | https://www.transunion.com/consumer-privacy |
| OptOutPrescreen (Experian/Equifax/TransUnion/Innovis) | https://www.optoutprescreen.com/ |
| PimEyes | https://pimeyes.com/en/opt-out |
| 192.com | https://www.192.com/c01/new-request/ |
| National Do Not Call Registry | https://www.donotcall.gov/ |
| Canada National Do Not Call List | https://www.lnnte-dncl.gc.ca/en/ |
| Canada411 | https://www.canada411.ca/help.html?key=faq |
| Canada Post | https://www.canadapost-postescanada.ca/cpc/en/support/kb/receiving/delivery-faq/how-to-stop-receiving-advertising-mail.page |
| CMA Do Not Mail | https://www.thecma.ca/resources/consumer-centre/get-less-print-mail |
| Elections Canada | https://www.elections.ca/content.aspx?section=vot&dir=reg/des&document=index&lang=e |
| UK open electoral register | https://www.gov.uk/electoral-register/opt-out-of-the-open-register |
| DMAChoice | https://www.dmachoice.org/ |
| Digital Advertising Alliance opt-out | https://optout.aboutads.info/ |
| YourOnlineChoices (EDAA) | https://www.youronlinechoices.com/uk/your-ad-choices |
| YourAdChoices Canada | https://www.youradchoices.ca/choices |
| California DROP | https://privacy.ca.gov/drop/ |

Search the CSV before you send extra personal details. Notes say when a page returned HTTP 403 or 429 to an automated check, when email is part of the flow, and when an account may be required.

## Catalog

| Category | File | Entries |
| --- | --- | ---: |
| People search | [brokers/people-search.md](brokers/people-search.md) | 158 |
| Background checks | [brokers/background-checks.md](brokers/background-checks.md) | 68 |
| Marketing | [brokers/marketing.md](brokers/marketing.md) | 122 |
| Credit and financial | [brokers/credit-financial.md](brokers/credit-financial.md) | 18 |
| Public records | [brokers/public-records.md](brokers/public-records.md) | 81 |
| Other | [brokers/other.md](brokers/other.md) | 287 |
| **Total** | [opt-outs.csv](opt-outs.csv) | **734** |

`opt-outs.csv` is the source of truth for links. The Markdown files are tables generated from it. Column definitions and the check date are in [SOURCES.md](SOURCES.md).

The default broker record for a full public-page scan is the v3 profile in [data/v3/brokers.json](data/v3/brokers.json). Notes are in [docs/v3.md](docs/v3.md). The working profile for search, removal, location, and legal facts is v4 in [data/v4/brokers.json](data/v4/brokers.json). Notes are in [docs/v4.md](docs/v4.md). A 2026-10-12 page map of an earlier, larger catalog is in [docs/broker-maps.md](docs/broker-maps.md). Commands that build and check these files are grouped in [scripts/README.md](scripts/README.md).

Earlier layers are still in the repo. Researched workflow records are in [data/v1/broker-opt-outs.enriched.json](data/v1/broker-opt-outs.enriched.json) ([docs/dataset-fields.md](docs/dataset-fields.md), [docs/findings.md](docs/findings.md)). Hand-reviewed automation specs for a short list of brokers are in `data/v2/` ([docs/automation-layer.md](docs/automation-layer.md)). Catalog notes are claims. They are not proof that a removal workflow still works. No request was submitted to produce these files.

## How to use a row

1. Open the URL in a normal browser. Some sites block datacenter checks and still work for people.
2. Search for your own listing first. Send a removal only for a profile you can already see, or use the company's published privacy form.
3. Confirm the email or phone step when the site asks for one.
4. Expect the same person to be listed again later. Brokers re-import public records.

California residents may also have a statewide deletion request through the state privacy agency. That process is separate from the per-company links in this list.

## Sources

URLs were adapted from the PersProtect open dataset (CC BY 4.0), The Markup / CalMatters investigation data (Apache 2.0), and the California Privacy Protection Agency data broker registry, then checked on 2026-10-01. Later rows were taken from company opt-out and privacy pages and from official choice sites, including YourOnlineChoices, YourAdChoices Canada, Canada's National Do Not Call List, and the UK open electoral register. A Canadian pass added directory-removal, advertising-mail, voters-list, telecom do-not-list, and privacy-commissioner pages. A coverage pass compared the catalog with the broker names Optery publishes in its public directory. Opt-out URLs added from that pass were opened on each company's own site. Optery's guide text was not copied. Credit and license details are in [NOTICE](NOTICE).

## License

Original organization and text in this repository are under the [MIT License](LICENSE). Source datasets keep the terms in [NOTICE](NOTICE).
