# Usage

Current behavior is text-only two-way sync.

## Setup Entry Points

- Desk: **WhatsApp Raven Bridge Settings** -> **Run Bootstrap Setup**
- CLI: `bootstrap_whatsapp_raven_bridge`
- Manual: configure settings/routes directly

`Run Bootstrap Setup` in Desk now runs immediately (no dialog) and applies defaults for all available WhatsApp Accounts.

## Inbound Flow

1. Customer sends WhatsApp text message.
2. `frappe_whatsapp` creates a **WhatsApp Message** (`Incoming`, `content_type=text`).
3. Bridge resolves conversation + destination:
   - account-routed inbox/thread when route is configured
   - channel-per-contact fallback otherwise
4. Bridge creates one Raven **Text** message.
5. Bridge creates one **WhatsApp Raven Message Link** for idempotency.

## Outbound Flow

1. Assigned human user replies with Raven **Text** message in mapped channel/thread.
2. Bridge validates route membership and `can_reply`.
3. Bridge requires `conversation.whatsapp_account` (no global fallback account).
4. Bridge creates one outgoing **WhatsApp Message**.
5. `frappe_whatsapp` sends it through WhatsApp Cloud API integration.
6. Bridge creates one **WhatsApp Raven Message Link** for idempotency.

## Historical Backfill

Backfill imports existing **WhatsApp Message** rows already stored in Frappe. It does not fetch unlimited history directly from Meta.

Behavior:

1. Preview candidates first (`dry_run=1`).
2. Import only text messages for this phase.
3. Create/reuse conversation + destination thread/channel.
4. Insert Raven message, then set `Raven Message.creation/modified` to source WhatsApp/Frappe timestamp.
5. Create **WhatsApp Raven Message Link** with `is_backfilled=1` and source timestamp fields.
6. Never send to WhatsApp during backfill.

CLI examples:

```bash
bench --site SITE execute whatsapp_raven_bridge.api.backfill.preview_all_message_history
bench --site SITE execute whatsapp_raven_bridge.api.backfill.enqueue_sync_all_message_history
bench --site SITE execute whatsapp_raven_bridge.api.backfill.preview_backfill --kwargs '{"whatsapp_account":"ACCOUNT","limit":100}'
bench --site SITE execute whatsapp_raven_bridge.api.backfill.run_backfill --kwargs '{"whatsapp_account":"ACCOUNT","limit":100}'
```

Desk actions (Settings):

- **Preview Backfill**: dry-run preview for all accounts/all time/both directions (no writes)
- **Sync All Message History Now**: queues full-history import for all accounts/all time/both directions

Scheduled reconciliation:

- Controlled by Bridge Settings scheduled fields.
- Uses lookback window + limit, so it scans recent missed records only.
- Does not resend WhatsApp messages.

## Permissions

- Routed inbox/thread visibility depends on route memberships.
- Outbound replies are allowed only for route members with `can_reply=1` (unless route explicitly allows unassigned reply).
- Bridge infrastructure audit ownership can be shifted to `bridge_system_user`; outbound reply authorization still uses the actual Raven message owner.
