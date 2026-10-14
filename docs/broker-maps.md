# Broker page maps

These maps were built for the catalog on the check date below. Rows added or removed in `opt-outs.csv` after that date are not in this file until the map is rebuilt.

Check date: 2026-10-12T23:00:35+00:00. Catalog rows: 1122. Domains crawled: 1122.

Public pages only. Homepage, catalog URL, same-site privacy/opt-out/contact/legal/about links, robots.txt, and a bounded sitemap slice. robots.txt honored. No form submitted, no login, no CAPTCHA. A bot wall is blocked, not dead. People-search listing URLs are counted by path prefix and not stored. Dead means the hostname does not resolve, the domain is parked, or both the homepage and the catalog URL are gone. Rows that list the same catalog domain share one page map.

`opt-outs.csv` was not rewritten. HTTP 403, 429, and challenge pages stay `blocked`. They are not dead pages.

Page maps are stored once per domain in [`data/v1/broker-maps.json`](../data/v1/broker-maps.json) under `domains`. Catalog rows point at that domain. One row per broker is in [`data/v1/broker-liveness.csv`](../data/v1/broker-liveness.csv). Every published address is in [`data/v1/broker-emails.csv`](../data/v1/broker-emails.csv). Rebuild with `python scripts/research/map_brokers.py`. A finished log can be republished with `python scripts/research/map_brokers.py --compile-only`.

## Liveness

| Status | Brokers |
| --- | ---: |
| `alive` | 929 |
| `blocked` | 47 |
| `opt_out_missing` | 10 |
| `dead` | 4 |
| `robots_disallowed` | 124 |
| `error` | 8 |

Brokers with at least one published email: **578** of 1122.

`extracted` means the address is on the broker's own domain (or a known parent domain) and the local part is a privacy, opt-out, or removal mailbox. `uncertain` is a generic mailbox such as info@ or support@, or a privacy address on some other domain, and needs a look before you send anything.

## Dead

| Broker | Domain | Catalog URL | What we saw |
| --- | --- | --- | --- |
| Blue Action Inc. | `blueaction.io` | https://blueaction.io/do-not-sell/ | homepage and catalog URL are gone |
| Locate-Friend | `locate-friend.com` | https://locate-friend.com/optout | catalog hostname does not resolve |
| Nuwber | `nuwber.com` | https://nuwber.com/removal/link | catalog hostname does not resolve |
| PeopleWhiz network (azerch.com) | `azerch.com` | https://www.azerch.com/Policies/Privacy | domain is parked |

## Opt-out URL missing

| Broker | Domain | Catalog URL | What we saw |
| --- | --- | --- | --- |
| 5X5 US, LLC | `5x5coop.com` | https://5x5data.com/dsr-out/ | catalog URL returned 404 or 410; the site itself still responded |
| Absolute People Search | `absolutepeoplesearch.com` | https://absolutepeoplesearch.com/optout | catalog URL returned 404 or 410; the site itself still responded |
| ArrestFacts (BeenVerified-related) | `arrestfacts.com` | https://arrestfacts.com/ng/control/privacy | catalog URL returned 404 or 410; the site itself still responded |
| AudiencePoint Inc. | `audiencepoint.com` | https://audiencepoint.com/cpra/ | catalog URL returned 404 or 410; the site itself still responded |
| FindMugshots | `findmugshots.com` | https://www.findmugshots.com/google_remove.php | catalog URL returned 404 or 410; the site itself still responded |
| Lotame | `lotame.com` | https://www.lotame.com/about-lotame/privacy/lotames-opt-out/ | catalog URL returned 404 or 410; the site itself still responded |
| Outward Media, Inc. | `outwardmedia.com` | https://outwardmedia.com/do-not-sell | catalog URL returned 404 or 410; the site itself still responded |
| PrivateNumberChecker | `privatenumberchecker.com` | https://www.privatenumberchecker.com/removal-request | catalog URL returned 404 or 410; the site itself still responded |
| ReversePhoneLookup (Intelius/PeopleConnect) | `reversephonelookup.com` | https://www.reversephonelookup.com/remove.php | catalog URL returned 404 or 410; the site itself still responded |
| Trulia, LLC | `trulia.com` | https://support.trulia.com/hc/en-us/articles/221366928 | catalog URL returned 404 or 410; the site itself still responded |

## Likely dead

None in this pass.

## Fetch errors

| Broker | Domain | Catalog URL | What we saw |
| --- | --- | --- | --- |
| CocoFinder | `cocofinder.com` | https://cocofinder.com/remove-my-info | ReadTimeout: HTTPSConnectionPool(host='cocofinder.com', port=443): Read timed out. (read timeout=18) |
| CocoFinder.net | `cocofinder.net` | https://cocofinder.net/remove-my-info | ReadTimeout: HTTPSConnectionPool(host='cocofinder.com', port=443): Read timed out. (read timeout=18) |
| FastPeopleSearch.io | `fastpeoplesearch.io` | https://fastpeoplesearch.io/remove-my-info | ReadTimeout: HTTPSConnectionPool(host='fastpeoplesearch.io', port=443): Read timed out. (read timeout=18) |
| NumLooker | `numlooker.com` | https://numlooker.com/remove-my-info | ReadTimeout: HTTPSConnectionPool(host='numlooker.com', port=443): Read timed out. (read timeout=18) |
| PhoneNumberInfo.us | `phonenumberinfo.us` | https://phonenumberinfo.us/contact.php | catalog URL returned HTTP 520 |
| PublicRecordsNow (Confi-Chek) | `publicrecordsnow.com` | https://www.publicrecordsnow.com/static/view/optout/ | TooManyRedirects: Exceeded 30 redirects. |
| TruePeopleSearch.net | `truepeoplesearch.net` | https://truepeoplesearch.net/remove-my-info | ReadTimeout: HTTPSConnectionPool(host='truepeoplesearch.net', port=443): Read timed out. (read timeout=18) |
| Voxtur Data Services | `benutech.com` | https://www.benutech.com | SSLError: HTTPSConnectionPool(host='benutech.com', port=443): Max retries exceeded with url: / (Caused by SSLError(SSLError(1, '[SSL: TLSV1_UNRECOGNIZED_NAME] tlsv1 unrecognized name (_ssl.c:1006)'))) |

## Redirects

83 catalog URLs loaded after a redirect. Same-site path changes: 73. Other host: 10.

| Broker | Catalog URL | Final URL |
| --- | --- | --- |
| DSPolitical, LLC | https://www.winwithoptimal.com/privacy-policy/ | https://www.onemagnify.com/privacy-policy |
| H1 Insights, Inc. | https://www.h1.co | https://h1.com/ |
| Live Data Technologies, Inc. | https://www.livedatatechnologies.com/opt-out | https://www.livedatatech.com/opt-out |
| PublicRecordCom, LLC | https://publicrecord.com/privacy-rights/ | https://validate.perfdrive.com/?ssa=68ef2e19-c950-4af2-8113-b1a4cb269b9f&ssb=26244221262&ssc=https%3A%2F%2Fpublicrecord.com%2F&ssi=090349... |
| Quorum Data, Inc | https://quorum.inc/privacy | https://app.quorum.live/privacy |
| Reklaim Ltd. | https://www.reklaimyours.com/privacy-policy | https://reklaimyou.com/privacy |
| Samba TV, Inc. | https://www.samba.tv/users/privacy-policy | https://samba.com/privacy-center/privacy-policy |
| TeamUnify, LLC | https://www.teamunify.com/team/mdest/page/safe-sport/photo-opt-out | https://www.eagleswimteam.com/page/safe-sport/photo-opt-out |
| TowerData, Inc. | https://www.towerdata.com/ccpa-form | https://privacyportal.onetrust.com/webform/b38ccfa3-c14a-451e-bf1d-974d1e278b7c/6e9b1df1-280c-4754-b92c-02f50ae1f1f4 |
| TruthFinder (PeopleConnect) | https://www.truthfinder.com/opt-out/ | https://suppression.peopleconnect.us |

