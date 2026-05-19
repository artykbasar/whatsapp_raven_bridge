"""Outbound Raven text sync to WhatsApp."""

from __future__ import annotations

import html
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, now, strip_html_tags

from whatsapp_raven_bridge.bridge.conversation import (
	create_message_link,
	find_conversation_by_raven_channel,
	find_private_conversation_by_previous_thread_channel,
	get_existing_message_link_by_raven_message,
)
from whatsapp_raven_bridge.bridge.account_route import is_raven_user_allowed_to_reply
from whatsapp_raven_bridge.bridge.private_channel import (
	DELIVERY_MODE_PRIVATE_CHANNEL,
	is_private_channel_user_allowed_to_reply,
)
from whatsapp_raven_bridge.utils.settings import get_settings


def handle_raven_message_after_insert(doc, method=None):
	"""Hook entrypoint for Raven Message.after_insert."""
	try:
		process_outgoing_raven_message(doc)
	except Exception as error:
		log_outbound_error(doc, error)
	return None


def process_outgoing_raven_message(doc):
	"""Process one Raven text message and mirror it to an outgoing WhatsApp Message."""
	if getattr(frappe.flags, "in_install", False) or getattr(frappe.flags, "in_migrate", False) or getattr(
		frappe.flags, "in_patch", False
	):
		return "skipped_setup"

	if getattr(frappe.flags, "whatsapp_raven_bridge_syncing", False):
		return "skipped_syncing"

	settings = get_settings()
	if not settings or not cint(settings.get("enabled")):
		return "skipped_disabled"

	if not cint(settings.get("enable_outbound_replies")):
		return "skipped_outbound_disabled"

	if cstr(getattr(doc, "doctype", "")).strip() != "Raven Message":
		return "skipped_invalid_doctype"

	if cstr(doc.get("message_type")) != "Text":
		return "skipped_unsupported_message_type"

	if cint(doc.get("is_bot_message")):
		return "skipped_bot_message"

	if not cstr(doc.get("channel_id")).strip():
		return "skipped_missing_channel"

	existing_link = get_existing_message_link_by_raven_message(doc.name)
	if existing_link:
		return "skipped_existing_raven_message"

	if cstr(doc.get("link_doctype")) == "WhatsApp Message" and cstr(doc.get("link_document")).strip():
		return "skipped_linked_whatsapp_source"

	metadata = _as_dict(doc.get("json"))
	if cstr(metadata.get("source")).lower() == "whatsapp" or cstr(metadata.get("direction")).lower() == "incoming":
		return "skipped_source_whatsapp"

	conversation = find_conversation_by_raven_channel(doc.get("channel_id"))
	if not conversation:
		moved_private = find_private_conversation_by_previous_thread_channel(doc.get("channel_id"))
		if moved_private:
			return "conversation_moved_to_private_channel"
		return "skipped_no_conversation"

	if not cint(conversation.get("enabled")) or cstr(conversation.get("status")) == "Disabled":
		return "skipped_conversation_disabled"

	if not cstr(conversation.get("phone_number")).strip():
		return "skipped_missing_phone"

	sender_raven_user = _resolve_sender_raven_user(doc)
	delivery_mode = cstr(conversation.get("delivery_mode") or "").strip()
	if delivery_mode == DELIVERY_MODE_PRIVATE_CHANNEL:
		if not is_private_channel_user_allowed_to_reply(
			conversation=conversation,
			raven_user=sender_raven_user,
			sender_user=cstr(doc.get("owner") or "").strip(),
		):
			return "skipped_user_not_allowed"
	elif conversation.get("account_route"):
		if not sender_raven_user:
			return "skipped_user_not_allowed"
		if not is_raven_user_allowed_to_reply(conversation.get("account_route"), sender_raven_user):
			return "skipped_user_not_allowed"

	plain_text = extract_plain_text_from_raven_message(doc)
	if not plain_text:
		return "skipped_empty_text"

	payload = build_whatsapp_outbound_payload(
		doc=doc,
		conversation=conversation,
		settings=settings,
		plain_text=plain_text,
	)

	try:
		frappe.flags.whatsapp_raven_bridge_syncing = True

		whatsapp_message = frappe.get_doc(payload).insert(ignore_permissions=True)
		outbound_metadata = build_outbound_metadata(doc, conversation)

		message_link = create_message_link(
			conversation=conversation.name,
			direction="Outgoing",
			whatsapp_message=whatsapp_message.name,
			whatsapp_message_id=whatsapp_message.get("message_id"),
			raven_message=doc.name,
			raven_channel=doc.get("channel_id"),
			content_type="text",
			sync_status="Synced",
			metadata=outbound_metadata,
		)

		conversation.last_outbound_at = now()
		conversation.last_outbound_whatsapp_message = whatsapp_message.name
		conversation.last_raven_message = doc.name
		conversation.save(ignore_permissions=True)
	finally:
		frappe.flags.whatsapp_raven_bridge_syncing = False

	return frappe._dict(
		{
			"status": "synced",
			"conversation": conversation.name,
			"whatsapp_message": whatsapp_message.name,
			"link": message_link.name,
		}
	)


def is_supported_outgoing_raven_text(doc):
	"""Return True only for human Raven text messages."""
	if cstr(getattr(doc, "doctype", "")).strip() != "Raven Message":
		return False
	if cstr(doc.get("message_type")) != "Text":
		return False
	if cint(doc.get("is_bot_message")):
		return False
	return True


def extract_plain_text_from_raven_message(doc):
	"""Extract plain outbound WhatsApp body text from Raven message content/text."""
	content = cstr(doc.get("content")).strip()
	if content:
		return _normalize_plain_text(content)

	text_html = cstr(doc.get("text")).strip()
	if not text_html:
		return ""

	# Keep basic line breaks before stripping tags.
	text_html = re.sub(r"(?i)<br\s*/?>", "\n", text_html)
	text_html = re.sub(r"(?i)</p\s*>", "\n", text_html)
	text_html = re.sub(r"(?i)<p[^>]*>", "", text_html)

	plain = strip_html_tags(text_html or "")
	plain = html.unescape(cstr(plain))
	return _normalize_plain_text(plain)


def build_whatsapp_outbound_payload(doc, conversation, settings, plain_text=None):
	"""Build outgoing WhatsApp Message insert payload."""
	message_text = plain_text if plain_text is not None else extract_plain_text_from_raven_message(doc)
	whatsapp_account = cstr(conversation.get("whatsapp_account") or "").strip()
	if not whatsapp_account:
		frappe.throw(_("Cannot send WhatsApp reply because the bridge conversation has no WhatsApp Account."))

	payload = {
		"doctype": "WhatsApp Message",
		"type": "Outgoing",
		"message_type": "Manual",
		"content_type": "text",
		"to": conversation.get("phone_number"),
		"message": message_text,
		"whatsapp_account": whatsapp_account,
	}

	meta = frappe.get_meta("WhatsApp Message")
	if meta.has_field("reference_doctype"):
		payload["reference_doctype"] = "Raven Message"
	if meta.has_field("reference_name"):
		payload["reference_name"] = doc.name

	return payload


def build_outbound_metadata(doc, conversation):
	"""Build message-link metadata for Raven to WhatsApp outbound sync."""
	return {
		"source": "raven",
		"direction": "outgoing",
		"raven_message": doc.name,
		"raven_channel": doc.get("channel_id"),
		"conversation": conversation.name,
		"phone_number": conversation.get("phone_number"),
		"whatsapp_account": conversation.get("whatsapp_account"),
	}


def log_outbound_error(doc, error):
	context = {
		"raven_message": cstr(getattr(doc, "name", "")).strip(),
		"channel_id": cstr(doc.get("channel_id") if doc else ""),
		"message_type": cstr(doc.get("message_type") if doc else ""),
		"is_bot_message": cint(doc.get("is_bot_message") if doc else 0),
		"link_doctype": cstr(doc.get("link_doctype") if doc else ""),
		"link_document": cstr(doc.get("link_document") if doc else ""),
		"error": repr(error),
	}
	frappe.log_error(
		title="WhatsApp Raven Bridge: outbound sync error",
		message=json.dumps(context, indent=2, default=str) + "\n\n" + frappe.get_traceback(),
	)


def _as_dict(value):
	if isinstance(value, dict):
		return value
	if not value:
		return {}
	try:
		return frappe.parse_json(value) or {}
	except Exception:
		return {}


def _normalize_plain_text(value):
	text = cstr(value or "")
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	text = re.sub(r"[ \t]+\n", "\n", text)
	text = re.sub(r"\n{3,}", "\n\n", text)
	lines = [line.strip() for line in text.split("\n")]
	return "\n".join(lines).strip()


def _resolve_sender_raven_user(doc):
	"""Resolve Raven User for the Raven Message owner."""
	owner = cstr(doc.get("owner") or "").strip()
	if not owner:
		return None
	return frappe.db.get_value("Raven User", {"user": owner, "enabled": 1}, "name")
