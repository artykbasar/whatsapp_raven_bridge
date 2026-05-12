from __future__ import annotations

import frappe
from frappe.utils import cint, cstr

DEFAULT_BRIDGE_SYSTEM_USER_EMAIL = "whatsapp.bridge@example.com"


def execute():
	"""Repair stale/missing bridge_system_user single value during migrate."""
	if not frappe.db.exists("DocType", "WhatsApp Raven Bridge Settings"):
		return
	if not frappe.db.table_exists("User"):
		return

	current_value = cstr(
		frappe.db.get_single_value("WhatsApp Raven Bridge Settings", "bridge_system_user")
	).strip()

	if not current_value:
		target_user = _ensure_user(DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)
	elif frappe.db.exists("User", current_value):
		target_user = _ensure_user(current_value)
	elif "@" in current_value:
		target_user = _ensure_user(current_value)
	else:
		target_user = _ensure_user(DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)

	if target_user:
		frappe.db.set_single_value(
			"WhatsApp Raven Bridge Settings",
			"bridge_system_user",
			target_user,
		)


def _ensure_user(user_identifier: str) -> str | None:
	user_identifier = cstr(user_identifier).strip()
	if not user_identifier:
		return None

	user_name = None
	if frappe.db.exists("User", user_identifier):
		user_name = user_identifier
	elif "@" in user_identifier:
		user_name = frappe.db.get_value("User", {"email": user_identifier}, "name")

	if user_name:
		user_doc = frappe.get_doc("User", user_name)
		if not cint(user_doc.enabled):
			user_doc.enabled = 1
			user_doc.save(ignore_permissions=True)
		return user_doc.name

	if "@" not in user_identifier:
		return None

	user_doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user_identifier,
			"first_name": "WhatsApp Bridge",
			"last_name": "Service",
			"send_welcome_email": 0,
			"enabled": 1,
			"user_type": "System User",
		}
	).insert(ignore_permissions=True)
	return user_doc.name

