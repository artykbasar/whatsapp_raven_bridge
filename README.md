# WhatsApp Raven Bridge

Bridge app for syncing `frappe_whatsapp` messages with Raven.

## Requirements

Install dependency Frappe apps before this app:

- `frappe_whatsapp`
- `raven`

This app declares those dependencies in `hooks.py` via:

```python
required_apps = ["frappe_whatsapp", "raven"]
```

## Quick Install

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for full steps.

```bash
bench get-app <frappe_whatsapp_repo_url>
bench get-app https://github.com/The-Commit-Company/raven
bench get-app https://github.com/artykbasar/whatsapp_raven_bridge

bench --site <site-name> install-app frappe_whatsapp
bench --site <site-name> install-app raven
bench --site <site-name> install-app whatsapp_raven_bridge

bench --site <site-name> migrate
bench --site <site-name> clear-cache
```

## Quick Configuration

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

High-level order:

1. Configure `frappe_whatsapp` account + webhook.
2. Configure Raven workspace + bot.
3. Configure `WhatsApp Raven Bridge Settings`.
4. Configure `WhatsApp Raven Account Route` (recommended `Thread Per Contact`).

## Setup Methods

### A) Recommended (Desk UI)

Go to **WhatsApp Raven Bridge Settings** and use:

- **Check Setup Status**
- **Run Bootstrap Setup**
- **Preview Backfill**
- **Sync All Message History Now**

`bridge_system_user` is optional. If left blank, saving settings auto-creates and assigns
`whatsapp.bridge@example.com`.
This user is for audit attribution only; inbound mirrored messages still use `bridge_raven_user`.

### B) CLI / Bench

Run bootstrap from bench execute or bench console.

### C) Fully Manual

Manually configure Raven Workspace/Bot, Bridge Settings, and Account Routes.

## Bootstrap (CLI Example)

You can bootstrap most bridge records after at least one WhatsApp Account exists:

```python
frappe.call(
	"whatsapp_raven_bridge.api.setup.bootstrap_whatsapp_raven_bridge",
	workspace_name="WhatsApp Support",
	bridge_bot_name="WhatsApp",
	bridge_system_user="whatsapp-bridge-service@example.com",
	whatsapp_accounts=["My WhatsApp Account"],
	route_members=[{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}],
	conversation_strategy="Thread Per Contact",
	channel_type="Private",
	enable_outbound_replies=1,
)
```

Check current setup state with:

```python
frappe.call("whatsapp_raven_bridge.api.setup.get_setup_status")
```

Bootstrap does not configure Meta webhook URLs, WhatsApp tokens, or template approval flows.

## Current Features

- Inbound WhatsApp text to Raven
- Outbound Raven text to WhatsApp
- Compact WhatsApp-origin Raven message layout with clickable WhatsApp source header
  - Incoming headers are highlighted for quick visual scan
  - Outgoing imported/backfilled headers show the best available agent name
- Historical WhatsApp-to-Raven text backfill with preserved message timestamps
- One-click backfill preview and full-history sync queue on Bridge Settings
- Scheduled missed-message reconciliation (lookback + limit based)
- Conversation/message mapping with idempotency links
- Account-based routing
- Thread-per-contact destination for account routes
- Channel-per-contact fallback
- Route-based outbound permission checks (`can_reply`)
- Bridge System User audit context for bridge-created infrastructure
- Admin-only private channel escalation per conversation (shared thread -> dedicated private channel)

## Current Limitations

- Text-only sync (no media sync yet)
- Start-conversation/template workflow not implemented yet
- Message status sync not implemented yet
- No Raven frontend action button yet
- Meta webhook/token/template setup is still manual
- Bridge does not expose a Raven UI button for template-start yet

## WhatsApp Message UI

For WhatsApp-origin mirrored/imported messages, the bridge keeps source mapping in
**WhatsApp Raven Message Link** and renders a compact clickable header in Raven.
Incoming headers are highlighted; outgoing imported/backfilled headers are not.
It does not attach each chat line as a visible WhatsApp Message document card.
Thread starter rows in account inbox now render as highlighted contact labels plus
code-formatted phone identifiers with leading `+`
instead of large `WhatsApp Raven Conversation` document cards.
Parent starters intentionally avoid custom links and rely on Raven’s built-in **View Thread** action.

Recommended bot display name is `WhatsApp`; Raven adds its own bot badge in UI.
Legacy default bot label `WhatsApp Bridge Bot` is repaired to `WhatsApp` by setup/migrate repair helpers when safe.

## Documentation

- Installation: [docs/INSTALLATION.md](docs/INSTALLATION.md)
- Configuration: [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
- Usage: [docs/USAGE.md](docs/USAGE.md)
- Troubleshooting: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- Development: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)

## Backfill (CLI)

Preview all local history first:

```bash
bench --site SITE execute whatsapp_raven_bridge.api.backfill.preview_all_message_history
```

Queue full-history import for all accounts:

```bash
bench --site SITE execute whatsapp_raven_bridge.api.backfill.enqueue_sync_all_message_history
```

Optional scoped/scheduled APIs remain available:

```bash
bench --site SITE execute whatsapp_raven_bridge.api.backfill.preview_backfill --kwargs '{"whatsapp_account":"ACCOUNT","limit":100}'
bench --site SITE execute whatsapp_raven_bridge.api.backfill.run_backfill --kwargs '{"whatsapp_account":"ACCOUNT","limit":100}'
```

## Testing

```bash
bench --site <site-name> run-tests --app whatsapp_raven_bridge
```

Bridge tests monkeypatch WhatsApp sending methods to avoid real Meta API calls.

## License

mit
