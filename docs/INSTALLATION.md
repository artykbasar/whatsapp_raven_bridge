# Installation

## Requirements

- Frappe bench and a working site
- `frappe_whatsapp` app installed
- `raven` app installed
- WhatsApp Cloud API access configured through `frappe_whatsapp`

## Get Apps

From your bench root:

```bash
bench get-app <frappe_whatsapp_repo_url>
bench get-app https://github.com/The-Commit-Company/raven
bench get-app https://github.com/artykbasar/whatsapp_raven_bridge
```

Use the correct upstream URL for `frappe_whatsapp` in place of `<frappe_whatsapp_repo_url>`.

## Install Order

Install dependencies first, then this bridge app:

```bash
bench --site <site-name> install-app frappe_whatsapp
bench --site <site-name> install-app raven
bench --site <site-name> install-app whatsapp_raven_bridge
```

## Migrate and Clear Cache

```bash
bench --site <site-name> migrate
bench --site <site-name> clear-cache
```

## Optional Bootstrap

After at least one WhatsApp Account exists, you can bootstrap bridge setup:

```python
frappe.call(
	"whatsapp_raven_bridge.api.setup.bootstrap_whatsapp_raven_bridge",
	workspace_name="WhatsApp Support",
	bridge_bot_name="WhatsApp Bridge Bot",
	bridge_system_user="whatsapp-bridge-service@example.com",
	whatsapp_accounts=["<whatsapp-account-name>"],
	route_members=[{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}],
	conversation_strategy="Thread Per Contact",
	channel_type="Private",
	enable_outbound_replies=1,
	enable_start_conversation=0,
)
```

Check bootstrap/status:

```python
frappe.call("whatsapp_raven_bridge.api.setup.get_setup_status")
```
