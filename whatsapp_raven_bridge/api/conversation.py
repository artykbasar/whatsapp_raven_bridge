"""Admin APIs for WhatsApp Raven Conversation operations."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr

from whatsapp_raven_bridge.bridge.conversation import normalize_phone_number
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

	result = dict(
		move_conversation_to_private_channel(
			conversation=conversation,
			raven_users=raven_users,
			channel_name=channel_name,
			actor=frappe.session.user,
		)
	)
	result["ok"] = True
	return result


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

	resolution = resolve_conversation_from_raven_message(
		raven_message=message_name,
		channel_id=cstr(channel_id or "").strip() or None,
	)
	conversation_name = cstr((resolution or {}).get("conversation") or "").strip()
	if not conversation_name:
		reason = cstr((resolution or {}).get("reason") or "").strip()
		return {
			"ok": False,
			"message": _(
				"This Raven message is not part of a WhatsApp Raven conversation. "
				"Right-click a WhatsApp conversation parent starter with the View Thread button, "
				"or a message inside the WhatsApp thread."
			),
			"reason": reason or "not_part_of_whatsapp_conversation",
			"clicked_message": message_name,
			"clicked_channel": cstr(channel_id or "").strip(),
		}

	result = move_conversation_to_private_channel(
		conversation=conversation_name,
		raven_users=raven_users,
		channel_name=channel_name,
		actor=frappe.session.user,
	)
	result = dict(result)
	result["ok"] = True
	result["resolved_conversation"] = conversation_name
	result["raven_message"] = message_name
	return result


@frappe.whitelist()
def list_active_raven_users(limit: int = 500) -> list[dict[str, Any]]:
	"""List active Raven Users for admin selection controls."""
	_require_conversation_admin_permission()
	max_rows = min(max(cint(limit), 1), 2000)
	rows = frappe.get_all(
		"Raven User",
		filters={"enabled": 1},
		fields=["name", "user", "full_name", "first_name"],
		order_by="modified desc",
		limit=max_rows,
	)
	result = []
	for row in rows:
		raven_user = cstr(row.get("name") or "").strip()
		if not raven_user:
			continue
		user_id = cstr(row.get("user") or "").strip()
		name_label = (
			cstr(row.get("full_name") or "").strip()
			or cstr(row.get("first_name") or "").strip()
			or user_id
			or raven_user
		)
		label = name_label if not user_id else f"{name_label} ({user_id})"
		result.append({"value": raven_user, "label": label, "user": user_id})
	return result


@frappe.whitelist()
def explain_private_channel_action_target(
	raven_message: str | None = None,
	channel_id: str | None = None,
) -> dict[str, Any]:
	"""Explain whether a Raven message target can be moved to private channel."""
	_require_conversation_admin_permission()
	message_name = cstr(raven_message or "").strip()
	if not message_name:
		frappe.throw(_("Raven Message is required."))
	if not frappe.db.exists("Raven Message", message_name):
		frappe.throw(_("Raven Message does not exist: {0}").format(message_name))
	resolved = dict(
		resolve_conversation_from_raven_message(
			raven_message=message_name,
			channel_id=cstr(channel_id or "").strip() or None,
		)
	)
	conversation_name = cstr(resolved.get("conversation") or "").strip()
	resolved["can_move"] = bool(conversation_name)
	resolved["suggested_action"] = (
		"Use this message action now."
		if conversation_name
		else (
			"Right-click a WhatsApp conversation parent starter with the View Thread button, "
			"or a message inside the WhatsApp thread."
		)
	)
	return resolved


def resolve_conversation_from_raven_message(
	*,
	raven_message: str | None = None,
	channel_id: str | None = None,
) -> frappe._dict:
	message_name = cstr(raven_message or "").strip()
	clicked_channel = cstr(channel_id or "").strip()
	if not message_name:
		return frappe._dict(
			{
				"conversation": None,
				"reason": "missing_raven_message",
				"clicked_message": message_name,
				"clicked_channel": clicked_channel,
			}
		)

	if not frappe.db.exists("Raven Message", message_name):
		return frappe._dict(
			{
				"conversation": None,
				"reason": "missing_raven_message_doc",
				"clicked_message": message_name,
				"clicked_channel": clicked_channel,
			}
		)

	# A) parent starter clicked
	conversation_name = frappe.db.get_value(
		"WhatsApp Raven Conversation",
		{"parent_raven_message": message_name},
		"name",
	)
	if conversation_name:
		return frappe._dict(
			{
				"conversation": cstr(conversation_name).strip(),
				"reason": "matched_parent_raven_message",
				"clicked_message": message_name,
				"clicked_channel": clicked_channel,
			}
		)

	# B) any message in mapped channel (thread or private channel)
	message_doc = frappe.get_doc("Raven Message", message_name)
	message_channel = cstr(message_doc.channel_id or "").strip()
	if message_channel:
		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"raven_channel": message_channel},
			"name",
		)
		if conversation_name:
			return frappe._dict(
				{
					"conversation": cstr(conversation_name).strip(),
					"reason": "matched_message_channel_to_conversation_channel",
					"clicked_message": message_name,
					"clicked_channel": clicked_channel or message_channel,
				}
			)

	# C) explicit channel_id
	if clicked_channel:
		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"raven_channel": clicked_channel},
			"name",
		)
		if conversation_name:
			return frappe._dict(
				{
					"conversation": cstr(conversation_name).strip(),
					"reason": "matched_explicit_channel_to_conversation_channel",
					"clicked_message": message_name,
					"clicked_channel": clicked_channel,
				}
			)

	# D) specific WhatsApp-origin message link row
	conversation_name = frappe.db.get_value(
		"WhatsApp Raven Message Link",
		{"raven_message": message_name},
		"conversation",
	)
	if conversation_name:
		return frappe._dict(
			{
				"conversation": cstr(conversation_name).strip(),
				"reason": "matched_whatsapp_raven_message_link",
				"clicked_message": message_name,
				"clicked_channel": clicked_channel,
			}
		)

	# E) legacy link_doctype fallback
	if cstr(message_doc.link_doctype or "").strip() == "WhatsApp Message":
		whatsapp_message_name = cstr(message_doc.link_document or "").strip()
		if whatsapp_message_name and frappe.db.exists("WhatsApp Message", whatsapp_message_name):
			conversation_name = frappe.db.get_value(
				"WhatsApp Raven Message Link",
				{"whatsapp_message": whatsapp_message_name},
				"conversation",
			)
			if conversation_name:
				return frappe._dict(
					{
						"conversation": cstr(conversation_name).strip(),
						"reason": "matched_legacy_link_doctype_via_message_link",
						"clicked_message": message_name,
						"clicked_channel": clicked_channel,
					}
				)

			wm = frappe.db.get_value(
				"WhatsApp Message",
				whatsapp_message_name,
				["whatsapp_account", "from", "to", "type"],
				as_dict=True,
			)
			if wm:
				phone_raw = cstr(wm.get("from") if cstr(wm.get("type") or "").strip() == "Incoming" else wm.get("to") or "").strip()
				phone_norm = normalize_phone_number(phone_raw) if phone_raw else ""
				if cstr(wm.get("whatsapp_account") or "").strip() and phone_norm:
					conversation_name = frappe.db.get_value(
						"WhatsApp Raven Conversation",
						{"whatsapp_account": cstr(wm.get("whatsapp_account")).strip(), "phone_number": phone_norm},
						"name",
					)
					if conversation_name:
						return frappe._dict(
							{
								"conversation": cstr(conversation_name).strip(),
								"reason": "matched_legacy_link_doctype_via_whatsapp_message_fields",
								"clicked_message": message_name,
								"clicked_channel": clicked_channel,
							}
						)

	# F) route inbox helpful detection
	inbox_channel = clicked_channel or message_channel
	if inbox_channel:
		route_name = frappe.db.get_value(
			"WhatsApp Raven Account Route",
			{"inbox_channel": inbox_channel, "enabled": 1},
			"name",
		)
		if route_name:
			return frappe._dict(
				{
					"conversation": None,
					"reason": (
						"clicked_route_inbox_non_parent: Clicked message is in the WhatsApp shared inbox, "
						"but it is not a WhatsApp conversation parent starter. Right-click the parent starter "
						"message with the View Thread button, or a message inside the WhatsApp thread."
					),
					"clicked_message": message_name,
					"clicked_channel": inbox_channel,
				}
			)

	return frappe._dict(
		{
			"conversation": None,
			"reason": "not_part_of_whatsapp_conversation",
			"clicked_message": message_name,
			"clicked_channel": clicked_channel or message_channel,
		}
	)
