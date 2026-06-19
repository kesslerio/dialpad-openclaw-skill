# Attio schema reference (for Dialpad enrichment adapters — S1/U1)

Discovered 2026-06-18 against the live Attio REST API (`https://api.attio.com/v2`) using
`ATTIO_API_KEY` from `~/.config/systemd/user/secrets.conf`. The claude.ai Attio MCP was
returning 401 at discovery time — do not rely on it; the adapters call REST directly.

## Correction to the plan assumption

The pipeline is the **standard `deals` object** (object_id `bd6e5162-47bf-46ac-81fb-632ee877cd6a`),
NOT a custom object or List. The earlier "0 rows on `deals`" was solely the MCP's broken token,
not an empty/absent object. KTD5 / U1's "likely a custom object/list" assumption is retired.

## Objects present

`subscriptions, workspaces, people, users, orders, companies, deals, test_projects, locations`

## `deals` attribute slugs the adapters use

| Need | Slug | Type |
|---|---|---|
| Deal name | `name` | text |
| Stage (customer vs prospect signal) | `stage` | status |
| Owner | `owner` | actor-reference |
| Company link | `associated_company` | record-reference |
| People link | `associated_people` | record-reference |
| Deal value | `value` | currency |
| Primary interest | `primary_interest` | select |
| Lead priority | `ai_lead_priority` | select |
| **Demo date (calendar fallback, precise)** | `demo_scheduled_at` | timestamp |
| Demo date (date-only variant) | `demo_scheduled_date` | date |
| Demo lifecycle | `demo_booked_at`, `demo_completed_at`, `demo_no_show_at`, `demo_canceled_at` | timestamp |
| Last outreach | `last_outreach_at` | date |

## Phone → person → deal resolution (U2 path)

Inbound SMS gives only a phone number. Phone numbers live on the **`people`** object, not on
`deals`. Resolution for the CRM adapter:

1. Search `people` by phone number (people standard `phone_numbers` attribute).
2. From the matched person, follow to their associated deal (via the deal's `associated_people`
   record-reference, or the person's deals back-reference).
3. Map the deal's `stage` / `owner` / `associated_company` / `name` / `value` into the CRM
   context contract, and surface `demo_scheduled_at` for the calendar adapter (U3) to reuse.

If no person matches the phone, the adapter returns `{usable: false, status: "not_found"}`.

## Auth

`Authorization: Bearer ${ATTIO_API_KEY}` (64-char token). `ATTIO_WORKSPACE_ID` and
`ATTIO_ACCESS_TOKEN` also exist in secrets.conf if needed.
