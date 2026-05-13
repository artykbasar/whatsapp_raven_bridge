# Configuration

Configure apps in this order.

## 1) Configure `frappe_whatsapp`

- Create at least one **WhatsApp Account**.
- Configure webhook verification and webhook endpoint in Meta.
- Confirm incoming webhook traffic creates **WhatsApp Message** records.
- Template setup is optional for current bridge scope.

## 2) Configure Raven

- Create or confirm a **Raven Workspace**.
- Create or confirm a **Raven Bot**.
- Confirm the bot has a linked **Raven User** (used by bridge-origin messages).

## 3) Configure WhatsApp Raven Bridge Settings

Open **WhatsApp Raven Bridge Settings** and set:

- `enabled` = 1
- `bridge_system_user` (optional custom service user for bridge-created infra audit)
- `bridge_raven_bot`
- `bridge_raven_user` (sender identity for inbound mirrored Raven messages)
- `enable_outbound_replies` = 1 for two-way text sync
- optional scheduled backfill controls:
  - `enable_scheduled_backfill`
  - `scheduled_backfill_interval`
  - `scheduled_backfill_lookback_hours`
  - `scheduled_backfill_limit`
  - `scheduled_backfill_direction`
- optional fallback defaults:
  - `default_raven_workspace`
  - `default_channel_type`
  - `conversation_strategy`

Install/default behavior:

- If `bridge_system_user` is blank, saving settings auto-creates and assigns
  `whatsapp.bridge@example.com`.
- Bridge remains disabled until you explicitly enable it.

## 4) Configure WhatsApp Raven Account Route

Create one route per WhatsApp account in **WhatsApp Raven Account Route**:

- `whatsapp_account`
- `raven_workspace`
- `inbox_channel` or `inbox_channel_name`
- `conversation_strategy` = `Thread Per Contact` (recommended)
- route `members`
  - `raven_user`
  - `is_admin`
  - `can_reply`

Route memberships control visibility and outbound permissions.
Outbound replies always use the `whatsapp_account` stored on each bridge conversation.

## 5) Bootstrap Alternative (Optional)

You can create/update most settings, routes, workspace, bot, and memberships with:

```python
frappe.call("whatsapp_raven_bridge.api.setup.bootstrap_whatsapp_raven_bridge", ...)
```

Use:

```python
frappe.call("whatsapp_raven_bridge.api.setup.get_setup_status")
```

to inspect configuration completeness.

Desk-friendly setup is available from **WhatsApp Raven Bridge Settings**:

- **Check Setup Status**
- **Run Bootstrap Setup**
- **Preview Backfill**
- **Run Backfill Now**
- **Run Scheduled Backfill Now**

The setup dialog supports one primary route member for MVP simplicity.
Use CLI bootstrap (`route_members`) when you need to seed multiple members at once.

## Production Recommendation

- One route per WhatsApp Account
- `Thread Per Contact` strategy
- Explicit route members
- Enable `can_reply` only for agents allowed to send WhatsApp replies
- For scheduled reconciliation start with:
  - `Hourly`
  - `24` lookback hours
  - `200` limit

## Bridge System User vs Bridge Raven Bot

- `bridge_system_user`: audit actor for bridge-created infrastructure writes.
- `bridge_raven_user`: Raven sender identity for mirrored inbound WhatsApp messages.

They serve different purposes and should both be configured.
