# Development Utilities

## Demo Data Seeder (Development Only)

Use this to seed local Raven/WhatsApp bridge demo threads without real Meta sends:

```bash
bench --site development.localhost execute whatsapp_raven_bridge.dev_seed.create_demo_whatsapp_raven_data --kwargs '{"conversations":10,"messages_per_conversation":10,"cleanup_existing":True}'
```

Safety guard:

- Allowed when `developer_mode` is enabled, or
- `WHATSAPP_RAVEN_BRIDGE_ALLOW_DEMO_SEED=1` is set, or
- `force=1` is passed on a site name containing `dev`, `development`, `local`, or `test`.

This utility is for development/testing only.
