from __future__ import annotations

import frappe

from whatsapp_raven_bridge.bridge.raven_actions import ensure_raven_message_actions


def execute():
	try:
		ensure_raven_message_actions()
	except Exception:
		frappe.log_error(
			title="WhatsApp Raven Bridge: patch ensure_raven_message_actions failed",
			message=frappe.get_traceback(),
		)
