"""Conversation and message-link helpers for the WhatsApp/Raven bridge."""

import re

import frappe
from frappe import _

from whatsapp_raven_bridge.bridge.account_route import get_route_for_whatsapp_account
from whatsapp_raven_bridge.utils.settings import bridge_user_context, get_bridge_identity

CONVERSATION_DOCTYPE = "WhatsApp Raven Conversation"
MESSAGE_LINK_DOCTYPE = "WhatsApp Raven Message Link"


def normalize_phone_number(phone):
	"""Return WhatsApp phone input as digits only."""
	if phone is None:
		return ""

	value = str(phone).strip()
	if value.lower().startswith("whatsapp:"):
		value = value[len("whatsapp:") :]

	return re.sub(r"\D+", "", value)


def get_conversation_filters(phone_number, whatsapp_account=None):
	"""Return filters for finding an enabled conversation by account and phone."""
	filters = {
		"enabled": 1,
		"phone_number": normalize_phone_number(phone_number),
	}

	if whatsapp_account:
		filters["whatsapp_account"] = whatsapp_account
	else:
		filters["whatsapp_account"] = ["is", "not set"]

	return filters


def get_or_create_conversation(
	phone_number,
	whatsapp_account=None,
	profile_name=None,
	raven_workspace=None,
	conversation_strategy=None,
):
	"""Return an enabled conversation for phone/account, creating one if needed."""
	normalized_phone = normalize_phone_number(phone_number)
	if not normalized_phone:
		frappe.throw(_("Phone number is required after normalization."))

	existing = _find_conversation_name(normalized_phone, whatsapp_account)
	if existing:
		conversation = frappe.get_doc(CONVERSATION_DOCTYPE, existing)
		_assign_route_to_conversation(conversation, whatsapp_account)
		return conversation

	identity = get_bridge_identity()
	route = get_route_for_whatsapp_account(whatsapp_account)
	route_name = route.name if route else None
	raven_workspace_value = raven_workspace or (route.raven_workspace if route else None) or identity.default_raven_workspace
	conversation_strategy_value = (
		conversation_strategy
		or (route.conversation_strategy if route else None)
		or identity.conversation_strategy
		or "Channel Per Contact"
	)
	conversation = frappe.get_doc(
		{
			"doctype": CONVERSATION_DOCTYPE,
			"enabled": 1,
			"phone_number": normalized_phone,
			"whatsapp_account": whatsapp_account,
			"account_route": route_name,
			"profile_name": profile_name,
			"display_name": profile_name or normalized_phone,
			"raven_workspace": raven_workspace_value,
			"conversation_strategy": conversation_strategy_value,
			"status": "Open",
		}
	)
	with bridge_user_context():
		conversation.insert(ignore_permissions=True)
	return conversation


def find_conversation_by_raven_channel(channel_id):
	"""Return the active conversation document for a Raven Channel, if one exists."""
	if not channel_id:
		return None

	name = frappe.db.exists(
		CONVERSATION_DOCTYPE,
		{
			"enabled": 1,
			"raven_channel": channel_id,
		},
	)
	return frappe.get_doc(CONVERSATION_DOCTYPE, name) if name else None


def get_existing_message_link_by_whatsapp_message(whatsapp_message_name):
	return _get_existing_message_link("whatsapp_message", whatsapp_message_name)


def get_existing_message_link_by_whatsapp_message_id(whatsapp_message_id):
	return _get_existing_message_link("whatsapp_message_id", whatsapp_message_id)


def get_existing_message_link_by_raven_message(raven_message_name):
	return _get_existing_message_link("raven_message", raven_message_name)


def create_message_link(
	*,
	conversation,
	direction,
	whatsapp_message=None,
	whatsapp_message_id=None,
	raven_message=None,
	raven_channel=None,
	content_type=None,
	sync_status="Synced",
	error=None,
	metadata=None,
):
	"""Create and return a WhatsApp/Raven message link."""
	link = frappe.get_doc(
		{
			"doctype": MESSAGE_LINK_DOCTYPE,
			"conversation": conversation,
			"direction": direction,
			"whatsapp_message": whatsapp_message,
			"whatsapp_message_id": whatsapp_message_id,
			"raven_message": raven_message,
			"raven_channel": raven_channel,
			"content_type": content_type,
			"sync_status": sync_status,
			"error": error,
			"metadata": metadata,
		}
	)
	link.insert(ignore_permissions=True)
	return link


def _find_conversation_name(phone_number, whatsapp_account=None):
	filters = [
		["enabled", "=", 1],
		["phone_number", "=", phone_number],
	]

	if whatsapp_account:
		filters.append(["whatsapp_account", "=", whatsapp_account])
		names = frappe.get_all(CONVERSATION_DOCTYPE, filters=filters, pluck="name", limit=1)
	else:
		names = frappe.get_all(
			CONVERSATION_DOCTYPE,
			filters=filters,
			or_filters=[
				["whatsapp_account", "is", "not set"],
				["whatsapp_account", "=", ""],
			],
			pluck="name",
			limit=1,
		)

	return names[0] if names else None


def _get_existing_message_link(fieldname, value):
	if not value:
		return None

	return frappe.db.exists(MESSAGE_LINK_DOCTYPE, {fieldname: value})


def _assign_route_to_conversation(conversation, whatsapp_account):
	"""Backfill conversation.account_route for legacy conversations when route is configured."""
	if not whatsapp_account:
		return

	route = get_route_for_whatsapp_account(whatsapp_account)
	if not route:
		return

	updates = {}
	if conversation.account_route != route.name:
		updates["account_route"] = route.name
	if not conversation.raven_workspace and route.raven_workspace:
		updates["raven_workspace"] = route.raven_workspace
	if conversation.conversation_strategy != route.conversation_strategy and route.conversation_strategy:
		updates["conversation_strategy"] = route.conversation_strategy

	if updates:
		with bridge_user_context():
			conversation.update(updates)
			conversation.save(ignore_permissions=True)
