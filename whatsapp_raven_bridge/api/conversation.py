"""Admin APIs for WhatsApp Raven Conversation operations."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr

from whatsapp_raven_bridge.bridge.private_channel import (
	get_private_channel_state as get_private_channel_state_internal,
	move_conversation_to_private_channel,
)


def _require_conversation_admin_permission():
	if frappe.session.user == "Administrator":
		return
	if "System Manager" not in set(frappe.get_roles(frappe.session.user)):
		raise frappe.PermissionError(_("Only Administrator or System Manager can perform this action."))


@frappe.whitelist()
def move_to_private_channel(
	conversation: str,
	raven_users: list[str] | str | None = None,
	channel_name: str | None = None,
) -> dict[str, Any]:
	"""Move one conversation from route-thread delivery to private-channel delivery."""
	_require_conversation_admin_permission()
	if not cstr(conversation or "").strip():
		frappe.throw(_("Conversation is required."))

	return dict(
		move_conversation_to_private_channel(
			conversation=conversation,
			raven_users=raven_users,
			channel_name=channel_name,
			actor=frappe.session.user,
		)
	)


@frappe.whitelist()
def check_private_channel_state(conversation: str) -> dict[str, Any]:
	"""Read current private-channel state for one conversation."""
	_require_conversation_admin_permission()
	if not cstr(conversation or "").strip():
		frappe.throw(_("Conversation is required."))
	return dict(get_private_channel_state_internal(conversation))

