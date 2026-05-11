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

## Duplicate messages

Inspect **WhatsApp Raven Message Link** records:

- `whatsapp_message`
- `whatsapp_message_id`
- `raven_message`

Also confirm incoming retries keep the same WhatsApp `message_id`.

## Tests are making real Meta calls

Bridge tests monkeypatch `WhatsAppMessage.notify` to avoid real network calls.
If you add tests, preserve that monkeypatch pattern.

## Install fails with missing dependencies

Install dependency apps first:

1. `frappe_whatsapp`
2. `raven`
3. `whatsapp_raven_bridge`

The bridge declares required apps in `hooks.py`.
