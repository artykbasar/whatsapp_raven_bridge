"""Raven destination resolver for WhatsApp Raven Conversation records."""

from __future__ import annotations

import re
from contextlib import nullcontext

import frappe
from frappe import _
from frappe.utils import cint, cstr, escape_html, now_datetime

from whatsapp_raven_bridge.bridge.account_route import (
	ensure_route_memberships,
	get_or_create_inbox_channel,
	get_route_for_whatsapp_account,
)
from whatsapp_raven_bridge.utils.settings import bridge_user_context, get_settings

CONVERSATION_DOCTYPE = "WhatsApp Raven Conversation"
RAVEN_CHANNEL_DOCTYPE = "Raven Channel"
RAVEN_CHANNEL_MEMBER_DOCTYPE = "Raven Channel Member"
RAVEN_MESSAGE_DOCTYPE = "Raven Message"
RAVEN_WORKSPACE_MEMBER_DOCTYPE = "Raven Workspace Member"


def sanitize_channel_name(value):
	"""Return a safe, deterministic Raven channel name fragment."""
	text = str(value or "").strip().lower()
	text = re.sub(r"[^a-z0-9]+", "-", text)
	text = re.sub(r"-{2,}", "-", text).strip("-")
	return text or "whatsapp-conversation"


def get_default_channel_members(settings=None):
	"""Return default members configured in bridge settings."""
	settings = settings or get_settings()
	if not settings:
		return []

	members = []
	for row in settings.get("default_channel_members") or []:
		raven_user = row.get("raven_user")
		if not raven_user:
			continue
		members.append(
			{
				"raven_user": raven_user,
				"is_admin": cint(row.get("is_admin")),
			}
		)
	return members


def ensure_raven_destination(conversation):
	"""Create or reuse a Raven Channel for the given WhatsApp Raven Conversation."""
	conversation_doc = _get_conversation_doc(conversation)

	settings = get_settings()
	if not settings or not cint(settings.get("enabled")):
		frappe.throw(_("WhatsApp Raven Bridge is disabled in settings."))

	route = get_route_for_whatsapp_account(conversation_doc.whatsapp_account)
	if route:
		return _ensure_route_destination(conversation_doc, route, settings)

	if conversation_doc.conversation_strategy == "Thread Per Contact":
		frappe.throw(
			_("Thread Per Contact is reserved for a later phase and is not implemented yet."),
			exc=NotImplementedError,
		)

	if conversation_doc.conversation_strategy != "Channel Per Contact":
		frappe.throw(
			_("Unsupported conversation strategy: {0}").format(conversation_doc.conversation_strategy or "None")
		)

	if not settings.get("default_raven_workspace"):
		frappe.throw(_("Default Raven Workspace is required in WhatsApp Raven Bridge Settings."))

	if not settings.get("default_channel_type"):
		frappe.throw(_("Default Channel Type is required in WhatsApp Raven Bridge Settings."))

	return _ensure_global_channel_per_contact_destination(conversation_doc, settings)


def _ensure_route_destination(conversation_doc, route, settings):
	"""Resolve destination for configured account route."""
	route = _get_route_doc(route)
	inbox_channel = get_or_create_inbox_channel(route)
	ensure_route_memberships(route)

	strategy = route.conversation_strategy or "Thread Per Contact"
	if strategy == "Thread Per Contact":
		return ensure_thread_destination(conversation_doc, route, settings=settings)

	if strategy != "Channel Per Contact":
		frappe.throw(_("Unsupported route conversation strategy: {0}").format(strategy))

	workspace = route.raven_workspace
	channel_type = route.channel_type or "Private"
	raven_channel = _find_or_create_conversation_channel(
		conversation_doc=conversation_doc,
		workspace=workspace,
		channel_type=channel_type,
	)

	# Ensure route members can see the per-contact destination while Channel Per Contact is active.
	ensure_route_memberships(route)
	for row in route.members or []:
		if row.raven_user:
			ensure_channel_member(raven_channel.name, row.raven_user, is_admin=cint(row.is_admin))

	bridge_raven_user = settings.get("bridge_raven_user") if settings else None
	if bridge_raven_user:
		ensure_channel_member(raven_channel.name, bridge_raven_user, is_admin=1)

	updates = {
		"account_route": route.name,
		"raven_channel": raven_channel.name,
		"raven_workspace": workspace,
		"conversation_strategy": strategy,
	}
	_save_conversation_updates(conversation_doc, updates)
	return raven_channel


def ensure_thread_destination(conversation, route, settings=None):
	"""Resolve destination as a Raven thread under the route inbox channel."""
	conversation_doc = _get_conversation_doc(conversation)
	route_doc = _get_route_doc(route)
	settings = settings or get_settings()

	if not settings or not cint(settings.get("enabled")):
		frappe.throw(_("WhatsApp Raven Bridge is disabled in settings."))

	if not settings.get("bridge_raven_user"):
		frappe.throw(_("Bridge Raven User is required in WhatsApp Raven Bridge Settings."))

	inbox_channel = get_or_create_inbox_channel(route_doc)
	ensure_route_memberships(route_doc)

	parent_message = _get_existing_parent_thread_message(conversation_doc)
	thread_channel = _get_existing_thread_channel(conversation_doc, parent_message)

	if not parent_message:
		parent_message = _create_parent_thread_message(conversation_doc, route_doc, settings, inbox_channel)

	if not thread_channel:
		thread_channel = _create_or_get_thread_channel(parent_message, inbox_channel)

	if not cint(parent_message.is_thread):
		frappe.db.set_value(
			RAVEN_MESSAGE_DOCTYPE,
			parent_message.name,
			"is_thread",
			1,
			update_modified=False,
		)

	updates = {
		"account_route": route_doc.name,
		"conversation_strategy": "Thread Per Contact",
		"raven_workspace": route_doc.raven_workspace or inbox_channel.workspace,
		"parent_raven_message": parent_message.name,
		"raven_channel": thread_channel.name,
	}
	_save_conversation_updates(conversation_doc, updates)

	ensure_thread_memberships(thread_channel, route_doc, settings=settings)
	return thread_channel


def ensure_thread_memberships(thread_channel, route, settings=None):
	"""Ensure route + bridge users are members of the thread channel and workspace."""
	route_doc = _get_route_doc(route)
	thread_doc = thread_channel if hasattr(thread_channel, "doctype") else frappe.get_doc(RAVEN_CHANNEL_DOCTYPE, thread_channel)
	settings = settings or get_settings()

	members = {}
	for row in route_doc.members or []:
		if row.raven_user:
			members[row.raven_user] = cint(row.is_admin)

	bridge_raven_user = settings.get("bridge_raven_user") if settings else None
	if bridge_raven_user:
		members[bridge_raven_user] = 1

	for raven_user, is_admin in members.items():
		ensure_workspace_member(route_doc.raven_workspace, raven_user, is_admin=is_admin)
		ensure_channel_member(thread_doc.name, raven_user, is_admin=is_admin)

	return thread_doc


def _ensure_global_channel_per_contact_destination(conversation_doc, settings):
	"""Resolve destination via legacy global settings fallback."""
	raven_channel = None
	workspace = conversation_doc.raven_workspace or settings.get("default_raven_workspace")

	if conversation_doc.raven_channel and frappe.db.exists(RAVEN_CHANNEL_DOCTYPE, conversation_doc.raven_channel):
		raven_channel = frappe.get_doc(RAVEN_CHANNEL_DOCTYPE, conversation_doc.raven_channel)
	else:
		if not conversation_doc.phone_number:
			frappe.throw(_("Conversation phone number is required to resolve a Raven destination."))

		channel_type = settings.get("default_channel_type") or "Private"
		raven_channel = _find_or_create_conversation_channel(
			conversation_doc=conversation_doc,
			workspace=workspace,
			channel_type=channel_type,
		)

	workspace = raven_channel.workspace or workspace

	members_to_ensure = []
	bridge_raven_user = settings.get("bridge_raven_user")
	if bridge_raven_user:
		members_to_ensure.append({"raven_user": bridge_raven_user, "is_admin": 1})
	members_to_ensure.extend(get_default_channel_members(settings))

	seen_members = set()
	for member in members_to_ensure:
		raven_user = member.get("raven_user")
		if not raven_user:
			continue
		is_admin = cint(member.get("is_admin"))
		if raven_user in seen_members:
			continue
		seen_members.add(raven_user)
		try:
			ensure_workspace_member(
				workspace=workspace,
				raven_user=raven_user,
				is_admin=is_admin,
			)
			ensure_channel_member(
				channel_id=raven_channel.name,
				raven_user=raven_user,
				is_admin=is_admin,
			)
		except Exception:
			frappe.log_error(
				title="WhatsApp Raven Bridge: Failed to ensure bridge membership",
				message=frappe.get_traceback(),
			)
			raise

	updates = {
		"raven_channel": raven_channel.name,
		"raven_workspace": workspace,
	}
	_save_conversation_updates(conversation_doc, updates)

	return raven_channel


def _find_or_create_conversation_channel(conversation_doc, workspace, channel_type):
	channel_name = sanitize_channel_name(f"whatsapp-{conversation_doc.phone_number}")

	channel_name_or_id = frappe.db.exists(
		RAVEN_CHANNEL_DOCTYPE,
		{
			"linked_doctype": CONVERSATION_DOCTYPE,
			"linked_document": conversation_doc.name,
		},
	)

	if not channel_name_or_id:
		channel_name_or_id = frappe.db.exists(
			RAVEN_CHANNEL_DOCTYPE,
			{
				"workspace": workspace,
				"channel_name": channel_name,
				"is_direct_message": 0,
				"is_thread": 0,
			},
		)

	if channel_name_or_id:
		return frappe.get_doc(RAVEN_CHANNEL_DOCTYPE, channel_name_or_id)

	channel = frappe.get_doc(
		{
			"doctype": RAVEN_CHANNEL_DOCTYPE,
			"type": channel_type,
			"channel_name": channel_name,
			"workspace": workspace,
			"linked_doctype": CONVERSATION_DOCTYPE,
			"linked_document": conversation_doc.name,
			"is_thread": 0,
			"is_direct_message": 0,
		}
	)
	# Prevent Raven from auto-inserting session user as member for bridge-created channels.
	with bridge_user_context():
		channel.flags.do_not_add_member = True
		channel.insert(ignore_permissions=True)
	return channel


def _save_conversation_updates(conversation_doc, updates):
	changed = False
	for fieldname, value in updates.items():
		if conversation_doc.get(fieldname) != value:
			conversation_doc.set(fieldname, value)
			changed = True
	if changed:
		with bridge_user_context():
			conversation_doc.save(ignore_permissions=True)


def _get_existing_parent_thread_message(conversation_doc):
	parent_name = cstr(conversation_doc.parent_raven_message or "").strip()
	if not parent_name:
		return None
	if frappe.db.exists(RAVEN_MESSAGE_DOCTYPE, parent_name):
		return frappe.get_doc(RAVEN_MESSAGE_DOCTYPE, parent_name)
	return None


def _get_existing_thread_channel(conversation_doc, parent_message=None):
	channel_name = cstr(conversation_doc.raven_channel or "").strip()
	if channel_name and frappe.db.exists(RAVEN_CHANNEL_DOCTYPE, channel_name):
		channel = frappe.get_doc(RAVEN_CHANNEL_DOCTYPE, channel_name)
		if cint(channel.is_thread):
			return channel

	parent_name = parent_message.name if parent_message else cstr(conversation_doc.parent_raven_message or "").strip()
	if parent_name and frappe.db.exists(RAVEN_CHANNEL_DOCTYPE, parent_name):
		channel = frappe.get_doc(RAVEN_CHANNEL_DOCTYPE, parent_name)
		if cint(channel.is_thread):
			return channel

	return None


def _create_parent_thread_message(conversation_doc, route_doc, settings, inbox_channel):
	phone = escape_html(cstr(conversation_doc.phone_number or "unknown"))
	text = f"<p><strong>WhatsApp conversation</strong> <code>{phone}</code></p>"
	metadata = {
		"source": "whatsapp_raven_bridge",
		"purpose": "thread_parent",
		"conversation": conversation_doc.name,
		"phone_number": conversation_doc.phone_number,
		"whatsapp_account": conversation_doc.whatsapp_account,
		"account_route": route_doc.name,
	}

	previous_flag = getattr(frappe.flags, "whatsapp_raven_bridge_syncing", False)
	try:
		frappe.flags.whatsapp_raven_bridge_syncing = True
		with bridge_user_context():
			return frappe.get_doc(
				{
					"doctype": RAVEN_MESSAGE_DOCTYPE,
					"channel_id": inbox_channel.name,
					"message_type": "Text",
					"text": text,
					"is_bot_message": 1,
					"bot": settings.get("bridge_raven_user"),
					"link_doctype": CONVERSATION_DOCTYPE,
					"link_document": conversation_doc.name,
					"json": metadata,
				}
			).insert(ignore_permissions=True)
	finally:
		frappe.flags.whatsapp_raven_bridge_syncing = previous_flag


def _create_or_get_thread_channel(parent_message, inbox_channel):
	thread_name = parent_message.name
	if frappe.db.exists(RAVEN_CHANNEL_DOCTYPE, thread_name):
		channel = frappe.get_doc(RAVEN_CHANNEL_DOCTYPE, thread_name)
		if cint(channel.is_thread):
			if not cint(parent_message.is_thread):
				frappe.db.set_value(
					RAVEN_MESSAGE_DOCTYPE,
					parent_message.name,
					"is_thread",
					1,
					update_modified=False,
				)
			return channel

	channel = frappe.get_doc(
		{
			"doctype": RAVEN_CHANNEL_DOCTYPE,
			"channel_name": parent_message.name,
			"workspace": inbox_channel.workspace,
			"type": inbox_channel.type or "Private",
			"is_thread": 1,
			"is_dm_thread": cint(inbox_channel.is_direct_message),
			"channel_description": cstr(parent_message.content or "")[:140],
		}
	)
	with bridge_user_context():
		channel.flags.do_not_add_member = True
		channel.insert(ignore_permissions=True)

	if not cint(parent_message.is_thread):
		with bridge_user_context():
			frappe.db.set_value(
				RAVEN_MESSAGE_DOCTYPE,
				parent_message.name,
				"is_thread",
				1,
				update_modified=False,
			)

	return channel


def ensure_channel_member(channel_id, raven_user, is_admin=0, use_bridge_context=True):
	"""Create a Raven Channel Member if missing and return the member document."""
	if not channel_id or not raven_user:
		return None

	ctx = bridge_user_context() if use_bridge_context else nullcontext()
	with ctx:
		existing = frappe.db.exists(
			RAVEN_CHANNEL_MEMBER_DOCTYPE,
			{
				"channel_id": channel_id,
				"user_id": raven_user,
			},
		)
		if existing:
			if cint(is_admin):
				frappe.db.set_value(
					RAVEN_CHANNEL_MEMBER_DOCTYPE,
					existing,
					"is_admin",
					1,
					update_modified=False,
				)
			return frappe.get_doc(RAVEN_CHANNEL_MEMBER_DOCTYPE, existing)

		try:
			member = frappe.get_doc(
				{
					"doctype": RAVEN_CHANNEL_MEMBER_DOCTYPE,
					"channel_id": channel_id,
					"user_id": raven_user,
					"is_admin": cint(is_admin),
					"last_visit": now_datetime(),
					"allow_notifications": 1,
				}
			).insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			member = None

		existing = frappe.db.exists(
			RAVEN_CHANNEL_MEMBER_DOCTYPE,
			{
				"channel_id": channel_id,
				"user_id": raven_user,
			},
		)
		if existing and cint(is_admin):
			frappe.db.set_value(
				RAVEN_CHANNEL_MEMBER_DOCTYPE,
				existing,
				"is_admin",
				1,
				update_modified=False,
			)
		return frappe.get_doc(RAVEN_CHANNEL_MEMBER_DOCTYPE, existing) if existing else member


def ensure_workspace_member(workspace, raven_user, is_admin=0, use_bridge_context=True):
	"""Create a Raven Workspace Member if missing and return the member document."""
	if not workspace or not raven_user:
		return None

	ctx = bridge_user_context() if use_bridge_context else nullcontext()
	with ctx:
		existing = frappe.db.exists(
			RAVEN_WORKSPACE_MEMBER_DOCTYPE,
			{
				"workspace": workspace,
				"user": raven_user,
			},
		)
		if existing:
			if cint(is_admin):
				frappe.db.set_value(
					RAVEN_WORKSPACE_MEMBER_DOCTYPE,
					existing,
					"is_admin",
					1,
					update_modified=False,
				)
			return frappe.get_doc(RAVEN_WORKSPACE_MEMBER_DOCTYPE, existing)

		try:
			member = frappe.get_doc(
				{
					"doctype": RAVEN_WORKSPACE_MEMBER_DOCTYPE,
					"workspace": workspace,
					"user": raven_user,
					"is_admin": cint(is_admin),
				}
			).insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			member = None

		existing = frappe.db.exists(
			RAVEN_WORKSPACE_MEMBER_DOCTYPE,
			{
				"workspace": workspace,
				"user": raven_user,
			},
		)
		if existing and cint(is_admin):
			frappe.db.set_value(
				RAVEN_WORKSPACE_MEMBER_DOCTYPE,
				existing,
				"is_admin",
				1,
				update_modified=False,
			)
		return frappe.get_doc(RAVEN_WORKSPACE_MEMBER_DOCTYPE, existing) if existing else member


def _get_conversation_doc(conversation):
	if isinstance(conversation, str):
		return frappe.get_doc(CONVERSATION_DOCTYPE, conversation)
	return conversation


def _get_route_doc(route):
	if isinstance(route, str):
		return frappe.get_doc("WhatsApp Raven Account Route", route)
	return route
