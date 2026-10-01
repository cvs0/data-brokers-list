# Workflow groups

Use `data/v1/workflow-groups.json` to pick one implementation target.

A `shared_request_url` group is one published entry point. Sharing that URL does not, by itself, mean one submission removes every brand. `single_request_covers_members` is true only when an official page says so.

PeopleConnect's suppression page says the tool does not apply to Classmates.com or to user data. That exception stays on the portal even though those brands share the suppression URL in the catalog.

A `shared_form_signature` group is one form shape. Each member URL is still its own submission. The arrests.org families remove a commercial repost of an arrest record. They do not erase the government record.

Parent companies in `data/v1/parent-companies.json` are verified only for PeopleConnect, and only for the suppression URL the page describes. Other brand families are `catalog_leads_unverified` because the relationship comes from the catalog name, not from an official page.
