"""Raven Message Action setup helpers for WhatsApp Raven Bridge."""

from __future__ import annotations

import frappe
from frappe.utils import cint

ACTION_NAME = "Move WhatsApp Conversation to Private Channel"
ACTION_TITLE = "Move WhatsApp Conversation to Private Channel"
ACTION_DESCRIPTION = (
	"Choose Raven users who should access this WhatsApp conversation as a private channel."
)
ACTION_SUCCESS_MESSAGE = (
	"WhatsApp conversation moved to private channel. "
	"Open the channel from the Raven sidebar (the old thread route is no longer active)."
)
ACTION_FUNCTION_PATH = "whatsapp_raven_bridge.api.conversation.move_message_conversation_to_private_channel"


def ensure_raven_message_actions() -> frappe._dict:
	"""Create/update bridge Raven Message Action records idempotently."""
	if not frappe.db.exists("DocType", "Raven Message Action"):
		return frappe._dict({"updated": False, "reason": "missing_raven_message_action_doctype"})
	if not frappe.db.exists("DocType", "Raven Message Action Fields"):
		return frappe._dict({"updated": False, "reason": "missing_raven_message_action_fields_doctype"})

	action = _get_existing_action()
	was_enabled = None
	if action:
		was_enabled = cint(action.enabled)
	else:
		action = frappe.new_doc("Raven Message Action")
		action.action_name = ACTION_NAME
		action.enabled = 1

	action.action_name = ACTION_NAME
	action.action = "Custom Function"
	action.custom_function_path = ACTION_FUNCTION_PATH
	action.title = ACTION_TITLE
	action.description = ACTION_DESCRIPTION
	action.success_message = ACTION_SUCCESS_MESSAGE

	action.set("fields", [])
	for row in _build_action_fields():
		action.append("fields", row)

	# Preserve intentional disable state for existing actions.
	if was_enabled == 0:
		action.enabled = 0
	elif was_enabled is None:
		action.enabled = 1

	action.save(ignore_permissions=True)
	frappe.clear_document_cache("Raven Message Action", action.name)
	return frappe._dict({"updated": True, "action_name": action.name, "enabled": cint(action.enabled)})


def _get_existing_action():
	name = frappe.db.get_value("Raven Message Action", {"custom_function_path": ACTION_FUNCTION_PATH}, "name")
	if not name:
		name = frappe.db.get_value("Raven Message Action", {"action_name": ACTION_NAME}, "name")
	if not name:
		return None
	return frappe.get_doc("Raven Message Action", name)


def _build_action_fields() -> list[dict]:
	return [
		{
			"fieldname": "raven_message",
			"label": "Raven Message",
			"type": "Data",
			"is_required": 1,
			"default_value_type": "Message Field",
			"default_value": "name",
			"helper_text": "Auto-filled from selected Raven message.",
		},
		{
			"fieldname": "channel_id",
			"label": "Raven Channel",
			"type": "Data",
			"is_required": 0,
			"default_value_type": "Message Field",
			"default_value": "channel_id",
			"helper_text": "Auto-filled from selected Raven message.",
		},
		{
			"fieldname": "raven_users",
			"label": "Raven Users",
			"type": "Small Text",
			"is_required": 0,
			"helper_text": (
				"Optional. Comma-separated Raven User IDs/User IDs, or JSON list. "
				"If empty, route defaults + actor + Administrator are used."
			),
		},
		{
			"fieldname": "channel_name",
			"label": "Private Channel Display Name",
			"type": "Data",
			"is_required": 0,
			"helper_text": "Optional. Default: WhatsApp - <contact label>.",
		},
	]
