"""Helpers for per-account WhatsApp to Raven route configuration."""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from whatsapp_raven_bridge.utils.settings import bridge_user_context, get_settings

ROUTE_DOCTYPE = "WhatsApp Raven Account Route"


def get_route_for_whatsapp_account(whatsapp_account):
	"""Return enabled route for a WhatsApp Account, if configured."""
	if not whatsapp_account:
		return None

	route_name = frappe.db.get_value(
		ROUTE_DOCTYPE,
		{"enabled": 1, "whatsapp_account": whatsapp_account},
		"name",
	)
	return frappe.get_doc(ROUTE_DOCTYPE, route_name) if route_name else None


def get_default_route():
	"""Return default enabled route when an account-specific route is not explicitly passed."""
	settings = get_settings()
	if settings and settings.get("default_whatsapp_account"):
		route = get_route_for_whatsapp_account(settings.get("default_whatsapp_account"))
		if route:
			return route

	route_name = frappe.db.get_value(ROUTE_DOCTYPE, {"enabled": 1}, "name", order_by="modified desc")
	return frappe.get_doc(ROUTE_DOCTYPE, route_name) if route_name else None


def get_or_create_inbox_channel(route):
	"""Return the route inbox channel, creating/reusing one when missing."""
	route_doc = _get_route_doc(route)

	if route_doc.inbox_channel:
		channel = frappe.get_doc("Raven Channel", route_doc.inbox_channel)
		_validate_existing_inbox_channel(route_doc, channel)
		return channel

	channel_name = normalize_inbox_channel_name(route_doc.inbox_channel_name)
	if not channel_name:
		account_slug = normalize_inbox_channel_name(route_doc.whatsapp_account or "account")
		channel_name = f"whatsapp-inbox-{account_slug}"

	channel_id = frappe.db.get_value(
		"Raven Channel",
		{
			"workspace": route_doc.raven_workspace,
			"channel_name": channel_name,
			"is_direct_message": 0,
			"is_thread": 0,
		},
		"name",
	)
	if channel_id:
		channel = frappe.get_doc("Raven Channel", channel_id)
	else:
		with bridge_user_context():
			channel = frappe.get_doc(
				{
					"doctype": "Raven Channel",
					"type": route_doc.channel_type or "Private",
					"workspace": route_doc.raven_workspace,
					"channel_name": channel_name,
				}
			)
			channel.flags.do_not_add_member = True
			channel.insert(ignore_permissions=True)

	with bridge_user_context():
		route_doc.inbox_channel = channel.name
		route_doc.inbox_channel_name = channel.channel_name
		route_doc.save(ignore_permissions=True)
	return channel


def ensure_route_memberships(route):
	"""Ensure route members can see route workspace + inbox channel."""
	route_doc = _get_route_doc(route)
	channel = get_or_create_inbox_channel(route_doc)

	from whatsapp_raven_bridge.bridge.raven_destination import ensure_channel_member, ensure_workspace_member

	bridge_raven_user = None
	settings = get_settings()
	if settings:
		bridge_raven_user = settings.get("bridge_raven_user")

	members = {row.raven_user: row for row in (route_doc.members or []) if row.raven_user}
	if bridge_raven_user and bridge_raven_user not in members:
		members[bridge_raven_user] = frappe._dict({"raven_user": bridge_raven_user, "is_admin": 1})

	for row in members.values():
		raven_user = row.raven_user
		is_admin = cint(row.get("is_admin"))
		ensure_workspace_member(route_doc.raven_workspace, raven_user, is_admin=is_admin)
		ensure_channel_member(channel.name, raven_user, is_admin=is_admin)

	return channel


def is_raven_user_allowed_for_route(route, raven_user):
	"""Return True when a Raven User is explicitly assigned to the route."""
	if not raven_user:
		return False

	route_doc = _get_route_doc(route)
	return bool(
		frappe.db.exists(
			"WhatsApp Raven Account Route Member",
			{
				"parenttype": ROUTE_DOCTYPE,
				"parent": route_doc.name,
				"raven_user": raven_user,
			},
		)
	)


def is_raven_user_allowed_to_reply(route, raven_user):
	"""Return True when the Raven User can send outbound replies for the route."""
	if not raven_user:
		return False

	route_doc = _get_route_doc(route)
	member = frappe.db.get_value(
		"WhatsApp Raven Account Route Member",
		{
			"parenttype": ROUTE_DOCTYPE,
			"parent": route_doc.name,
			"raven_user": raven_user,
		},
		["name", "can_reply"],
		as_dict=True,
	)
	if member:
		return bool(cint(member.can_reply))

	return bool(cint(route_doc.allow_unassigned_reply))


def normalize_inbox_channel_name(value):
	text = cstr(value or "").strip().lower()
	text = re.sub(r"[^a-z0-9]+", "-", text)
	text = re.sub(r"-{2,}", "-", text).strip("-")
	return text


def _validate_existing_inbox_channel(route_doc, channel):
	if channel.workspace != route_doc.raven_workspace:
		frappe.throw(
			_("Inbox Channel {0} must belong to Raven Workspace {1}.").format(
				channel.name, route_doc.raven_workspace
			)
		)
	if cint(channel.is_direct_message):
		frappe.throw(_("Inbox Channel cannot be a direct message channel."))


def _get_route_doc(route):
	if isinstance(route, str):
		return frappe.get_doc(ROUTE_DOCTYPE, route)
	return route
