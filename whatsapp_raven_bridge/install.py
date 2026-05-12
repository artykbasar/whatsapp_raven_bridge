"""App install hooks."""

from __future__ import annotations

import frappe

from whatsapp_raven_bridge.api.setup import ensure_default_bridge_system_user


def after_install() -> None:
	"""Seed safe bridge audit user defaults without enabling the bridge."""
	try:
		ensure_default_bridge_system_user()
	except Exception:
		frappe.log_error(
			title="WhatsApp Raven Bridge: after_install default setup failed",
			message=frappe.get_traceback(),
		)
