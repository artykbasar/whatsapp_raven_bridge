# Copyright (c) 2026, Local and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr

from whatsapp_raven_bridge.api.setup import (
	_ensure_bridge_system_user_with_state,
	_ensure_default_bridge_system_user_state,
)


class WhatsAppRavenBridgeSettings(Document):
	def _validate_links(self):
		self._ensure_bridge_system_user_link()
		return super()._validate_links()

	def validate(self):
		self._ensure_bridge_system_user_link()

	def _ensure_bridge_system_user_link(self):
		user_ref = cstr(self.bridge_system_user).strip()

		if not user_ref:
			ensured = _ensure_default_bridge_system_user_state(update_settings=False)
			user_name = cstr((ensured or {}).get("user") or "").strip()
			if not user_name:
				frappe.throw(_("Could not create or resolve the default Bridge System User."))
			self.bridge_system_user = user_name
			return

		if frappe.db.exists("User", user_ref):
			ensured = _ensure_bridge_system_user_with_state(user_ref)
			self.bridge_system_user = cstr((ensured or {}).get("user") or user_ref).strip()
			return

		if "@" in user_ref:
			ensured = _ensure_bridge_system_user_with_state(user_ref)
			user_name = cstr((ensured or {}).get("user") or "").strip()
			if not user_name:
				frappe.throw(_("Could not create or resolve Bridge System User: {0}").format(user_ref))
			self.bridge_system_user = user_name
			return

		if "@" not in user_ref:
			frappe.throw(
				_(
					"Bridge System User must be an existing User or a valid email address to auto-create."
				)
			)
