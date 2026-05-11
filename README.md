# WhatsApp Raven Bridge

Bridge app for syncing `frappe_whatsapp` messages with Raven.

## Purpose

`whatsapp_raven_bridge` is a Frappe app that keeps adapter logic outside Raven and `frappe_whatsapp`. The app owns bridge settings, future conversation mapping, sync helpers, and whitelisted APIs for starting WhatsApp conversations from Raven-oriented workflows.

Phase 4 adds outbound text sync on top of the Phase 1/2/3A/3B infrastructure.

## Architecture

Inbound target architecture:

```text
Meta WhatsApp webhook
-> frappe_whatsapp creates WhatsApp Message
-> whatsapp_raven_bridge detects incoming WhatsApp Message
-> bridge finds or creates a WhatsApp/Raven conversation mapping
-> bridge creates Raven Message
-> Raven UI updates through existing Raven realtime behavior
```

Outbound target architecture:

```text
Agent sends Raven Message inside a WhatsApp-linked Raven conversation
-> whatsapp_raven_bridge detects the Raven Message
-> bridge creates outgoing WhatsApp Message
-> frappe_whatsapp sends it through its existing WhatsApp sending logic
```

The first MVP is text only. The first Raven destination strategy is Channel Per Contact.

## Requirements

Install these apps on the target site before installing this bridge:

- `frappe_whatsapp`
- `raven`

Confirmed local Raven behavior:

- `Raven Message.bot` links to `Raven User`.
- The bridge settings store both `bridge_raven_bot` and `bridge_raven_user`.

## Installation

From the bench root:

```bash
bench new-app whatsapp_raven_bridge
bench --site development.localhost install-app whatsapp_raven_bridge
bench --site development.localhost migrate
bench --site development.localhost clear-cache
```

For an existing checkout:

```bash
bench --site development.localhost install-app whatsapp_raven_bridge
bench --site development.localhost migrate
bench --site development.localhost clear-cache
```

## Settings

`WhatsApp Raven Bridge Settings` is a Single DocType with:

- `enabled`: global bridge toggle.
- `default_raven_workspace`: workspace used when creating Raven destinations.
- `default_channel_type`: default Raven channel type.
- `bridge_raven_bot`: configured Raven Bot.
- `bridge_raven_user`: Raven User linked to the configured Raven Bot.
- `default_whatsapp_account`: WhatsApp Account used for outbound messages.
- `conversation_strategy`: Channel Per Contact or Thread Per Contact.
- `enable_outbound_replies`: future outbound sync toggle.
- `enable_start_conversation`: future template start-conversation toggle.
- `default_channel_members`: Raven users to add to new contact channels.

`WhatsApp Raven Default Channel Member` is the child table for default channel members.

## Conversation Mapping

`WhatsApp Raven Conversation` maps a WhatsApp phone/account pair to a Raven destination.

`WhatsApp Raven Message Link` maps each WhatsApp/Raven message pair, including Meta WhatsApp message IDs where available.

These DocTypes are used by active inbound/outbound sync and idempotency checks.

## Raven Destination Resolver

`ensure_raven_destination` in `whatsapp_raven_bridge.bridge.raven_destination` creates or reuses the Raven destination for a `WhatsApp Raven Conversation`.

For `Channel Per Contact`, destination naming remains deterministic (`whatsapp-<phone_number>`).
For account routes configured as `Thread Per Contact`, the bridge creates or reuses:
- one inbox channel per route/account
- one thread parent message per customer conversation in that inbox
- one thread channel (name = parent message id) as the active message destination

Channel resolution is used by active inbound/outbound sync phases.

## Inbound Text Sync

Incoming `WhatsApp Message` records with `content_type = text` are mirrored to Raven.

The bridge:

- creates or reuses a mapped `WhatsApp Raven Conversation`
- ensures the mapped Raven Channel exists
- creates one Raven `Text` message as the configured bridge Raven User
- creates one `WhatsApp Raven Message Link` for idempotency

Outgoing WhatsApp messages and incoming non-text message types are ignored in this phase.
Outbound Raven-to-WhatsApp text sync is available separately in the Outbound Text Sync section.

## Raven UI Visibility

Bridge destination resolution now ensures membership at both levels for configured users:

- `Raven Workspace Member` is ensured in the destination workspace.
- `Raven Channel Member` is ensured in the destination channel.

This applies to `bridge_raven_user` and all `default_channel_members`, so private bridge channels are visible to configured human Raven users in Raven channel list APIs/UI.

## Outbound Text Sync

Only human Raven `Text` messages in WhatsApp-linked Raven channels are sent to WhatsApp.

The bridge:

- ignores Raven bot messages
- ignores mirrored inbound WhatsApp-origin Raven messages
- ignores Raven messages outside WhatsApp-linked channels
- creates one outgoing `WhatsApp Message` and one `WhatsApp Raven Message Link`
- relies on `frappe_whatsapp` send behavior during outgoing `WhatsApp Message` insert

Outbound sync requires `WhatsApp Raven Bridge Settings.enable_outbound_replies = 1`.

For local tests, mock or monkeypatch the WhatsApp send call to avoid real Meta API traffic.

Two-way MVP status: inbound text sync and outbound text sync are both active. Media/template/status sync is not part of this phase.

## Account Routing and Thread Inbox

Per-account routing is now supported via `WhatsApp Raven Account Route`:

- each `WhatsApp Account` can map to a dedicated Raven inbox workspace/channel
- each route has assigned Raven users with per-user `can_reply`
- outbound sync enforces route-member reply permissions
- route memberships are ensured at workspace, inbox-channel, and thread-channel level
- `Thread Per Contact` is implemented for account routes
- `Channel Per Contact` remains supported as route/global fallback

## MVP Roadmap

1. Settings: present
2. Conversation mapping: present
3. Raven destination resolver: present
4. Inbound WhatsApp text to Raven: present
5. Outbound Raven text to WhatsApp: present
6. Account routing and thread inbox design: present
7. Thread Per Contact destination: present (for account routes)
8. Start conversation with template
9. Media/status sync

## Phase 1 Warning

No WhatsApp or Raven message sync is active in Phase 1. `hooks.py` intentionally has no active `doc_events` for `WhatsApp Message` or `Raven Message`.

## License

mit
