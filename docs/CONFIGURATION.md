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
- `bridge_raven_bot`
- `bridge_raven_user`
- `enable_outbound_replies` = 1 for two-way text sync
- optional fallback defaults:
  - `default_raven_workspace`
  - `default_channel_type`
  - `default_whatsapp_account`
  - `conversation_strategy`

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
