"""Render WhatsApp-origin Raven messages in a compact conversation-friendly format."""

from __future__ import annotations

from urllib.parse import quote

from frappe.utils import cstr, escape_html


def desk_whatsapp_message_route(whatsapp_message_name: str) -> str:
	"""Return the local Desk route for a WhatsApp Message document."""
	return f"/app/whatsapp-message/{quote(cstr(whatsapp_message_name or '').strip(), safe='')}"


def build_whatsapp_origin_message_html(
	*,
	whatsapp_message_name: str,
	header_label: str,
	body_text: str | None,
) -> str:
	"""Build compact Raven HTML with clickable WhatsApp source header and message body."""
	source_route = desk_whatsapp_message_route(whatsapp_message_name)
	label = cstr(header_label or "").strip() or "Unknown WhatsApp Contact"
	header = f'<p><a href="{escape_html(source_route)}"><strong>{escape_html(label)} · WhatsApp</strong></a></p>'
	body = _render_body_html(body_text)
	return f"{header}{body}"


def build_whatsapp_origin_message_content(header_label: str, body_text: str | None) -> str:
	"""Build plain-text content companion for WhatsApp-origin Raven messages."""
	label = cstr(header_label or "").strip() or "Unknown WhatsApp Contact"
	body = cstr(body_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
	if not body:
		body = "Empty WhatsApp text message"
	return f"{label} · WhatsApp\n{body}"


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


def _render_body_html(body_text: str | None) -> str:
	body = cstr(body_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
	if not body:
		return "<p><em>Empty WhatsApp text message</em></p>"
	return f"<p>{escape_html(body).replace(chr(10), '<br>')}</p>"
