# Enriched opt-out dataset

`opt-outs.csv` is still the link catalog. Its columns are unchanged. This dataset adds a stable ID and a researched record for every row.

| File | Role |
| --- | --- |
| `data/v1/broker-opt-outs.enriched.json` | Versioned records. Schema version `1.0.0`. |
| `data/schema/broker-opt-out-record.schema.json` | JSON Schema draft 2020-12. |
| `data/v1/catalog-ids.csv` | `id` joined to `name` and `url`. |
| `data/v1/shared-portals.json` | Same-URL portals and parsed-form families. |
| `data/v1/parent-companies.json` | Verified parents, plus unverified name hints. |
| `data/v1/automation-shortlist.json` | First-implementation shortlist. |
| `data/v1/research-progress.json` | Counts from the latest build. |
| `scripts/validate_dataset.py` | ID, type, enum, group, and catalog-link checks. |

Join key: `records[].id` equals `catalog-ids.csv`. `records[].catalog.name` plus `records[].catalog.url` equals the catalog row. `name` + `url` is unique.

## How to read a record

`catalog` and `catalog_claims` repeat the existing row. Notes, the 2026-10-01 HTTP status, source names, and parenthetical brand hints are leads. They are not copied into the research fields unless a fetched page says the same thing.

Research fields use `null` or the string `unknown` when a fact was not established. Boolean fields are `true`, `false`, or `null`. `null` means unknown, not false. Arrays that were not established are `["unknown"]` and do not mix `unknown` with other values.

Empty `required_fields` means no required inputs were parsed. Check `fields_observation` before treating that as "the form has no fields."

## Identity and scope

| Field | Meaning |
| --- | --- |
| `id` | Stable slug of the catalog name. A domain suffix is added only when two names slug together. |
| `identity.entry_type` | `data_broker`, `people_search_site`, `consumer_reporting_agency`, `public_records_source`, `marketing_choice_service`, `regulator_guidance`, `other`, or `unknown`. The catalog category is not this field. |
| `identity.parent_company` | Set only from an official page or a reviewed override. |
| `identity.sister_brands` | Other catalog rows. `same_request_url` means the catalog URL matches. `catalog_name_hint` is not used as verification. |
| `identity.shared_portal_group_id` | Rows that publish the same request URL. |
| `identity.workflow_family_id` | Separate URLs whose fetched HTML exposed the same form fields. |
| `identity.single_request_covers_multiple_brands` | `true` only with `single_request_coverage_evidence`. A shared URL is not enough. |
| `identity.geographic_coverage` | Where the operator says the product or request applies. |
| `identity.documented_request_eligibility` | Who the page says may submit. Kept separate from coverage. |
| `identity.supported_request_types` | `public_listing_suppression`, `deletion`, `sale_sharing_opt_out`, `marketing_opt_out`, `access`, `correction`, `other`, or `unknown`. |

## Discovery

`official_homepage`, `privacy_policy_url`, `request_url`, and `privacy_contact_email` come from the fetched page. A catalog URL is not restated as a verified request URL when the fetch was blocked.

`public_profiles_searchable` and `exposure_check` (`public`, `broker_confirmation_only`, `both`, `unknown`, `not_applicable`) are separate. Search inputs are `name`, `location`, `phone`, `email`, `profile_url`, `other`, or `unknown`. Search login, payment, and CAPTCHA stay `null` unless the fetched page shows that search step. `search_observation` is set when a headless browser opened a public homepage. It records the landing page only. No search query was submitted, so a result-page paywall can stay unknown.

## Request workflow

`submission_methods`: `web_form`, `email`, `official_api`, `postal_mail`, `phone`, `other`, or `unknown`.

`steps[]` is ordered. `observed: true` means this pass saw the redirect or the form in the HTML. Instructions for a later screen, including a confirmation screen, stay `observed: false`. Seeing instructions is not the same as submitting, receiving an acknowledgement, or confirming removal.

`email_verification_mechanism`: `link`, `code`, `both`, `unknown`, or `not_applicable`.

`captcha_requirement`: `always_required`, `conditionally_observed`, or `unknown`. A marker in the fetched HTML is `conditionally_observed` unless the page says the CAPTCHA is required. No marker on a parsed form still leaves the requirement `unknown`, because a later script can add one.

`documented_processing_time` is a sentence from the page. `observed_turnaround` is null in this dataset because no request was submitted.

`identity_document_redaction` is set only when the page itself describes permitted redaction.

## Automation

`automation.classification`: `automation_candidate`, `assisted_workflow`, `manual_workflow`, `not_applicable`, or `unknown`.

`observed_blockers`: `captcha`, `login`, `verification`, `bot_wall`, `geo_restriction`, `inaccessible_page`, `other`. An empty array means none of those were observed, not that the live site is clear.

`direct_access.proxy_required` is `false` only when a direct GET returned readable text. It is `null` when the fetch failed or a bot wall appeared. A single 403 is not evidence that a residential proxy is required.

`implementation_notes` may list input names that were actually parsed. They are not CSS selectors, and no API path is invented from a vendor hostname.

## Verification and maintenance

| Field | Meaning |
| --- | --- |
| `verification.workflow_kind` | `public_listing_form`, `dsar_portal`, `privacy_policy_only`, `cookie_or_device_choice`, `government_or_industry_choice`, `defunct_or_unavailable`, or `unknown`. |
| `verification.inspection_channel` | `http_get` when the research GET returned the text used above. `rendered_reader` when that GET was a shell, challenge, or access denial and a second reader supplied the visible text. `not_inspected` when no page text was used. A rendered reader is not a CAPTCHA bypass and is not evidence that a proxy is required. |
| `verification.browser_inspection` | Optional headless-browser read. Outcomes are `readable`, `challenge`, `http_error`, `timeout`, and `error`. Forms were not submitted and CAPTCHAs were not solved. A challenge is not evidence that a residential proxy is required. |
`verification.browser_inspection` | Optional headless-browser read. `outcome` is `readable`, `challenge`, `http_error`, `timeout`, or `error`. Forms were not submitted and CAPTCHAs were not solved. A `challenge` outcome is not evidence that a residential proxy is required. |
| `verification.research_status` | `verified`, `partial`, `blocked`, `unresearched`, or `not_applicable`. |
| `verification.verification_level` | `source_documented`, `page_inspected`, `workflow_partially_inspected`, or `end_to_end_tested`. |
| `verification.confidence` | `high`, `medium`, `low`, or `none`. |

`verified` means the fetched page stated both a request type and a submission method. It does not mean a broker confirmed deletion.

`not_applicable` means the inspected page is not a live request workflow, such as a domain seized under a court order. A government registry can still be `verified` as a registry, with its own entry type.

`end_to_end_tested` would require an explicitly authorized submission. This pass does not use it. `broker_confirmation` stays null. `independent_removal_verification` describes a check the page tells a person to do; it does not mean that check was performed.

`distinct_suppression_deletion_marketing_outcomes` is `true` only when the page contrasts those outcomes, `false` only when it says they are the same, and otherwise `null`.

## Rebuild

```bash
python3 scripts/fetch_evidence.py --batch pilot
python3 scripts/fetch_evidence.py --batch all
python3 scripts/build_dataset.py
python3 scripts/validate_dataset.py
```

The fetch log is `research/evidence/url-evidence.jsonl`. Completed batches write `research/checkpoints/`. The fetcher skips URLs already in the log. It does not submit forms.
