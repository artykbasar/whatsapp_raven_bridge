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
