# Troubleshooting

## I do not see the channel/thread in Raven

Check memberships:

- **Raven Workspace Member** exists for expected users.
- **Raven Channel Member** exists for inbox and thread channels.

For route-based flows, ensure users are listed in route `members`.

## Reply does not send to WhatsApp

Check:

- `WhatsApp Raven Bridge Settings.enabled = 1`
- `WhatsApp Raven Bridge Settings.enable_outbound_replies = 1`
- Route membership and `can_reply` for sender
- `conversation.whatsapp_account` or fallback `default_whatsapp_account`
- `frappe_whatsapp` account health/credentials

Also confirm the Raven message owner resolves to a route member Raven User.

## Duplicate messages

Inspect **WhatsApp Raven Message Link** records:

- `whatsapp_message`
- `whatsapp_message_id`
- `raven_message`

Also confirm incoming retries keep the same WhatsApp `message_id`.

## Backfill order looks wrong

Backfill sets `Raven Message.creation/modified` from source WhatsApp/Frappe timestamps.

Check:

- `WhatsApp Raven Message Link.original_message_datetime`
- `WhatsApp Raven Message Link.source_creation/source_modified`
- `WhatsApp Raven Message Link.whatsapp_timestamp` (raw Meta timestamp when available)

If raw Meta timestamp is unavailable, backfill falls back to `WhatsApp Message.creation`.

## Backfill created outbound sends

Backfill should never call WhatsApp send. It imports historical rows only.

Verify:

- Backfill API used (`preview_backfill` / `run_backfill`)
- No new outbound WhatsApp rows with `reference_doctype = "Raven Message"` created by backfill run

## Tests are making real Meta calls

Bridge tests monkeypatch `WhatsAppMessage.notify` to avoid real network calls.
If you add tests, preserve that monkeypatch pattern.

## Install fails with missing dependencies

Install dependency apps first:

1. `frappe_whatsapp`
2. `raven`
3. `whatsapp_raven_bridge`

The bridge declares required apps in `hooks.py`.

## Infrastructure records are owned by Guest

Set `bridge_system_user` in **WhatsApp Raven Bridge Settings**.

Bridge infrastructure writes (workspace/channel memberships, inbox/thread setup) run under this user context when configured. This does not change outbound permission checks, which still use the actual Raven message owner.

You can set this automatically with the Settings bootstrap dialog or CLI bootstrap.

## What setup cannot be fully automated

Bootstrap helps with bridge-side records, but you still must complete:

- Meta webhook endpoint and verification setup
- WhatsApp Cloud API tokens/credentials
- Template approval and template lifecycle in Meta

## Bootstrap button is missing or fails

Check:

- You opened **WhatsApp Raven Bridge Settings** (not list view)
- JS assets were built (`bench build --app whatsapp_raven_bridge`)
- User has `System Manager` role (or is `Administrator`)
