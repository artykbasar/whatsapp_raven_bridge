# Copyright (c) 2026, Local and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from whatsapp_raven_bridge.bridge.conversation import normalize_phone_number


class WhatsAppRavenConversation(Document):
	def validate(self):
		self._normalize_fields()
		self._validate_account_route_mapping()
		self._validate_unique_active_conversation()
		self._normalize_delivery_mode()

	def _normalize_fields(self):
		self.phone_number = normalize_phone_number(self.phone_number)
		if not self.phone_number:
			frappe.throw(_("Phone Number is required after normalization."))

		if not self.display_name:
			self.display_name = self.profile_name or self.phone_number

	def _validate_unique_active_conversation(self):
		if not self.enabled or not _doctype_ready(self.doctype):
			return

		filters = [
			["enabled", "=", 1],
			["phone_number", "=", self.phone_number],
		]

		if not self.is_new():
			filters.append(["name", "!=", self.name])

		if self.whatsapp_account:
			filters.append(["whatsapp_account", "=", self.whatsapp_account])
			duplicate = frappe.get_all(self.doctype, filters=filters, pluck="name", limit=1)
		else:
			duplicate = frappe.get_all(
				self.doctype,
				filters=filters,
				or_filters=[
					["whatsapp_account", "is", "not set"],
					["whatsapp_account", "=", ""],
				],
				pluck="name",
				limit=1,
			)

		if duplicate:
			frappe.throw(
				_(
					"An enabled WhatsApp Raven Conversation already exists for phone number {0} and WhatsApp Account {1}: {2}"
				).format(self.phone_number, self.whatsapp_account or _("not set"), duplicate[0])
			)

	def _validate_account_route_mapping(self):
		if not self.account_route:
			return

		route_account = frappe.db.get_value(
			"WhatsApp Raven Account Route",
			self.account_route,
			"whatsapp_account",
		)
		if route_account and self.whatsapp_account and route_account != self.whatsapp_account:
			frappe.throw(
				_(
					"Account Route {0} is mapped to WhatsApp Account {1}, not {2}."
				).format(self.account_route, route_account, self.whatsapp_account)
			)

	def _normalize_delivery_mode(self):
		mode = (self.get("delivery_mode") or "").strip()
		if not mode:
			mode = "Route Thread"
		if mode not in ("Route Thread", "Private Channel"):
			frappe.throw(_("Unsupported delivery mode: {0}").format(mode))
		self.set("delivery_mode", mode)


def _doctype_ready(doctype):
	try:
		return frappe.db.exists("DocType", doctype) and frappe.db.table_exists(doctype)
	except Exception:
		return False
