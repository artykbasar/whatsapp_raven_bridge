from __future__ import annotations

from whatsapp_raven_bridge.api.setup import _repair_bridge_system_user_single_value


def execute():
	"""Repair stale/missing bridge_system_user single value (v2)."""
	_repair_bridge_system_user_single_value()

