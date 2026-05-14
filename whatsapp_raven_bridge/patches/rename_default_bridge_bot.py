"""Repair legacy default bridge bot name for cleaner Raven UI badge rendering."""

from __future__ import annotations

import frappe

from whatsapp_raven_bridge.api.setup import _repair_default_bridge_bot_name_state


def execute():
	try:
		_repair_default_bridge_bot_name_state(update_settings=True)
	except Exception:
		frappe.log_error(
			title="WhatsApp Raven Bridge: default bot rename patch failed",
			message=frappe.get_traceback(),
		)
