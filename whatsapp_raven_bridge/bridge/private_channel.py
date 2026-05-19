"""Private Channel Escalation helpers for WhatsApp Raven Conversation."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

from whatsapp_raven_bridge.utils.settings import bridge_user_context, get_settings

CONVERSATION_DOCTYPE = "WhatsApp Raven Conversation"
ROUTE_DOCTYPE = "WhatsApp Raven Account Route"
RAVEN_CHANNEL_DOCTYPE = "Raven Channel"
RAVEN_MESSAGE_DOCTYPE = "Raven Message"
RAVEN_CHANNEL_MEMBER_DOCTYPE = "Raven Channel Member"
RAVEN_USER_DOCTYPE = "Raven User"
DELIVERY_MODE_ROUTE_THREAD = "Route Thread"
DELIVERY_MODE_PRIVATE_CHANNEL = "Private Channel"


def is_private_channel_user_allowed_to_reply(conversation, raven_user: str | None, sender_user: str | None = None) -> bool:
	"""Return whether a sender is allowed to send outbound WhatsApp from a private channel conversation."""
	conversation_doc = _get_conversation_doc(conversation)
	if cstr(conversation_doc.get("delivery_mode") or DELIVERY_MODE_ROUTE_THREAD) != DELIVERY_MODE_PRIVATE_CHANNEL:
		return True

	if sender_user and _is_admin_or_system_manager(sender_user):
		return True

	raven_user_id = cstr(raven_user or "").strip()
	if not raven_user_id:
		return False

	for row in conversation_doc.get("private_members") or []:
		if cstr(row.get("raven_user") or "").strip() == raven_user_id:
			return bool(cint(row.get("can_reply", 1)))

	return False


def move_conversation_to_private_channel(
	*,
	conversation: str | Any,
	raven_users: list[str] | str | None = None,
	channel_name: str | None = None,
	actor: str | None = None,
) -> frappe._dict:
	"""Convert an existing route thread destination into a normal private channel."""
	conversation_doc = _get_conversation_doc(conversation)
	actor = cstr(actor or frappe.session.user or "").strip() or "Administrator"

	if not cstr(conversation_doc.get("account_route") or "").strip():
		frappe.throw(_("Conversation {0} has no Account Route.").format(conversation_doc.name))
	if not cstr(conversation_doc.get("raven_channel") or "").strip():
		frappe.throw(_("Conversation {0} has no Raven Channel.").format(conversation_doc.name))

	route_doc = frappe.get_doc(ROUTE_DOCTYPE, conversation_doc.account_route)
	channel_doc = _get_existing_channel(conversation_doc.raven_channel)
	if not channel_doc:
		frappe.throw(_("Conversation channel does not exist: {0}").format(conversation_doc.raven_channel))

	selected_members = _resolve_selected_private_members(raven_users, route_doc=route_doc, actor=actor)
	target_channel_name = _resolve_private_channel_name(conversation_doc, channel_name)
	bridge_raven_user = cstr((get_settings() or {}).get("bridge_raven_user") or "").strip()
	admin_raven_user = cstr(frappe.db.get_value("Raven User", {"user": "Administrator", "enabled": 1}, "name") or "").strip()

	already_private = (
		cstr(conversation_doc.get("delivery_mode") or DELIVERY_MODE_ROUTE_THREAD) == DELIVERY_MODE_PRIVATE_CHANNEL
		and not cint(channel_doc.is_thread)
	)
	parent_doc = _get_optional_parent_message(conversation_doc.parent_raven_message)
	if not already_private:
		if not cint(channel_doc.is_thread):
			frappe.throw(_("Conversation channel {0} is not a thread channel.").format(channel_doc.name))
		parent_doc = _get_required_parent_message(conversation_doc.parent_raven_message)
		if cstr(parent_doc.name or "").strip() != cstr(channel_doc.name or "").strip():
			frappe.throw(_("Thread invariants are broken: parent message and thread channel do not match."))

	with bridge_user_context():
		frappe.db.set_value(
			RAVEN_CHANNEL_DOCTYPE,
			channel_doc.name,
			{
				"is_thread": 0,
				"type": "Private",
				"channel_name": target_channel_name,
			},
			update_modified=False,
		)
		frappe.clear_document_cache(RAVEN_CHANNEL_DOCTYPE, channel_doc.name)

		if parent_doc and cint(parent_doc.is_thread):
			frappe.db.set_value(
				RAVEN_MESSAGE_DOCTYPE,
				parent_doc.name,
				{
					"is_thread": 0,
					"text": "<p><em>Moved to private WhatsApp channel.</em></p>",
					"content": "Moved to private WhatsApp channel.",
					"hide_link_preview": 1,
					"link_doctype": None,
					"link_document": None,
				},
				update_modified=False,
			)
			frappe.clear_document_cache(RAVEN_MESSAGE_DOCTYPE, parent_doc.name)

	private_member_rows = _build_private_member_rows(selected_members, route_doc=route_doc, admin_raven_user=admin_raven_user)
		# Ensure bridge/system visibility while keeping route-wide membership narrow.
	keep_users = {row.get("raven_user") for row in private_member_rows if row.get("raven_user")}
	if admin_raven_user:
		keep_users.add(admin_raven_user)
	if bridge_raven_user:
		keep_users.add(bridge_raven_user)

	_ensure_private_memberships(
		channel_doc=channel_doc,
		workspace=route_doc.raven_workspace,
		private_member_rows=private_member_rows,
		bridge_raven_user=bridge_raven_user,
	)
	_remove_unselected_route_members_from_channel(
		channel_id=channel_doc.name,
		route_doc=route_doc,
		keep_users=keep_users,
	)

	summary_text = ", ".join(sorted(keep_users))
	conversation_doc.delivery_mode = DELIVERY_MODE_PRIVATE_CHANNEL
	conversation_doc.private_channel_moved_at = conversation_doc.private_channel_moved_at or now_datetime()
	conversation_doc.private_channel_moved_by = conversation_doc.private_channel_moved_by or actor
	conversation_doc.private_channel_name = target_channel_name
	if parent_doc and not cstr(conversation_doc.previous_parent_raven_message or "").strip():
		conversation_doc.previous_parent_raven_message = parent_doc.name
	if not cstr(conversation_doc.previous_route_thread_channel or "").strip():
		conversation_doc.previous_route_thread_channel = channel_doc.name
	if parent_doc and not cstr(conversation_doc.previous_route_inbox_channel or "").strip():
		conversation_doc.previous_route_inbox_channel = cstr(parent_doc.channel_id or "").strip() or None
	conversation_doc.set("private_members", [])
	for row in private_member_rows:
		conversation_doc.append("private_members", row)
	conversation_doc.private_channel_members_summary = summary_text
	conversation_doc.save(ignore_permissions=True)

	return frappe._dict(
		{
			"conversation": conversation_doc.name,
			"delivery_mode": conversation_doc.delivery_mode,
			"private_channel": channel_doc.name,
			"private_channel_name": target_channel_name,
			"previous_parent_raven_message": conversation_doc.previous_parent_raven_message,
			"previous_route_inbox_channel": conversation_doc.previous_route_inbox_channel,
			"previous_route_thread_channel": conversation_doc.previous_route_thread_channel,
			"selected_members": [row.get("raven_user") for row in private_member_rows],
		}
	)


def get_private_channel_state(conversation: str | Any) -> frappe._dict:
	"""Return current private-channel state snapshot for UI diagnostics."""
	conversation_doc = _get_conversation_doc(conversation)
	channel_doc = None
	channel_name = cstr(conversation_doc.raven_channel or "").strip()
	if channel_name and frappe.db.exists(RAVEN_CHANNEL_DOCTYPE, channel_name):
		channel_doc = frappe.get_doc(RAVEN_CHANNEL_DOCTYPE, channel_name)
	return frappe._dict(
		{
			"conversation": conversation_doc.name,
			"delivery_mode": cstr(conversation_doc.delivery_mode or DELIVERY_MODE_ROUTE_THREAD),
			"raven_channel": channel_name,
			"channel_is_thread": cint(channel_doc.is_thread) if channel_doc else None,
			"channel_type": cstr(channel_doc.type) if channel_doc else None,
			"channel_name": cstr(channel_doc.channel_name) if channel_doc else None,
			"parent_raven_message": cstr(conversation_doc.parent_raven_message or "").strip(),
			"previous_parent_raven_message": cstr(conversation_doc.previous_parent_raven_message or "").strip(),
			"previous_route_inbox_channel": cstr(conversation_doc.previous_route_inbox_channel or "").strip(),
			"previous_route_thread_channel": cstr(conversation_doc.previous_route_thread_channel or "").strip(),
			"private_members": [
				{
					"raven_user": cstr(row.get("raven_user") or "").strip(),
					"can_reply": cint(row.get("can_reply", 1)),
					"is_admin": cint(row.get("is_admin", 0)),
				}
				for row in (conversation_doc.get("private_members") or [])
			],
		}
	)


def ensure_private_channel_memberships(conversation: str | Any):
	"""Ensure workspace/channel memberships for a private-channel conversation destination."""
	conversation_doc = _get_conversation_doc(conversation)
	if cstr(conversation_doc.get("delivery_mode") or DELIVERY_MODE_ROUTE_THREAD) != DELIVERY_MODE_PRIVATE_CHANNEL:
		return None

	channel_name = cstr(conversation_doc.get("raven_channel") or "").strip()
	if not channel_name or not frappe.db.exists(RAVEN_CHANNEL_DOCTYPE, channel_name):
		frappe.throw(_("Private channel is missing for conversation {0}.").format(conversation_doc.name))
	channel_doc = frappe.get_doc(RAVEN_CHANNEL_DOCTYPE, channel_name)

	if cint(channel_doc.is_thread):
		frappe.throw(_("Private channel {0} is still marked as thread.").format(channel_doc.name))

	if not cstr(conversation_doc.account_route or "").strip():
		frappe.throw(_("Account Route is required for private-channel conversation {0}.").format(conversation_doc.name))
	route_doc = frappe.get_doc(ROUTE_DOCTYPE, conversation_doc.account_route)
	bridge_raven_user = cstr((get_settings() or {}).get("bridge_raven_user") or "").strip()

	rows = []
	for row in (conversation_doc.get("private_members") or []):
		raven_user = cstr(row.get("raven_user") or "").strip()
		if not raven_user:
			continue
		rows.append(
			{
				"raven_user": raven_user,
				"can_reply": cint(row.get("can_reply", 1)),
				"is_admin": cint(row.get("is_admin", 0)),
			}
		)
	if not rows:
		rows = _build_private_member_rows(
			_resolve_selected_private_members(None, route_doc=route_doc, actor=frappe.session.user),
			route_doc=route_doc,
			admin_raven_user=cstr(
				frappe.db.get_value("Raven User", {"user": "Administrator", "enabled": 1}, "name") or ""
			).strip(),
		)

	_ensure_private_memberships(
		channel_doc=channel_doc,
		workspace=route_doc.raven_workspace,
		private_member_rows=rows,
		bridge_raven_user=bridge_raven_user,
	)
	return channel_doc


def _get_existing_channel(channel_name: str):
	name = cstr(channel_name or "").strip()
	if not name or not frappe.db.exists(RAVEN_CHANNEL_DOCTYPE, name):
		return None
	return frappe.get_doc(RAVEN_CHANNEL_DOCTYPE, name)


def _get_required_parent_message(parent_name: str):
	name = cstr(parent_name or "").strip()
	if not name or not frappe.db.exists(RAVEN_MESSAGE_DOCTYPE, name):
		frappe.throw(_("Conversation parent message does not exist: {0}").format(name or _("not set")))
	parent_doc = frappe.get_doc(RAVEN_MESSAGE_DOCTYPE, name)
	if not cint(parent_doc.is_thread):
		frappe.throw(_("Conversation parent message {0} is not marked as thread parent.").format(name))
	return parent_doc


def _get_optional_parent_message(parent_name: str):
	name = cstr(parent_name or "").strip()
	if not name or not frappe.db.exists(RAVEN_MESSAGE_DOCTYPE, name):
		return None
	return frappe.get_doc(RAVEN_MESSAGE_DOCTYPE, name)


def _resolve_selected_private_members(raven_users, *, route_doc, actor: str) -> list[str]:
	values = _coerce_raven_user_tokens(raven_users)
	resolved = []
	seen = set()
	for token in values:
		raven_user = _resolve_raven_user(token)
		if raven_user in seen:
			continue
		seen.add(raven_user)
		resolved.append(raven_user)

	if not resolved:
		route_defaults = [
			cstr(row.raven_user).strip()
			for row in (route_doc.members or [])
			if cstr(row.raven_user).strip()
		]
		for raven_user in route_defaults:
			if raven_user not in seen:
				seen.add(raven_user)
				resolved.append(raven_user)

	actor_raven_user = _resolve_raven_user(actor, throw=False)
	if actor_raven_user and actor_raven_user not in seen:
		seen.add(actor_raven_user)
		resolved.append(actor_raven_user)

	admin_raven_user = cstr(frappe.db.get_value("Raven User", {"user": "Administrator", "enabled": 1}, "name") or "").strip()
	if admin_raven_user and admin_raven_user not in seen:
		resolved.append(admin_raven_user)

	if not resolved:
		frappe.throw(_("At least one private channel member is required."))

	return resolved


def _resolve_private_channel_name(conversation_doc, channel_name: str | None) -> str:
	custom_name = cstr(channel_name or "").strip()
	if custom_name:
		return custom_name
	label = (
		cstr(conversation_doc.display_name or "").strip()
		or cstr(conversation_doc.profile_name or "").strip()
		or cstr(conversation_doc.phone_number or "").strip()
		or "Contact"
	)
	return f"WhatsApp - {label}"[:140]


def _build_private_member_rows(selected_members: list[str], *, route_doc, admin_raven_user: str) -> list[dict[str, Any]]:
	route_admin_map = {
		cstr(row.raven_user).strip(): cint(row.is_admin)
		for row in (route_doc.members or [])
		if cstr(row.raven_user).strip()
	}
	rows = []
	for raven_user in selected_members:
		is_admin = 1 if raven_user == admin_raven_user else cint(route_admin_map.get(raven_user, 0))
		rows.append(
			{
				"raven_user": raven_user,
				"can_reply": 1,
				"is_admin": is_admin,
			}
		)
	return rows


def _ensure_private_memberships(*, channel_doc, workspace: str, private_member_rows: list[dict[str, Any]], bridge_raven_user: str):
	from whatsapp_raven_bridge.bridge.raven_destination import ensure_channel_member, ensure_workspace_member

	for row in private_member_rows:
		raven_user = cstr(row.get("raven_user") or "").strip()
		if not raven_user:
			continue
		is_admin = cint(row.get("is_admin", 0))
		ensure_workspace_member(workspace, raven_user, is_admin=is_admin)
		ensure_channel_member(channel_doc.name, raven_user, is_admin=is_admin)

	if bridge_raven_user:
		ensure_workspace_member(workspace, bridge_raven_user, is_admin=1)
		ensure_channel_member(channel_doc.name, bridge_raven_user, is_admin=1)


def _remove_unselected_route_members_from_channel(*, channel_id: str, route_doc, keep_users: set[str]):
	route_member_users = {
		cstr(row.raven_user).strip()
		for row in (route_doc.members or [])
		if cstr(row.raven_user).strip()
	}
	if not route_member_users:
		return

	for member_name in frappe.get_all(
		RAVEN_CHANNEL_MEMBER_DOCTYPE,
		filters={"channel_id": channel_id},
		pluck="name",
	):
		row = frappe.db.get_value(
			RAVEN_CHANNEL_MEMBER_DOCTYPE,
			member_name,
			["user_id", "is_admin"],
			as_dict=True,
		)
		raven_user = cstr((row or {}).get("user_id") or "").strip()
		if not raven_user:
			continue
		if raven_user not in route_member_users:
			continue
		if raven_user in keep_users:
			continue
		if cstr(frappe.db.get_value(RAVEN_USER_DOCTYPE, raven_user, "user") or "").strip() == "Administrator":
			continue
		frappe.delete_doc(RAVEN_CHANNEL_MEMBER_DOCTYPE, member_name, force=True, ignore_permissions=True)


def _coerce_raven_user_tokens(value) -> list[str]:
	if value is None:
		return []
	if isinstance(value, str):
		text = value.strip()
		if not text:
			return []
		try:
			parsed = frappe.parse_json(text)
		except Exception:
			parsed = [item.strip() for item in text.split(",") if item.strip()]
		value = parsed
	if isinstance(value, (tuple, set)):
		value = list(value)
	if isinstance(value, list):
		return [cstr(item).strip() for item in value if cstr(item).strip()]
	return [cstr(value).strip()] if cstr(value).strip() else []


def _resolve_raven_user(value: str, throw: bool = True) -> str | None:
	token = cstr(value or "").strip()
	if not token:
		return None

	if frappe.db.exists(RAVEN_USER_DOCTYPE, token):
		if cint(frappe.db.get_value(RAVEN_USER_DOCTYPE, token, "enabled")):
			return token
		if throw:
			frappe.throw(_("Raven User is disabled: {0}").format(token))
		return None

	found = frappe.db.get_value(
		RAVEN_USER_DOCTYPE,
		{"user": token, "enabled": 1},
		"name",
	)
	if found:
		return cstr(found).strip()
	if throw:
		frappe.throw(_("Could not resolve Raven User: {0}").format(token))
	return None


def _is_admin_or_system_manager(user: str) -> bool:
	user_id = cstr(user or "").strip()
	if not user_id:
		return False
	if user_id == "Administrator":
		return True
	return "System Manager" in set(frappe.get_roles(user_id))


def _get_conversation_doc(conversation):
	if isinstance(conversation, str):
		return frappe.get_doc(CONVERSATION_DOCTYPE, conversation)
	return conversation
