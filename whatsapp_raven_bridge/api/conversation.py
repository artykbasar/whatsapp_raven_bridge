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


@frappe.whitelist()
def move_message_conversation_to_private_channel(
	raven_message: str | None = None,
	channel_id: str | None = None,
	raven_users: list[str] | str | None = None,
	channel_name: str | None = None,
) -> dict[str, Any]:
	"""Resolve a clicked Raven message to conversation and move it to private channel."""
	_require_conversation_admin_permission()
	message_name = cstr(raven_message or "").strip()
	if not message_name:
		frappe.throw(_("Raven Message is required."))
	if not frappe.db.exists("Raven Message", message_name):
		frappe.throw(_("Raven Message does not exist: {0}").format(message_name))

	conversation_name = _resolve_conversation_from_raven_message(
		raven_message=message_name,
		channel_id=cstr(channel_id or "").strip() or None,
	)
	if not conversation_name:
		frappe.throw(_("This Raven message is not part of a WhatsApp Raven conversation."))

	result = move_conversation_to_private_channel(
		conversation=conversation_name,
		raven_users=raven_users,
		channel_name=channel_name,
		actor=frappe.session.user,
	)
	result = dict(result)
	result["resolved_conversation"] = conversation_name
	result["raven_message"] = message_name
	return result


def _resolve_conversation_from_raven_message(*, raven_message: str, channel_id: str | None = None) -> str | None:
	# A) parent starter clicked
	conversation_name = frappe.db.get_value(
		"WhatsApp Raven Conversation",
		{"parent_raven_message": raven_message},
		"name",
	)
	if conversation_name:
		return cstr(conversation_name).strip()

	# B) any message in mapped channel (thread or private channel)
	message_channel = cstr(channel_id or "").strip()
	if not message_channel:
		message_channel = cstr(frappe.db.get_value("Raven Message", raven_message, "channel_id") or "").strip()
	if message_channel:
		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"raven_channel": message_channel},
			"name",
		)
		if conversation_name:
			return cstr(conversation_name).strip()

	# C) specific WhatsApp-origin message link row
	conversation_name = frappe.db.get_value(
		"WhatsApp Raven Message Link",
		{"raven_message": raven_message},
		"conversation",
	)
	if conversation_name:
		return cstr(conversation_name).strip()

	return None
