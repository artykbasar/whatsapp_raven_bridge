"""Inbound WhatsApp text sync to Raven."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr, now

from whatsapp_raven_bridge.bridge.conversation import (
	create_message_link,
	get_existing_message_link_by_whatsapp_message,
	get_existing_message_link_by_whatsapp_message_id,
	get_or_create_conversation,
	normalize_phone_number,
)
from whatsapp_raven_bridge.bridge.raven_destination import ensure_raven_destination
from whatsapp_raven_bridge.bridge.whatsapp_message_rendering import (
	build_whatsapp_origin_message_content,
	build_whatsapp_origin_message_html,
	incoming_header_label,
)
from whatsapp_raven_bridge.utils.settings import get_settings


def handle_whatsapp_message_after_insert(doc, method=None):
	"""Hook entrypoint for WhatsApp Message.after_insert."""
	try:
		process_incoming_whatsapp_message(doc)
	except Exception as error:
		log_inbound_error(doc, error)
	return None


def process_incoming_whatsapp_message(doc):
	"""Process one incoming WhatsApp Message and mirror it to Raven."""
	if getattr(frappe.flags, "in_install", False) or getattr(frappe.flags, "in_migrate", False) or getattr(
		frappe.flags, "in_patch", False
	):
		return "skipped_setup"

	if getattr(frappe.flags, "whatsapp_raven_bridge_syncing", False):
		return "skipped_syncing"

	settings = get_settings()
	if not settings or not settings.get("enabled"):
		return "skipped_disabled"

	if not settings.get("bridge_raven_user"):
		return "skipped_missing_bridge_raven_user"

	if cstr(getattr(doc, "doctype", "")).strip() != "WhatsApp Message":
		return "skipped_invalid_doctype"

	if cstr(doc.get("type")) != "Incoming":
		return "skipped_outgoing"

	if not is_supported_incoming_text(doc):
		return "skipped_unsupported_content_type"

	source_phone = cstr(doc.get("from")).strip()
	if not source_phone:
		return "skipped_missing_phone"

	existing_link_by_whatsapp_message = get_existing_message_link_by_whatsapp_message(doc.name)
	if existing_link_by_whatsapp_message:
		return "skipped_existing_whatsapp_message"

	if doc.get("message_id"):
		existing_link_by_whatsapp_message_id = get_existing_message_link_by_whatsapp_message_id(
			doc.get("message_id")
		)
		if existing_link_by_whatsapp_message_id:
			return "skipped_existing_whatsapp_message_id"

	existing_raven_message = find_existing_raven_message_for_whatsapp(doc)
	if existing_raven_message:
		create_message_link(
			conversation=get_or_create_conversation(
				source_phone,
				whatsapp_account=doc.get("whatsapp_account"),
				profile_name=doc.get("profile_name"),
			).name,
			direction="Incoming",
			whatsapp_message=doc.name,
			whatsapp_message_id=doc.get("message_id"),
			raven_message=existing_raven_message.name,
			raven_channel=existing_raven_message.channel_id,
			content_type=doc.get("content_type"),
			sync_status="Synced",
			metadata=build_raven_metadata(doc, normalize_phone_number(source_phone)),
		)
		return "skipped_existing_raven_message"

	normalized_phone = normalize_phone_number(source_phone)
	conversation = get_or_create_conversation(
		source_phone,
		whatsapp_account=doc.get("whatsapp_account"),
		profile_name=doc.get("profile_name"),
	)
	raven_channel = ensure_raven_destination(conversation)

	raven_text = build_raven_text_from_whatsapp(doc, normalized_phone)
	metadata = build_raven_metadata(doc, normalized_phone)

	try:
		frappe.flags.whatsapp_raven_bridge_syncing = True

		raven_message = frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": raven_channel.name,
				"message_type": "Text",
				"text": raven_text,
				"content": build_whatsapp_origin_message_content(
					incoming_header_label(doc.get("profile_name"), normalized_phone),
					doc.get("message"),
				),
				"json": metadata,
				"is_bot_message": 1,
				"bot": settings.get("bridge_raven_user"),
				"hide_link_preview": 1,
			}
		).insert(ignore_permissions=True)

		message_link = create_message_link(
			conversation=conversation.name,
			direction="Incoming",
			whatsapp_message=doc.name,
			whatsapp_message_id=doc.get("message_id"),
			raven_message=raven_message.name,
			raven_channel=raven_channel.name,
			content_type=doc.get("content_type"),
			sync_status="Synced",
			metadata=metadata,
		)

		conversation.last_inbound_at = now()
		conversation.last_inbound_whatsapp_message = doc.name
		conversation.last_whatsapp_message_id = doc.get("message_id")
		conversation.last_raven_message = raven_message.name

		if doc.get("profile_name"):
			conversation.profile_name = doc.get("profile_name")
			if not conversation.display_name or conversation.display_name == conversation.phone_number:
				conversation.display_name = doc.get("profile_name")

		conversation.save(ignore_permissions=True)
	finally:
		frappe.flags.whatsapp_raven_bridge_syncing = False

	return frappe._dict(
		{
			"status": "synced",
			"conversation": conversation.name,
			"channel": raven_channel.name,
			"raven_message": raven_message.name,
			"link": message_link.name,
			"idempotency_key": get_whatsapp_message_idempotency_key(doc),
		}
	)


def is_supported_incoming_text(doc):
	return cstr(doc.get("type")) == "Incoming" and cstr(doc.get("content_type")).lower() == "text"


def get_whatsapp_message_idempotency_key(doc):
	return cstr(doc.get("message_id")).strip() or cstr(doc.get("name")).strip()


def build_raven_text_from_whatsapp(doc, normalized_phone):
	return build_whatsapp_origin_message_html(
		whatsapp_message_name=doc.name,
		header_label=incoming_header_label(doc.get("profile_name"), normalized_phone),
		body_text=doc.get("message"),
		highlight_header=True,
	)


def build_raven_metadata(doc, normalized_phone):
	return {
		"source": "whatsapp",
		"direction": "incoming",
		"whatsapp_message": doc.name,
		"whatsapp_message_id": doc.get("message_id"),
		"idempotency_key": get_whatsapp_message_idempotency_key(doc),
		"phone_number": normalized_phone,
		"profile_name": doc.get("profile_name"),
		"whatsapp_account": doc.get("whatsapp_account"),
		"content_type": doc.get("content_type"),
		"is_reply": doc.get("is_reply"),
		"reply_to_message_id": doc.get("reply_to_message_id"),
	}


def find_existing_raven_message_for_whatsapp(doc):
	message_name = frappe.db.exists(
		"Raven Message",
		{
			"link_doctype": "WhatsApp Message",
			"link_document": doc.name,
		},
	)
	return frappe.get_doc("Raven Message", message_name) if message_name else None


def log_inbound_error(doc, error):
	message_id = cstr(doc.get("message_id") if doc else "").strip()
	docname = cstr(getattr(doc, "name", "")).strip()
	context = {
		"whatsapp_message": docname,
		"message_id": message_id,
		"type": cstr(doc.get("type") if doc else ""),
		"content_type": cstr(doc.get("content_type") if doc else ""),
		"from": cstr(doc.get("from") if doc else ""),
		"error": repr(error),
	}
	frappe.log_error(
		title="WhatsApp Raven Bridge: inbound sync error",
		message=json.dumps(context, indent=2, default=str) + "\n\n" + frappe.get_traceback(),
	)
