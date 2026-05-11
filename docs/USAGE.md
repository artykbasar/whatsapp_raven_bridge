# Usage

Current behavior is text-only two-way sync.

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
3. Bridge creates one outgoing **WhatsApp Message**.
4. `frappe_whatsapp` sends it through WhatsApp Cloud API integration.
5. Bridge creates one **WhatsApp Raven Message Link** for idempotency.

## Permissions

- Routed inbox/thread visibility depends on route memberships.
- Outbound replies are allowed only for route members with `can_reply=1` (unless route explicitly allows unassigned reply).
