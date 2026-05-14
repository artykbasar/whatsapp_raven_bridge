"""Render WhatsApp-origin Raven messages in a compact conversation-friendly format."""

from __future__ import annotations

from urllib.parse import quote

import frappe
from frappe.utils import cstr, escape_html

from whatsapp_raven_bridge.utils.settings import get_bridge_system_user


def desk_whatsapp_message_route(whatsapp_message_name: str) -> str:
	"""Return the local Desk route for a WhatsApp Message document."""
	return f"/app/whatsapp-message/{quote(cstr(whatsapp_message_name or '').strip(), safe='')}"


def raven_thread_route(workspace_id: str, channel_id: str, thread_id: str) -> str:
	"""Return Raven thread route used by View Thread links."""
	return (
		f"/raven/{quote(cstr(workspace_id or '').strip(), safe='')}"
		f"/{quote(cstr(channel_id or '').strip(), safe='')}"
		f"/thread/{quote(cstr(thread_id or '').strip(), safe='')}"
	)


def build_parent_thread_starter_html(
	*,
	workspace_id: str,
	inbox_channel_id: str,
	thread_id: str,
	contact_label: str,
	phone_number: str | None,
) -> str:
	"""Render clean parent thread-starter text with links to Raven thread."""
	route = raven_thread_route(workspace_id, inbox_channel_id, thread_id)
	label = cstr(contact_label or "").strip() or "Unknown WhatsApp Contact"
	phone = cstr(phone_number or "").strip()
	lines = [f'<p><a href="{escape_html(route)}"><strong>{escape_html(label)}</strong></a></p>']
	if phone:
		lines.append(f'<p><a href="{escape_html(route)}">{escape_html(phone)}</a></p>')
	return "".join(lines)


def build_whatsapp_origin_message_html(
	*,
	whatsapp_message_name: str,
	header_label: str,
	body_text: str | None,
	highlight_header: bool = False,
) -> str:
	"""Build compact Raven HTML with clickable WhatsApp source header and message body."""
	source_route = desk_whatsapp_message_route(whatsapp_message_name)
	label = cstr(header_label or "").strip() or "Unknown WhatsApp Contact"
	header_text = f"<strong>{escape_html(label)}</strong>"
	if highlight_header:
		header_text = f"<mark>{header_text}</mark>"
	header_anchor = f'<a href="{escape_html(source_route)}">{header_text}</a>'
	if highlight_header:
		return f"<p>{header_anchor}</p>{_render_body_html(body_text)}"
	header = f"<p>{header_anchor}</p>"
	body = _render_body_html(body_text)
	return f"{header}{body}"


def build_whatsapp_origin_message_content(header_label: str, body_text: str | None) -> str:
	"""Build plain-text content companion for WhatsApp-origin Raven messages."""
	label = cstr(header_label or "").strip() or "Unknown WhatsApp Contact"
	body = cstr(body_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
	if not body:
		body = "Empty WhatsApp text message"
	return f"{label}\n{body}"


def incoming_header_label(profile_name: str | None, normalized_phone: str | None) -> str:
	"""Resolve incoming contact label for WhatsApp-origin messages."""
	name = cstr(profile_name or "").strip()
	if name:
		return name
	phone = cstr(normalized_phone or "").strip()
	if phone:
		return phone
	return "Unknown WhatsApp Contact"


def outgoing_import_header_label() -> str:
	"""Label for historically imported outgoing WhatsApp rows."""
	return "Agent"


def get_outgoing_whatsapp_agent_label(whatsapp_doc, link=None, raven_message=None) -> str:
	"""Resolve the best available agent label for an outgoing WhatsApp-origin message."""
	bridge_system_user = cstr(get_bridge_system_user() or "").strip()

	linked_raven_name = cstr((link or {}).get("raven_message") or "").strip()
	if raven_message and not hasattr(raven_message, "get"):
		raven_message = None
	if not raven_message and linked_raven_name and frappe.db.exists("Raven Message", linked_raven_name):
		raven_message = frappe.get_doc("Raven Message", linked_raven_name)

	if raven_message:
		if (
			not int(raven_message.get("is_bot_message") or 0)
			and not _is_excluded_actor(raven_message.get("owner"), bridge_system_user)
		):
			name = _resolve_user_label(raven_message.get("owner"))
			if name:
				return name

	for fieldname in ("owner", "modified_by"):
		candidate = cstr(whatsapp_doc.get(fieldname) if hasattr(whatsapp_doc, "get") else "").strip()
		if _is_excluded_actor(candidate, bridge_system_user):
			continue
		name = _resolve_user_label(candidate)
		if name:
			return name

	return outgoing_import_header_label()


def _is_excluded_actor(user_id: str | None, bridge_system_user: str | None) -> bool:
	value = cstr(user_id or "").strip()
	if not value:
		return True
	if value == "Guest":
		return True
	if bridge_system_user and value == bridge_system_user:
		return True
	return False


def _resolve_user_label(user_id: str | None) -> str | None:
	value = cstr(user_id or "").strip()
	if not value:
		return None
	if not frappe.db.exists("User", value):
		return None
	row = frappe.db.get_value("User", value, ["full_name", "first_name", "name"], as_dict=True) or {}
	return cstr(row.get("full_name") or row.get("first_name") or row.get("name") or value).strip() or None


def _render_body_html(body_text: str | None) -> str:
	body = cstr(body_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
	if not body:
		return "<p><em>Empty WhatsApp text message</em></p>"
	lines = [escape_html(line) for line in body.split("\n")]
	if len(lines) == 1:
		return f"<p>{lines[0]}</p>"
	return "".join(f"<p>{line or '&nbsp;'}</p>" for line in lines)
