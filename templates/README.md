# Request templates

Email and letter bodies for privacy requests, one file per legal basis. `removal.channels[].template_id` in `data/v2/brokers/*.yaml` names one of these files, and `scripts/build_frame_export.py` copies them into `data/v2/templates.json` so an app can render them without parsing Markdown.

Each file has YAML front matter:

| Key | Meaning |
| --- | --- |
| `id` | File name without `.md`. Referenced by `template_id`. |
| `basis` | The `legal_bases[].basis` value this template invokes. |
| `jurisdictions` | ISO 3166 codes (`US-CA`, `CA`, `CA-QC`, `EU`, `GB`, `ANY`) whose residents can use it. |
| `language` | `en` or `fr-CA`. |
| `statutory_response_days` | The deadline the law sets for a reply, or `null` when there isn't one. |
| `subject` | Email subject line. |
| `placeholders` | Every `{{name}}` used in the body. Names are the canonical field names from the schema plus `broker_name`, `date`, `profile_urls` and `agent_*`. |

Rules for anyone sending these:

- Send only the details the broker needs to find the person. The templates ask for what's typical; drop any line the broker's own form doesn't ask for.
- Never attach an identity document unless the broker requires one and the person approved it. Redact everything but name, photo and expiry.
- Requests from an authorized agent add `authorized-agent-cover.md` and the signed permission.
- These are templates, not legal advice. Each one cites the right it relies on so the broker can route it.
