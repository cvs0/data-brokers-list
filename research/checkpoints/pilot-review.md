# Pilot review

The pilot fetched 21 catalog URLs on 2026-10-01 before the rest of the catalog: people-search brands, shared portals, marketing and choice sites, Canadian and UK entries, credit-reporting pages, and government guidance.

## What the pilot changed

- A direct GET that returns a Cloudflare interstitial or a short 401/403/429 body stays `blocked`. That status is an access observation. It is not proof the URL is dead, and it is not proof a residential proxy is required.
- Pages under about 400 characters of visible text are shells. Equifax, the PeopleConnect suppression center, and the National Do Not Call Registry loaded this way. Their workflows were not copied from catalog notes.
- Site-search boxes and the GOV.UK feedback widget are not treated as the request form.
- A OneTrust or similar script on a page is not treated as a confirmed web form unless the request host is that vendor's form host or real request fields were parsed.
- CAPTCHA is recorded when a widget marker or visible text says so. A bare script string is not enough to call it always required.
- Login is recorded when a password field is parsed or the page says the person must sign in to submit. A header "Sign in" link is not enough.
- Postal mail is recorded from request-mailing language, not from a footer PO Box.
- `inspection_channel` distinguishes a research-environment GET from a later rendered reading. Rendered readings are overrides. They are not CAPTCHA bypasses.
- No pilot URL was submitted, and none is `end_to_end_tested`.

## Pilot access map

Readable GET text: Spokeo, Acxiom, LiveRamp, LexisNexis, Radaris, The Trade Desk, DMAchoice, YourAdChoices Canada, YourOnlineChoices, Canada DNCL, GOV.UK open register.

Shell or challenge on the research GET: BeenVerified and the brands that share its URL, Whitepages, TruthFinder, TruePeopleSearch, FastPeopleSearch, PeopleConnect suppression, Equifax, California DROP, National Do Not Call Registry.

Access denied on the research GET: OptOutPrescreen.

Radaris's fetched page is a court-order domain transfer, not a live opt-out form.
