"""App install hooks."""

from __future__ import annotations

import frappe

from whatsapp_raven_bridge.api.setup import _ensure_default_bridge_system_user_state
from whatsapp_raven_bridge.bridge.raven_actions import ensure_raven_message_actions


def after_install() -> None:
	"""Seed safe bridge audit user defaults without enabling the bridge."""
	try:
		_ensure_default_bridge_system_user_state()
	except Exception:
		frappe.log_error(
			title="WhatsApp Raven Bridge: after_install default setup failed",
			message=frappe.get_traceback(),
		)

	_try_ensure_raven_message_actions("after_install")


def after_migrate() -> None:
	"""Repair/install idempotent integration records after migrations."""
	_try_ensure_raven_message_actions("after_migrate")


def _try_ensure_raven_message_actions(source: str) -> None:
	try:
		ensure_raven_message_actions()
	except Exception:
		frappe.log_error(
			title=f"WhatsApp Raven Bridge: ensure message actions failed ({source})",
			message=frappe.get_traceback(),
		)
